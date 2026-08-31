"""One-time pull of prior-season Sleeper league chat.

Robowner can only read chat for leagues it belongs to, so seasons 2020-2025
need Nate's own token. Set NATE_SLEEPER_TOKEN in .env, run this once, then
delete the line — prior-season chat is immutable history and never needs
re-fetching.

Destinations are deliberately separate:
  RURFFL  -> data/chat_memory.db   (the bot's institutional memory)
  RatDick -> data/ratdick_chat.db  (Nate's search only; the bot never reads it)

python -m robo.pull_chat_history
"""

import os
import sqlite3
import sys
import time

import requests

from robo import DATA, ROOT
from robo.chat_memory import DB as MEMORY_DB, SCHEMA

GRAPHQL = "https://sleeper.com/graphql"
RATDICK_CHAT_DB = DATA / "ratdick_chat.db"
PAGE_GUARD = 80

RURFFL = {"2025": "1255710645953773568", "2024": "1124837824776925184",
          "2023": "988838358723915776", "2022": "832130025401712640",
          "2021": "668937148136759296", "2020": "585571735181504512"}
RATDICK = {"2026": "1378089137722130432", "2025": "1254270636365201408",
           "2024": "1094777600100143104", "2023": "921115614771564544",
           "2022": "816032322930458624", "2021": "668937384200597504",
           "2020": "584815212532637696"}


def _token() -> str:
    tok = os.environ.get("NATE_SLEEPER_TOKEN")
    if not tok:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("NATE_SLEEPER_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
    if not tok:
        raise SystemExit(
            "NATE_SLEEPER_TOKEN not set.\n"
            "Add it to .env (from sleeper.com localStorage 'token' while logged in as\n"
            "anders0nAZ), run this once, then delete the line.")
    return tok


def fetch_league_chat(league_id: str, token: str) -> list[dict]:
    """All messages in one league's chat, paginating backwards (50/page)."""
    out, before, seen = [], None, set()
    for _ in range(PAGE_GUARD):
        q = ('query m { messages(parent_id: "%s"%s) { message_id text created '
             'author_id author_display_name } }'
             % (league_id, f', before: "{before}"' if before else ""))
        r = requests.post(GRAPHQL, json={"operationName": "m", "variables": {}, "query": q},
                          headers={"Authorization": token, "Content-Type": "application/json"},
                          timeout=45)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(body["errors"][0].get("message"))
        page = body["data"]["messages"] or []
        if not page:
            break
        for m in page:
            if m["message_id"] not in seen:
                seen.add(m["message_id"])
                out.append(m)
        before = page[-1]["message_id"]
        if len(page) < 50:
            break
        time.sleep(0.15)
    return out


def store(db_path, rows: list[tuple]) -> int:
    c = sqlite3.connect(db_path)
    c.executescript(SCHEMA)
    n = 0
    for r in rows:
        n += c.execute("""INSERT OR IGNORE INTO messages
                          (id, source, ts, author, author_id, text) VALUES (?,?,?,?,?,?)""",
                       r).rowcount
    c.commit()
    c.close()
    return n


def harvest(leagues: dict, db_path, label: str, token: str) -> int:
    total = 0
    for season, lid in leagues.items():
        try:
            msgs = fetch_league_chat(lid, token)
        except Exception as e:
            print(f"  {label} {season}: FAILED ({str(e)[:80]})")
            continue
        rows = []
        for m in msgs:
            text = (m.get("text") or "").strip()
            author = m.get("author_display_name")
            if not text or author in (None, "sys"):
                continue          # skip Sleeper's automated notices
            rows.append((f"sl:{m['message_id']}", f"sleeper-{season}",
                         int((m.get("created") or 0) / 1000) or None,
                         author, m.get("author_id"), text))
        added = store(db_path, rows)
        total += added
        print(f"  {label} {season}: {len(msgs):>5} fetched, {added:>5} new human messages stored")
    return total


if __name__ == "__main__":
    token = _token()
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    if only in ("all", "rurffl"):
        print("RURFFL -> chat_memory.db (bot memory)")
        print(f"  total new: {harvest(RURFFL, MEMORY_DB, 'RURFFL', token)}")
    if only in ("all", "ratdick"):
        print("Rat Dick Dynasty -> ratdick_chat.db (search only, NOT bot memory)")
        print(f"  total new: {harvest(RATDICK, RATDICK_CHAT_DB, 'RDD', token)}")
