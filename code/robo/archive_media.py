"""Roboner's reaction-image pool, sourced from the personal GroupMe Archive.

The Archive (C:\\GroupMe Archive) already captions every synced image with a
vision model and embeds those captions nightly (SyncScheduled.ps1). Rather
than rebuild any of that, this module pulls a WHITELISTED SUBSET into the
bot's own database and searches it locally. The bot never queries the Archive
at reply time, so it can only ever see what a SOURCES entry let in.

Two sources are in scope (see SOURCES):
  ruready  every image ever posted in the league's own GroupMe. In-jokes.
  robot    a keyword cut of 'robot' GIFs from the wider archive. On-brand.

Selection is a query, not a snapshot: re-running `sync` picks up whatever the
Archive's nightly captioning has added since.

python -m robo.archive_media sync      # pull new rows from the archive
python -m robo.archive_media embed     # build semantic index (needs Ollama)
python -m robo.archive_media search "<query>"
python -m robo.archive_media verify    # confirm hosted urls still resolve
python -m robo.archive_media stats
"""

import json
import sqlite3
import sys
import time
import zipfile
from functools import lru_cache
from pathlib import Path

import requests

from robo import DATA, ROOT
from robo.chat_memory import (DOC_PREFIX, EMBED_MODEL, OLLAMA, QUERY_PREFIX,
                              _blocked)

ARCHIVE_ROOT = Path(r"C:\GroupMe Archive")
ARCHIVE_DB = ARCHIVE_ROOT / "groupme.db"
DB = DATA / "media_pool.db"
VEC_DB = DATA / "media_vectors.db"

# Cosine distance past which a semantic hit is not a match. 0.5 is the
# Archive's own browse cutoff and is fine for `search`, where a human is
# looking at a grid. PICK_CUTOFF is much tighter because pick() posts without
# review: measured on this pool, a caption that genuinely matches the request
# lands at 0.22-0.29 and nonsense lands at 0.37+, so 0.32 splits them. Loose
# thresholds here don't return nothing, they return a confidently wrong gif.
SEM_CUTOFF = 0.5
PICK_CUTOFF = 0.32

# data/settings.json overrides the two cutoffs above (robo/settings.py).
from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())

def _group_id() -> str:
    """The league's GroupMe group id, from .env like everywhere else.

    It was inlined into the WHERE fragment below until 31 Aug 2026, the one
    place in the package that hardcoded a value the rest of the code reads from
    the environment.
    """
    import os
    gid = os.environ.get("GROUPME_GROUP_ID")
    if not gid:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("GROUPME_GROUP_ID="):
                    gid = line.split("=", 1)[1].strip()
    return gid or ""


# The whitelist. Each entry is a WHERE fragment against media_alt `ma` joined
# to messages `m`; anything not selected here is invisible to the bot. This is
# an allowlist on purpose — the same database holds quarantined groups, and a
# blocklist would fail open the first time a source is added.
SOURCES = {
    "ruready": {
        "where": "ma.local_path LIKE ?",
        "params": [f"media/{_group_id()}/%"],
        "join": "",
    },
    "robot": {
        "where": "media_fts MATCH ? AND lower(ma.local_path) LIKE '%.gif'",
        "params": ["robot"],
        "join": "JOIN media_fts ON media_fts.rowid = ma.rowid",
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool (
    local_path TEXT PRIMARY KEY, source TEXT, message_id TEXT, group_name TEXT,
    sender TEXT, created_at INTEGER, is_gif INTEGER, caption TEXT, ocr_text TEXT,
    src_url TEXT, cdn_url TEXT, local_zip TEXT, added_at INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS pool_fts USING fts5(
    caption, ocr_text, content='pool', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS pool_ai AFTER INSERT ON pool BEGIN
    INSERT INTO pool_fts(rowid, caption, ocr_text)
    VALUES (new.rowid, new.caption, new.ocr_text);
END;
"""


def conn() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    if "local_zip" not in {r[1] for r in c.execute("PRAGMA table_info(pool)")}:
        c.execute("ALTER TABLE pool ADD COLUMN local_zip TEXT")
    return c


# Most media is extracted under media/, but some of it only ever landed inside
# the original export zips. Both are local; the zip ones just need opening.

@lru_cache(maxsize=64)
def _zip_names(zip_name: str) -> tuple[str, ...]:
    z = ARCHIVE_ROOT / "exports" / zip_name
    if not z.exists():
        return ()
    try:
        with zipfile.ZipFile(z) as zf:
            return tuple(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return ()


def _inner_path(local_path: str, local_zip: str | None) -> str | None:
    if not local_zip:
        return None
    base = Path(local_path).name
    return next((n for n in _zip_names(local_zip) if n.endswith(base)), None)


def _local_bytes(local_path: str, local_zip: str | None) -> bytes | None:
    """Image bytes from the extracted file, else from its export zip."""
    disk = ARCHIVE_ROOT / local_path
    if disk.exists():
        return disk.read_bytes()
    inner = _inner_path(local_path, local_zip)
    if not inner:
        return None
    try:
        with zipfile.ZipFile(ARCHIVE_ROOT / "exports" / local_zip) as zf:
            return zf.read(inner)
    except (zipfile.BadZipFile, KeyError, OSError):
        return None


def _is_local(local_path: str, local_zip: str | None) -> bool:
    return (ARCHIVE_ROOT / local_path).exists() or bool(_inner_path(local_path, local_zip))


# ------------------------------------------------------------------ sync

def sync() -> dict:
    """Pull whitelisted rows out of the archive (read-only). Idempotent."""
    src = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    c = conn()
    added = {}
    for name, spec in SOURCES.items():
        rows = src.execute(f"""
            SELECT ma.local_path, ma.caption, ma.ocr_text, ma.message_id,
                   m.group_name, m.sender_name, m.created_at,
                   (SELECT a.url FROM attachments a
                     WHERE a.local_path = ma.local_path LIMIT 1) AS url,
                   (SELECT a.local_zip FROM attachments a
                     WHERE a.local_path = ma.local_path LIMIT 1) AS local_zip
              FROM media_alt ma
              JOIN messages m ON m.id = ma.message_id
              {spec['join']}
             WHERE ma.status='ok' AND {spec['where']}""", spec["params"]).fetchall()
        n = 0
        for r in rows:
            # The archive spans six years of unmoderated group chat. Filter on
            # the way IN, so nothing ugly is ever sitting in the bot's pool.
            if _blocked(f"{r['caption']} {r['ocr_text']}"):
                continue
            # No local copy means nothing we can re-host, and the archived url
            # is not a fallback (see cdn_for) — such a row could never post.
            if not _is_local(r["local_path"], r["local_zip"]):
                continue
            cur = c.execute(
                """INSERT OR IGNORE INTO pool
                   (local_path, source, message_id, group_name, sender, created_at,
                    is_gif, caption, ocr_text, src_url, cdn_url, local_zip, added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["local_path"], name, r["message_id"], r["group_name"],
                 r["sender_name"], r["created_at"],
                 int(r["local_path"].lower().endswith(".gif")),
                 r["caption"] or "", r["ocr_text"] or "", r["url"],
                 None,  # cdn_url is filled on first use — see cdn_for
                 r["local_zip"],
                 int(time.time())))
            n += cur.rowcount
        added[name] = n
    c.commit()
    c.close()
    src.close()
    return added


# ------------------------------------------------------------------ posting

def cdn_for(local_path: str) -> str | None:
    """A postable i.groupme.com url, re-hosting + caching on first use.

    The url the archive recorded is NOT usable, even when it is already an
    i.groupme.com link: GroupMe expires them, and most of this pool's
    originals now return 403. Posting one attaches a broken image, which is
    worse than posting none — so every image is re-uploaded from its local
    copy, and only OUR url is ever cached.

    Never raises. An image that cannot be hosted must not break a reply.
    """
    try:
        c = conn()
        r = c.execute("SELECT cdn_url, local_zip FROM pool WHERE local_path=?",
                      (local_path,)).fetchone()
        if not r:
            c.close()
            return None
        if r["cdn_url"]:
            c.close()
            return r["cdn_url"]
        data = _local_bytes(local_path, r["local_zip"])
        if not data:
            c.close()
            return None
        from robo.groupme import upload_image
        url = upload_image(data)
        c.execute("UPDATE pool SET cdn_url=? WHERE local_path=?", (url, local_path))
        c.commit()
        c.close()
        return url
    except Exception:
        return None


# ------------------------------------------------------------------ search

def _embed(texts: list[str], prefix: str) -> list[list[float]]:
    r = requests.post(f"{OLLAMA}/api/embed",
                      json={"model": EMBED_MODEL, "input": [prefix + t for t in texts]},
                      timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"]


def annotations() -> dict:
    """local_path -> human annotation, from the curation pass.

    Keyed by path here even though curation stores by content hash: the hash is
    what survives a re-sync, but hashing 193 files to embed 193 rows would mean
    reading a quarter-gig off disk to index a few hundred captions. The curation
    file carries the path list per hash, so the fan-out is free.
    """
    f = DATA / "media_curation.json"
    if not f.exists():
        return {}
    out = {}
    for ann in json.loads(f.read_text(encoding="utf-8")).values():
        for path in ann.get("paths", []):
            out[path] = ann
    return out


def index_text(caption: str, ocr: str, ann: dict | None) -> str:
    """What actually gets embedded for one image.

    A human annotation REPLACES the machine caption rather than joining it: the
    caption is frequently wrong (one frame of an animation), and averaging a
    wrong description into a right one just moves the vector somewhere between
    them. `use` is appended because the model queries with a MOMENT in mind even
    when the prompt asks it for a picture, and "gloating" should be able to
    reach a gif whose description never says so.
    """
    if ann and ann.get("shows"):
        return ann["shows"].strip()
    return f"{caption} {ocr}".strip()


def use_text(ann: dict | None) -> str:
    """The "when to reach for it" half, embedded SEPARATELY from the picture.

    Measured on this pool: the query "pouting and denial" sits 0.194 from the
    use text on its own, 0.405 from that text concatenated to the visual
    description, and 0.550 from the description alone -- while "a sad yellow
    robot" wants the description. One vector cannot answer both styles of
    query, so each image carries two and pick() takes whichever is nearer.
    """
    return ((ann or {}).get("use") or "").strip()


def build_embeddings(batch: int = 64) -> int:
    """Embed caption+OCR per image. Cheap — the pool is a few hundred rows."""
    import sqlite_vec
    c = conn()
    rows = c.execute("SELECT rowid, local_path, caption, ocr_text FROM pool").fetchall()
    ann = annotations()
    v = sqlite3.connect(VEC_DB)
    v.enable_load_extension(True)
    sqlite_vec.load(v)
    v.enable_load_extension(False)
    v.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_media USING vec0("
              "rowid INTEGER PRIMARY KEY, embedding float[768] distance_metric=cosine)")
    v.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_use USING vec0("
              "rowid INTEGER PRIMARY KEY, embedding float[768] distance_metric=cosine)")
    have = {r[0] for r in v.execute("SELECT rowid FROM vec_media")}
    # An annotated row is always re-embedded: its text just changed.
    todo = [r for r in rows
            if r["rowid"] not in have or r["local_path"] in ann]
    n = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        vecs = _embed([index_text(r['caption'], r['ocr_text'],
                                  ann.get(r['local_path'])) for r in chunk],
                      DOC_PREFIX)
        for r, vec in zip(chunk, vecs):
            # vec0 rejects INSERT OR REPLACE on its primary key, so a re-embed
            # has to clear the old vector first. Only reachable since annotated
            # rows became eligible for re-indexing.
            v.execute("DELETE FROM vec_media WHERE rowid=?", (r["rowid"],))
            v.execute("INSERT INTO vec_media(rowid, embedding) VALUES (?,?)",
                      (r["rowid"], json.dumps(vec)))
            n += 1
        uses = [(r, use_text(ann.get(r["local_path"]))) for r in chunk]
        uses = [(r, u) for r, u in uses if u]
        if uses:
            uvecs = _embed([u for _, u in uses], DOC_PREFIX)
            for (r, _), uvec in zip(uses, uvecs):
                v.execute("DELETE FROM vec_use WHERE rowid=?", (r["rowid"],))
                v.execute("INSERT INTO vec_use(rowid, embedding) VALUES (?,?)",
                          (r["rowid"], json.dumps(uvec)))
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
            "SELECT rowid FROM pool_fts WHERE pool_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, limit))]
    except sqlite3.OperationalError:
        return []


def _semantic(query: str, limit: int) -> list[tuple[int, float]]:
    """(rowid, distance) within SEM_CUTOFF, nearest first. [] if Ollama is down."""
    try:
        import sqlite_vec
        qv = _embed([query], QUERY_PREFIX)[0]
        v = sqlite3.connect(f"file:{VEC_DB}?mode=ro", uri=True)
        v.enable_load_extension(True)
        sqlite_vec.load(v)
        v.enable_load_extension(False)
        best: dict[int, float] = {}
        for table in ("vec_media", "vec_use"):
            try:
                rows = v.execute(
                    f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? "
                    "AND k = ? ORDER BY distance", (json.dumps(qv), limit))
            except sqlite3.OperationalError:
                continue        # vec_use absent until something has been curated
            for rid, dist in rows:
                if dist <= SEM_CUTOFF and dist < best.get(rid, 9):
                    best[rid] = dist
        v.close()
        return sorted(best.items(), key=lambda kv: kv[1])
    except Exception:
        return []  # degrade to keyword-only, same philosophy as chat_memory


def search(query: str, limit: int = 5, k: int = 60) -> list[dict]:
    """Hybrid keyword + semantic search over the pool, fused via RRF."""
    c = conn()
    kw = _fts(c, query, limit * 4)
    scored = _semantic(query, limit * 4)
    dist = dict(scored)
    sem = [rid for rid, _d in scored]
    scores = {}
    for ranked in (kw, sem):
        for rank, rid in enumerate(ranked, 1):
            scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank)
    if not scores:
        c.close()
        return []
    top = sorted(scores, key=lambda r: -scores[r])[:limit]
    rows = c.execute(
        "SELECT rowid, local_path, source, caption, ocr_text, group_name, is_gif "
        f"FROM pool WHERE rowid IN ({','.join('?' * len(top))})", top).fetchall()
    c.close()
    by_id = {r["rowid"]: r for r in rows}
    out = []
    for rid in top:
        r = by_id.get(rid)
        if not r:
            continue
        out.append({"local_path": r["local_path"], "source": r["source"],
                    "caption": r["caption"], "ocr_text": r["ocr_text"],
                    "group_name": r["group_name"], "is_gif": bool(r["is_gif"]),
                    "distance": dist.get(rid),
                    "matched_by": ("both" if rid in kw and rid in sem
                                   else "keyword" if rid in kw else "semantic")})
    return out


def pick(description: str) -> tuple[str | None, dict | None]:
    """Best image for a free-text description -> (postable_url, hit).

    Only a tight semantic match qualifies. Keyword-only hits are rejected
    outright: FTS ORs the terms, so "a giraffe eating spaghetti" matches a gif
    of someone eating pancakes. If Ollama is down there are no semantic hits
    and the bot simply stops attaching images, which is the correct failure.
    """
    # A human "drop" verdict is a veto, applied here rather than in search() so
    # the image stays visible while curating and only becomes unpostable.
    _dropped = {p for a in annotations().values() if a.get("verdict") == "drop"
                for p in a.get("paths", [])}
    hits = [h for h in search(description, limit=3)
            if h["distance"] is not None and h["distance"] <= PICK_CUTOFF
            and h["local_path"] not in _dropped]
    if not hits:
        return None, None
    best = min(hits, key=lambda h: h["distance"])
    url = cdn_for(best["local_path"])
    return (url, best) if url else (None, None)


def verify(limit: int = 25) -> str:
    """Spot-check cached urls still resolve. GroupMe expired the archive's
    original links; if it ever expires ours too, this is how we find out
    before the bot posts another broken image."""
    c = conn()
    rows = c.execute("SELECT local_path, cdn_url FROM pool WHERE cdn_url IS NOT NULL "
                     "ORDER BY added_at LIMIT ?", (limit,)).fetchall()
    c.close()
    bad = []
    for r in rows:
        try:
            if requests.head(r["cdn_url"], timeout=20).status_code != 200:
                bad.append(r["local_path"])
        except requests.RequestException:
            bad.append(r["local_path"])
    if not rows:
        return "no hosted urls yet — nothing to verify"
    return (f"{len(rows) - len(bad)}/{len(rows)} hosted urls OK"
            + (f"\nDEAD (clear cdn_url to re-host): {', '.join(bad)}" if bad else ""))


def stats() -> str:
    c = conn()
    rows = c.execute("SELECT source, COUNT(*), SUM(is_gif), SUM(cdn_url IS NOT NULL) "
                     "FROM pool GROUP BY source").fetchall()
    c.close()
    return "\n".join(f"{r[0]}: {r[1]} images ({r[2]} gif, {r[3]} already hosted)"
                     for r in rows) or "pool empty — run `sync`"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "sync":
        print(f"added: {sync()}")
        print(stats())
    elif cmd == "embed":
        print(f"embedded {build_embeddings()}")
    elif cmd == "verify":
        print(verify())
    elif cmd == "search":
        for h in search(" ".join(sys.argv[2:])):
            print(f"[{h['matched_by']:8}] {h['source']:8} {h['caption'][:80]}")
            print(f"           {h['local_path']}")
    else:
        print(stats())
