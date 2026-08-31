"""League chat memory — the RUReady GroupMe plus Sleeper league chat, searchable.

Cribs the GroupMe Archive's retrieval design (archive_search.py): keyword FTS5
and semantic KNN fused via Reciprocal Rank Fusion, degrading gracefully to
keyword-only when Ollama or the vector index is unavailable.

Scope is deliberately narrow: ONLY the league's own chats. The wider personal
archive (51 groups, DMs) is out of bounds — this bot posts publicly to twelve
people and must never surface something said in another room.

python -m robo.chat_memory ingest     # pull messages + build FTS
python -m robo.chat_memory embed      # build semantic index (needs Ollama)
python -m robo.chat_memory search "<query>"
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone

import requests

from robo import DATA

ARCHIVE_DB = r"C:\GroupMe Archive\groupme.db"
LEAGUE_GROUP = "RUReady Lives Again?"      # the ONLY archive group in scope
DB = DATA / "chat_memory.db"
VEC_DB = DATA / "chat_vectors.db"
OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, source TEXT, ts INTEGER, author TEXT,
    author_id TEXT, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""


def conn() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ------------------------------------------------------------------ ingest

def ingest_groupme() -> int:
    """Pull the league GroupMe group out of the personal archive (read-only)."""
    src = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute("""SELECT id, created_at, sender_name, sender_id, text
                          FROM messages WHERE group_name=? AND text IS NOT NULL AND text != ''""",
                       (LEAGUE_GROUP,)).fetchall()
    src.close()
    c = conn()
    n = 0
    for r in rows:
        cur = c.execute("""INSERT OR IGNORE INTO messages (id, source, ts, author, author_id, text)
                           VALUES (?,?,?,?,?,?)""",
                        (f"gm:{r['id']}", "groupme", r["created_at"], r["sender_name"],
                         r["sender_id"], r["text"]))
        n += cur.rowcount
    c.commit()
    c.close()
    return n


def ingest_sleeper_chat() -> int:
    """Pull the Sleeper league chat we have access to (2026 only — prior
    seasons require membership, which Robowner doesn't have)."""
    from robo.sleeper_chat import messages as sleeper_messages
    try:
        msgs = sleeper_messages(limit=500)
    except Exception as e:
        print(f"  sleeper chat unavailable: {e}")
        return 0
    c = conn()
    n = 0
    for m in msgs:
        if not m.get("text") or m.get("name") in (None, "sys"):
            continue
        ts = int((m.get("created") or 0) / 1000) or None
        cur = c.execute("""INSERT OR IGNORE INTO messages (id, source, ts, author, author_id, text)
                           VALUES (?,?,?,?,?,?)""",
                        (f"sl:{m['id']}", "sleeper", ts, m["name"], m.get("author_id"), m["text"]))
        n += cur.rowcount
    c.commit()
    c.close()
    return n


# ------------------------------------------------------------------ search

def _embed(texts: list[str], prefix: str) -> list[list[float]]:
    r = requests.post(f"{OLLAMA}/api/embed",
                      json={"model": EMBED_MODEL, "input": [prefix + t for t in texts]},
                      timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"]


def build_embeddings(batch: int = 64) -> int:
    import sqlite_vec
    c = conn()
    rows = c.execute("SELECT rowid, text FROM messages").fetchall()
    v = sqlite3.connect(VEC_DB)
    v.enable_load_extension(True)
    sqlite_vec.load(v)
    v.enable_load_extension(False)
    v.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_msgs USING vec0("
              "rowid INTEGER PRIMARY KEY, embedding float[768] distance_metric=cosine)")
    have = {r[0] for r in v.execute("SELECT rowid FROM vec_msgs")}
    todo = [r for r in rows if r["rowid"] not in have]
    n = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vecs = _embed([r["text"] for r in chunk], DOC_PREFIX)
        for r, vec in zip(chunk, vecs):
            v.execute("INSERT OR REPLACE INTO vec_msgs(rowid, embedding) VALUES (?,?)",
                      (r["rowid"], json.dumps(vec)))
            n += 1
        v.commit()
    v.close()
    c.close()
    return n


def _fts(c, query: str, limit: int) -> list[int]:
    q = " OR ".join(w for w in "".join(ch if ch.isalnum() or ch.isspace() else " "
                                       for ch in query).split() if len(w) > 2)
    if not q:
        return []
    try:
        return [r[0] for r in c.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, limit))]
    except sqlite3.OperationalError:
        return []


def _semantic(query: str, limit: int) -> list[int]:
    try:
        import sqlite_vec
        qv = _embed([query], QUERY_PREFIX)[0]
        v = sqlite3.connect(f"file:{VEC_DB}?mode=ro", uri=True)
        v.enable_load_extension(True)
        sqlite_vec.load(v)
        v.enable_load_extension(False)
        out = [r[0] for r in v.execute(
            "SELECT rowid FROM vec_msgs WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (json.dumps(qv), limit))]
        v.close()
        return out
    except Exception:
        return []  # degrade to keyword-only, same philosophy as the Historian


import re

# Six years of unmoderated trash talk contains plenty that is funny in context
# and indefensible when a bot resurfaces it years later in front of everyone.
# Retrieval-side filter: these never come back as quotable history, regardless
# of how well they match. Cheap insurance against context collapse.
_BLOCK = re.compile(
    r"\b(kill(ing|ed)? (you|him|her|them)|domestic violence|suicide|rape|overdose|"
    r"cancer|divorce|died|death of|funeral|hospital|miscarriage|fired|laid off|"
    r"n[i1]gg|f[a4]gg?|retard|\bcunt)", re.I)


def _blocked(text: str) -> bool:
    return bool(_BLOCK.search(text or ""))


def search(query: str, limit: int = 8, k: int = 60) -> list[dict]:
    """Hybrid keyword + semantic search over league chat, fused via RRF.

    Results pass a content filter first — see _BLOCK.
    """
    c = conn()
    kw = _fts(c, query, limit * 4)
    sem = _semantic(query, limit * 4)
    scores = {}
    for ranked in (kw, sem):
        for rank, rid in enumerate(ranked, 1):
            scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank)
    if not scores:
        c.close()
        return []
    top = sorted(scores, key=lambda r: -scores[r])[:limit]
    rows = c.execute(
        f"SELECT rowid, source, ts, author, text FROM messages WHERE rowid IN ({','.join('?'*len(top))})",
        top).fetchall()
    c.close()
    by_id = {r["rowid"]: r for r in rows}
    out = []
    for rid in top:
        r = by_id.get(rid)
        if not r or _blocked(r["text"]):
            continue
        out.append({"source": r["source"], "author": r["author"], "text": r["text"],
                    "ts": r["ts"], "matched_by": ("both" if rid in kw and rid in sem
                                                  else "keyword" if rid in kw else "semantic")})
    return out


def fmt(hits: list[dict]) -> str:
    if not hits:
        return "Nothing in league chat history matches that."
    lines = []
    for h in hits:
        when = (datetime.fromtimestamp(h["ts"], timezone.utc).strftime("%b %d %Y")
                if h["ts"] else "?")
        lines.append(f"[{when}] {h['author']}: {h['text'][:280]}")
    return "\n".join(lines)


def stats() -> str:
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    rng = c.execute("SELECT MIN(ts), MAX(ts) FROM messages WHERE ts IS NOT NULL").fetchone()
    src = dict(c.execute("SELECT source, COUNT(*) FROM messages GROUP BY source").fetchall())
    c.close()
    f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else "?"
    return f"{n:,} messages {src} spanning {f(rng[0])} .. {f(rng[1])}"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "ingest":
        print(f"groupme: +{ingest_groupme()}   sleeper: +{ingest_sleeper_chat()}")
        print(stats())
    elif cmd == "embed":
        print(f"embedded {build_embeddings()} messages")
    elif cmd == "search":
        print(fmt(search(" ".join(sys.argv[2:]))))
    else:
        print(stats())
