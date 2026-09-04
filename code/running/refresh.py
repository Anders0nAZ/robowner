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

from robo import DATA, MODEL_ROOT, RAW, ROOT

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
    a step it had never been asked to run.

    IN SEASON IT IS A TIEBREAK, NOT A SIGNAL. This docstring used to call
    trending "the signal that matters most on a waiver wire", which contradicted
    buzz.py's own docstring saying the module is for August because in season it
    is contaminated by streaming and bye-week churn. Both cannot be true and the
    module knows its own data better: ros.py does not read buzz, and nothing
    lets crowd churn move a valuation. It still runs, because it widens the pool
    a human reads and breaks ties."""
    from robo import buzz
    net = buzz.load(refresh=True)
    top = max(net.values(), default=0)
    return f"{len(net)} players, top {top:,} net adds over {buzz.WINDOW_HOURS}h"


@step("proj-archive")
def capture_projections():
    """One snapshot a day of EVERY remaining week's projections.

    Evidence, not input: nothing reads these. They exist to settle whether
    Sleeper reprices FUTURE weeks on news, which ros.py currently assumes at
    half strength because it has never been observable. See robo/projarchive.py.
    """
    from robo import projarchive
    snap = projarchive.capture()
    n = sum(len(v) for v in snap["weeks"].values())
    return f"{len(snap['weeks'])} weeks, {n} player-weeks"


@step("playoff-odds")
def refresh_playoff_odds():
    """P(we make the playoffs), which weights every playoff week in ros.py.

    Before the board, because ros.py reads these odds and the board does not
    read ros.
    """
    from robo import playoffs
    d = playoffs.simulate()
    import json as _json
    playoffs.CACHE.write_text(_json.dumps(d, indent=1), encoding="utf-8")
    ours = (d.get("odds") or {}).get(d.get("ours") or "", 0)
    return f"{len(d.get('odds') or {})} teams, ours {ours:.1%}"


@step("ros")
def rebuild_ros():
    """The rest-of-season valuation every roster decision is priced on.

    LAST of the data steps on purpose: it reads the board, the model artifact,
    the news verdicts, the fitted role curve and the playoff odds, so every one
    of them must already be today's.
    """
    from robo import ros
    d = ros.build()
    rows = d.get("players") or {}
    top = max((r["mean"] for r in rows.values()), default=0.0)
    import json as _json
    ros.CACHE.write_text(_json.dumps(d), encoding="utf-8")
    return f"{len(rows)} players from week {d['week']}, top {top:.0f}"


MODEL_OUT = MODEL_ROOT / "out"


@step("model")
def refresh_model():
    """The NFL Model's weekly distributions, validated and copied in.

    COPIED, not read in place. Everything the bot runs on lives under its own
    data/ directory, so an NFL Model tree that is missing, half-written, or on
    a drive that did not mount cannot reach robo.lineup -- which writes to
    Sleeper unattended. A failure here leaves yesterday's file, and a file too
    old to trust is refused by model_proj rather than used.
    """
    from robo import LEAGUE_ID_2026, season
    wk = season.current_week()
    src = MODEL_OUT / f"weekly_{season.SEASON}_wk{wk:02d}.json"
    if not src.exists():
        raise FileNotFoundError(f"{src} - has nflmodel.export run for week {wk}?")
    d = json.loads(src.read_text(encoding="utf-8"))
    if d.get("schema") != 1:
        raise ValueError(f"schema {d.get('schema')}, expected 1")
    if str(d.get("season")) != season.SEASON or int(d.get("week", -1)) != wk:
        raise ValueError(f"artifact is {d.get('season')} week {d.get('week')}, "
                         f"not {season.SEASON} week {wk}")
    if d.get("league_id") != LEAGUE_ID_2026:
        raise ValueError(f"artifact is for league {d.get('league_id')}")
    players = d.get("players") or {}
    # The universe scope is every rostered player plus everyone Sleeper
    # projects, which has never been under 400. A short file means the model
    # ran against a broken anchor, and yesterday's numbers beat a third of
    # this week's.
    if len(players) < 300:
        raise ValueError(f"only {len(players)} players; keeping the old file")
    (DATA / "model_week.json").write_text(json.dumps(d), encoding="utf-8")
    return f"week {wk}, {len(players)} players, anchored {d.get('generated_utc', '?')[:16]}"


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
          refresh_buzz(), refresh_model(), rebuild_board(),
          capture_projections(), refresh_playoff_odds(), rebuild_ros(),
          ingest_chat(), sync_media_pool(), harvest_history(), rebuild_kb(),
          refresh_selfdoc(), publish_code(), publish_devlog(), publish_status()]
    if not args.no_restart:
        ok.append(restart_responder())
    _log(f"=== refresh done: {sum(ok)}/{len(ok)} steps OK ===")


if __name__ == "__main__":
    main()
