"""A daily snapshot of Sleeper's projections for EVERY remaining week.

WHY THIS EXISTS. robo/ros.py values a player by summing his projected rate over
the weeks that are left, and that only beats the frozen preseason board if the
rate actually MOVES when news breaks. For the current week that is proven: six
captures of week 1 taken over two and a half days disagree on 22 of 460
players, and the movers move hard -- +5.47, +4.45, +2.99, which is the shape of
a backup being handed a job. For weeks 5 or 12 it is unknown, because nothing
has ever stored yesterday's copy of week 12 to compare against.

That single unknown cuts both ways and the design is wrong either way:

  * if future weeks DO reprice on news, then also applying a scout verdict to
    them counts the same injury twice;
  * if they DO NOT, the spine is a stale rate and the scout verdict is quietly
    doing all the work.

So this module stops the argument by collecting the evidence. It is deliberately
tiny and has no consumers: it writes a file a day and answers one question a few
weeks from now.

    python -m robo.projarchive --capture       # what the daily refresh runs
    python -m robo.projarchive --list
    python -m robo.projarchive --diff --week 8

WHAT IT STORES. Points under our own scoring, per player, per week -- not the
raw stat blob. The question is whether the NUMBER moved, ~150KB/day answers it,
and the 2MB/day alternative would be storing 3,300 rows of unchanging kicker
projections to answer a question nobody asked. Only players with a game are
written, so absence means bye and never means zero.
"""

import argparse
import json
import time
from datetime import datetime, timezone

from robo import RAW, season

DIR = RAW / "proj_archive"
SCHEMA = 1


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def capture(season_yr: str = season.SEASON, weeks=None) -> dict:
    """Snapshot every week from the current one to the end of the season.

    The current week is included even though ros.py prefers the NFL Model for
    it: it is the only week whose movement we can already verify, so it is the
    control that says the capture itself is working.
    """
    now = season.current_week()
    weeks = list(weeks) if weeks else list(range(now, season.SEASON_WEEKS + 1))
    out = {}
    for w in weeks:
        try:
            wp = season.week_points(w, season_yr)
        except Exception as e:
            out[str(w)] = {}
            print(f"  week {w}: FAILED - {type(e).__name__}: {e}")
            continue
        out[str(w)] = {pid: round(v["pts"], 2)
                       for pid, v in wp.items() if v.get("has_game")}
    snap = {"schema": SCHEMA, "season": str(season_yr),
            "captured": time.time(),
            "captured_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "current_week": now, "weeks": out}
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"proj_{season_yr}_{_stamp()}.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    snap["_path"] = path
    return snap


def snapshots(season_yr: str = season.SEASON) -> list:
    """Every capture we hold, oldest first."""
    if not DIR.exists():
        return []
    return sorted(DIR.glob(f"proj_{season_yr}_*.json"))


def load(path) -> dict:
    """A capture, or {} if it is unreadable.

    An unreadable snapshot is skipped rather than raised on, for the reason
    chat_cursor learned the hard way: one truncated file written during an
    unclean shutdown should not take out every later read.
    """
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d if d.get("schema") == SCHEMA else {}


def diff(week: int, first=None, last=None, min_move: float = 0.5,
         season_yr: str = season.SEASON) -> dict:
    """What moved for `week` between two captures."""
    snaps = snapshots(season_yr)
    if len(snaps) < 2:
        return {"error": f"need two captures, hold {len(snaps)}", "moves": []}
    a, b = load(first or snaps[0]), load(last or snaps[-1])
    if not a or not b:
        return {"error": "a capture was unreadable", "moves": []}
    wa = a["weeks"].get(str(week)) or {}
    wb = b["weeks"].get(str(week)) or {}
    moves = []
    for pid in set(wa) | set(wb):
        x, y = wa.get(pid), wb.get(pid)
        if x is None or y is None:
            # Appearing or vanishing is a schedule change (or a bye correction),
            # not a repricing. Counting it as movement would inflate the answer
            # this module exists to give.
            continue
        if abs(y - x) >= min_move:
            moves.append({"player_id": pid, "was": x, "now": y, "move": round(y - x, 2)})
    moves.sort(key=lambda m: -abs(m["move"]))
    common = len(set(wa) & set(wb))
    return {"week": week, "from": a["captured_iso"], "to": b["captured_iso"],
            "compared": common, "moved": len(moves),
            "share": round(len(moves) / common, 4) if common else 0.0,
            "moves": moves}


def report(week: int | None = None, min_move: float = 0.5,
           season_yr: str = season.SEASON) -> str:
    from robo import sleeper_read as api
    snaps = snapshots(season_yr)
    if len(snaps) < 2:
        return (f"{len(snaps)} capture(s) held. Two are needed to say anything; "
                "the daily refresh writes one a day.")
    pl = api.players()
    now = season.current_week()
    weeks = [week] if week else list(range(now, season.SEASON_WEEKS + 1))
    L = [f"PROJECTION DRIFT - {snaps[0].name} -> {snaps[-1].name}", ""]
    L.append(f"  {'week':<6}{'compared':>9}{'moved':>7}{'share':>8}   biggest movers")
    for w in weeks:
        d = diff(w, min_move=min_move, season_yr=season_yr)
        if d.get("error"):
            L.append(f"  {w:<6}{d['error']}")
            continue
        top = ", ".join(f"{api.player_name(pl, m['player_id'])[:18]} {m['move']:+.1f}"
                        for m in d["moves"][:3])
        L.append(f"  {w:<6}{d['compared']:>9}{d['moved']:>7}"
                 f"{d['share']:>7.1%}   {top}")
    L += ["", "  A future week that never moves means Sleeper is not repricing it,",
          "  and ros.NEWS_APPLY_FUTURE should carry the news instead of halving it."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--capture", action="store_true")
    g.add_argument("--diff", action="store_true")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--min-move", type=float, default=0.5)
    args = ap.parse_args()

    if args.capture:
        snap = capture()
        n = sum(len(v) for v in snap["weeks"].values())
        print(f"captured {len(snap['weeks'])} week(s), {n} player-weeks "
              f"-> {snap['_path'].name}")
        return
    if args.list:
        for p in snapshots():
            d = load(p)
            n = sum(len(v) for v in (d.get("weeks") or {}).values())
            print(f"  {p.name}  {n} player-weeks  {d.get('captured_iso', '?')}")
        return
    print(report(args.week, args.min_move))


if __name__ == "__main__":
    main()
