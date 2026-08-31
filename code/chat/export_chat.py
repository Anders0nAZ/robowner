"""Export league chat transcripts for human reading.

Produces an annotated markdown transcript of the league's key episodes plus a
full CSV of every message, so Nate can re-read (or search) the history himself.

python -m robo.export_chat
"""

import csv
import html
import re
import sqlite3
from datetime import datetime, timezone

from robo import DATA, ROOT

DB = DATA / "chat_memory.db"
OUT_DIR = ROOT / "exports"

# Episodes worth reading as a story rather than as search results.
EPISODES = [
    ("2024-08-24", "2024-08-28", "The Collapse",
     "A draft-pick trade between Jake (JCGlock) and Eric (MorrieDuckett) — Eric traded down "
     "six spots in round 1 to move up three in round 8, while his first-rounder was already "
     "spoken for by a Josh Allen keeper. Ryan (rpsulli) called it collusion. Over 19 hours the "
     "league lost three owners, Bob disbanded and DELETED the GroupMe, dues were offered back, "
     "and the league was resurrected only when Bob agreed to run it with executive power. "
     "The constitution was abandoned the same afternoon. This is the origin of the Chancellorship."),
    ("2024-11-09", "2024-11-11", "Life After the Constitution",
     "The first real test of the new order: a trade made explicitly on the grounds that there is "
     "no longer a constitution or a vote to appeal to."),
    ("2024-12-23", "2024-12-26", "Christmas Eve Playoff Bile",
     "Playoff-week trash talk, included for tone — this is what the league sounded like after "
     "the reset."),
    ("2025-08-20", "2025-08-23", "RUReady Lives Again",
     "A new GroupMe is created, one year after the old one was deleted. Note Chris Sindik's "
     "opening question: 'Anyone quit over rules arguments again?'"),
    ("2025-08-27", "2025-08-31", "The Chancellorship Named",
     "Bob reconstructs the rules from the old document and coins the title: 'Supreme Chancellor "
     "is the title for a reason.' The power dated to August 2024; the name is from here."),
    ("2025-10-23", "2025-10-25", "Not a Full Blown Dictatorship",
     "Bob navigating a lineup/roster dispute, and explicitly bounding his own authority."),
]


def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _ts(day: str, end: bool = False) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp()) + (86400 if end else 0)


def _clean(text: str) -> str:
    t = html.unescape(text or "")
    t = re.sub(r"<@([^>]+)>", r"@\1", t)          # sleeper mention markup
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)   # sleeper link markup
    return t.strip()


def fetch(start: str, end: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("""SELECT ts, author, text, source FROM messages
                            WHERE ts BETWEEN ? AND ? ORDER BY ts""",
                         (_ts(start), _ts(end, True))).fetchall()


def write_markdown() -> tuple:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "league-history-transcripts.md"
    total = 0
    with path.open("w", encoding="utf-8") as f:
        f.write("# RURFFL — Annotated Chat Transcripts\n\n")
        f.write("Reconstructed from Sleeper league chat (2020-2026) and the RUReady GroupMe.\n\n")
        f.write("> **The short version:** the league died on 26 August 2024 over a draft-pick "
                "trade. Bob deleted the GroupMe that afternoon. It was revived hours later only "
                "on the condition that he run it with executive authority, and the constitution "
                "was abandoned in the same conversation. The 'Supreme Chancellor' title was "
                "coined a year later, in August 2025.\n\n")
        f.write("---\n\n## Contents\n\n")
        for i, (s, e, title, _) in enumerate(EPISODES, 1):
            f.write(f"{i}. [{title}](#{i}-{title.lower().replace(' ', '-')}) — {s}\n")
        f.write("\n---\n")
        for i, (start, end, title, blurb) in enumerate(EPISODES, 1):
            rows = fetch(start, end)
            total += len(rows)
            f.write(f"\n## {i}. {title}\n\n*{start} to {end} — {len(rows)} messages*\n\n")
            f.write(f"{blurb}\n\n")
            last_day = None
            for r in rows:
                dt = datetime.fromtimestamp(r["ts"], timezone.utc)
                day = dt.strftime("%A, %B %d %Y")
                if day != last_day:
                    f.write(f"\n### {day}\n\n")
                    last_day = day
                body = _clean(r["text"])
                if not body:
                    continue
                f.write(f"**{dt.strftime('%H:%M')}  {r['author']}:** {body}\n\n")
    return path, total


def write_csv() -> tuple:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "league-chat-full.csv"
    with _conn() as c:
        rows = c.execute("""SELECT ts, author, source, text FROM messages
                            WHERE ts IS NOT NULL ORDER BY ts""").fetchall()
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "time", "author", "source", "text"])
        for r in rows:
            dt = datetime.fromtimestamp(r["ts"], timezone.utc)
            w.writerow([dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"),
                        r["author"], r["source"], _clean(r["text"])])
    return path, len(rows)


if __name__ == "__main__":
    md, n_md = write_markdown()
    csv_path, n_csv = write_csv()
    print(f"{md}  ({n_md} messages across {len(EPISODES)} episodes)")
    print(f"{csv_path}  ({n_csv} messages, full corpus)")
