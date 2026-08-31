"""Daily data refresh — keeps the AI owner's world current without a human.

Run by the RobonerRefresh scheduled task each morning (and by hand before the
draft). Steps are independent; one failing never blocks the rest. After the
data refresh, the chat responder is restarted so its in-process caches (player
dump, lore lru_caches) pick up the new state.

What stays static on purpose: the locked FFC ADP (keeper-cost source of truth),
prior-season chat (immutable history), and — since the draft finished — the LIVE
FFC ADP too. refresh_adp_live() is still here and still works; it is simply not
in the daily list, because average draft position has no meaning or consumer
once the board is full. Put it back with the draft-prep tasks next August.

python -m robo.refresh            # full daily refresh
python -m robo.refresh --no-restart   # data only, leave responder alone
"""

import argparse
import json
import subprocess
import time
from datetime import datetime

import requests

from robo import DATA, RAW, ROOT

LOG = ROOT / "refresh.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def step(name):
    def deco(fn):
        def wrapped(*a, **kw):
            t0 = time.time()
            try:
                out = fn(*a, **kw)
                _log(f"{name}: OK {out if out is not None else ''} ({time.time()-t0:.1f}s)")
                return True
            except Exception as e:
                _log(f"{name}: FAILED — {str(e)[:200]}")
                return False
        return wrapped
    return deco


@step("players")
def refresh_players():
    from robo import sleeper_read as api
    data = api.players(refresh=True)
    return f"{len(data):,} players"


@step("projections")
def refresh_projections():
    r = requests.get(
        "https://api.sleeper.app/projections/nfl/2026?season_type=regular"
        "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF"
        "&order_by=adp_2qb", timeout=60)
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 500:
        raise ValueError(f"suspiciously few rows ({len(rows)}); keeping old file")
    (RAW / "projections_2026.json").write_text(json.dumps(rows), encoding="utf-8")
    return f"{len(rows)} rows"


@step("adp-live")
def refresh_adp_live():
    from robo import adp_live
    out = adp_live.fetch()
    return f"{len(out['players'])} players, drafts through {out['meta'].get('end_date')}"


@step("ecr")
def refresh_ecr():
    from robo import fantasypros
    out = fantasypros.refresh()
    return f"{len(out['players'])} players, updated {out.get('updated')}"


@step("buzz")
def refresh_buzz():
    """Trending adds/drops. Runs BEFORE the board so bench valuation sees today's
    market: ADP moves ~0.6 picks a week in August, trending moves in hours.

    This step was written, documented in CLAUDE.md, and asserted on by the status
    page with a 26-hour budget -- and never actually added to main()'s list, from
    the day it was written until 31 Aug 2026. It stayed fresh by accident: any
    call to buzz.signal() refetches a cache older than STALE_AFTER, and the draft
    tooling called it constantly. With the draft over nothing does, so the file
    would have frozen and the page would have blamed RobonerRefresh for skipping
    a step it had never been asked to run. Trending adds are the fastest signal
    the bot has and the one that matters most on a waiver wire."""
    from robo import buzz
    net = buzz.load(refresh=True)
    top = max(net.values(), default=0)
    return f"{len(net)} players, top {top:,} net adds over {buzz.WINDOW_HOURS}h"


@step("board")
def rebuild_board():
    from robo.rankings import build_board, write_csv
    board = build_board()
    write_csv(board)
    return f"{len(board)} players"


@step("chat-memory")
def ingest_chat():
    from robo import chat_memory
    gm = chat_memory.ingest_groupme()
    sl = chat_memory.ingest_sleeper_chat()
    emb = chat_memory.build_embeddings() if (gm or sl) else 0
    return f"+{gm} groupme, +{sl} sleeper, {emb} embedded"


@step("media-pool")
def sync_media_pool():
    """Pick up whatever the Archive's nightly captioning added to our sources."""
    from robo import archive_media
    added = archive_media.sync()
    emb = archive_media.build_embeddings() if sum(added.values()) else 0
    return f"{added}, {emb} embedded"


@step("history")
def harvest_history():
    # current season only — prior seasons are complete and immutable
    from robo import history
    from robo import LEAGUE_ID_2026
    import sqlite3
    # depth-1 harvest: patch league_chain via a single-league list
    L = __import__("robo.sleeper_read", fromlist=["league"]).league(LEAGUE_ID_2026)
    players = __import__("robo.sleeper_read", fromlist=["players"]).players()
    import robo.history as h
    orig = h.league_chain
    try:
        h.league_chain = lambda start=None: [L]
        h.harvest(verbose=False)
    finally:
        h.league_chain = orig
    c = sqlite3.connect(h.DB)
    n = c.execute("SELECT COUNT(*) FROM matchups WHERE season='2026'").fetchone()[0]
    c.close()
    return f"2026 refreshed ({n} matchup rows)"


@step("selfdoc")
def refresh_selfdoc():
    """Refresh data/selfdoc.md. The CHAT path does not need this -- digest() is
    generated from source on every question, which is why the bot's self-
    knowledge cannot go stale. This only keeps the on-disk snapshot honest for
    anyone reading it directly, which had drifted three days behind."""
    from robo import selfdoc
    d = selfdoc.digest()
    (DATA / "selfdoc.md").write_text(d, encoding="utf-8")
    return f"{len(d)} chars"


@step("kb")
def rebuild_kb():
    from robo import kb
    data = kb.build()
    (DATA / "league_kb.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    (ROOT / "LEAGUE.md").write_text(kb.write_md(data), encoding="utf-8")
    return "league_kb.json + LEAGUE.md"


@step("status")
def publish_status():
    """Runs LAST, so the daily snapshot reflects everything above it having
    landed -- a status page generated before the ingests would report yesterday.
    The 15-minute watchdog keeps it current the rest of the day."""
    from robo import status
    snap = status.snapshot()
    pushed = status.publish(snap)
    return f"{status.VERDICT[snap['overall']]}{'' if pushed else ' (no push needed)'}"


@step("code")
def publish_code():
    """Keep the public source in step with what is actually running.

    Runs BEFORE the devlog step, which is what commits and pushes the site --
    so the code lands in the same push rather than sitting locally until
    something else happens to publish. Allowlisted and credential-scanned; it
    refuses outright rather than publishing something it is unsure about.
    """
    from robo import publish_code as pc
    r = pc.publish(push=False, verbose=False)
    return f"{r['files']} source files ({r['changed']} changed, {r['removed']} removed)"


@step("devlog")
def publish_devlog():
    from robo import devlog, decisions
    devlog.render()
    decisions.render()
    # Report what actually happened. This used to return "...and pushed"
    # unconditionally while discarding publish()'s result, so a rejected push
    # left the public site frozen and the refresh log green.
    if not decisions.publish("daily refresh: regenerate site"):
        raise RuntimeError("site regenerated locally but the push did not land")
    return "changelog + index regenerated and pushed"


@step("restart-responder")
def restart_responder():
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
         "Where-Object { $_.CommandLine -like '*robo.chat_responder*' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, timeout=30)
    time.sleep(2)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / "StartRoboner.ps1")],
                   capture_output=True, timeout=60)
    return "responder bounced (fresh caches)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-restart", action="store_true")
    args = ap.parse_args()
    _log("=== refresh start ===")
    # refresh_adp_live() is NOT in this list. Average draft position stopped
    # meaning anything the moment the board filled, and nothing in season reads
    # it. Put it back with the draft-prep tasks next August; data/adp_live.json
    # simply holds its last pre-draft values until then.
    ok = [refresh_players(), refresh_projections(), refresh_ecr(),
          refresh_buzz(), rebuild_board(),
          ingest_chat(), sync_media_pool(), harvest_history(), rebuild_kb(),
          refresh_selfdoc(), publish_code(), publish_devlog(), publish_status()]
    if not args.no_restart:
        ok.append(restart_responder())
    _log(f"=== refresh done: {sum(ok)}/{len(ok)} steps OK ===")


if __name__ == "__main__":
    main()
