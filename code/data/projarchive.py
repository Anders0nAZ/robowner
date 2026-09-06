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

THE SEASON PROJECTION IS CAPTURED TOO, AND IT IS A DIFFERENT CLOCK. Sleeper's
season file is UNCONDITIONAL -- the chance a man plays is already inside the
projected volume -- while the weekly feed is a rate conditional on him starting
and zeroes every backup. Their ratio is therefore the market's own implied
P(available and in role), which nothing here has ever been able to read. It also
moves on a different schedule: the roster fields (`team`, `injury_status`) flip
within hours of a transaction, while the volume waits for Sleeper's nightly
compute. Trey Benson, waived 25 Aug 2026, lost his team field the same morning
and kept 30 projected carries until the 26th or 27th.

Both clocks have to be in the SAME capture or neither is datable. The only
reason that Benson gap is known at all is that data/raw/projections_2026.json
happens to be git-tracked, and `last_modified` is no help -- all 3,304 rows
restamp on every pull while the values sit unchanged, so values must be diffed.

    python -m robo.projarchive --capture       # what the daily refresh runs
    python -m robo.projarchive --list
    python -m robo.projarchive --diff --week 8
    python -m robo.projarchive --season-diff
    python -m robo.projarchive --lag

WHAT IT STORES. Weekly: points under our own scoring, per player, per week --
not the raw stat blob. The question is whether the NUMBER moved, ~150KB/day
answers it, and the 2MB/day alternative would be storing 3,300 rows of
unchanging kicker projections to answer a question nobody asked. Only players
with a game are written, so absence means bye and never means zero. Season:
points under our scoring plus the volume it was built from, because "Josh Jacobs
217 -> 93 carries" is legible as a role change in a way that "his points fell"
is not.
"""

import argparse
import json
import time
from datetime import datetime, timezone

from robo import RAW, season

DIR = RAW / "proj_archive"
# 2: the SEASON projection and the roster fields are captured alongside the
# weekly points. Schema 1 held only weekly, which can date when a number moved
# but never why -- and the two clocks below are the whole point.
SCHEMA = 2
READABLE_SCHEMAS = (1, 2)

# The volume the season projection is built from. Points alone would show a
# change, but not that it was a role change: Josh Jacobs going 217 -> 93 carries
# is legible in a way that "his points fell" is not.
VOLUME_KEYS = ("pass_att", "pass_yd", "rush_att", "rush_yd", "rec", "rec_yd")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def season_block(season_yr: str = season.SEASON) -> dict:
    """{pid: {pts, vol, team, inj}} from the SEASON projection file.

    TWO CLOCKS, AND THEY MUST BE CAPTURED TOGETHER. The roster fields move on
    the transaction -- Trey Benson's `team` went ARI -> None within about four
    hours of being waived -- while the projected volume waits for Sleeper's
    nightly compute and took two to three days to zero him out. Storing only
    the slow one leaves a file that changed with nothing on record saying what
    changed underneath it, which is exactly the Jordyn Tyson shape: a
    projection sitting unmoved through a Doubtful -> IR designation.

    MEMBERSHIP IS THE UNION OF THE TWO CLOCKS, and it has to be. Keying on the
    roster alone drops a man during the hours he is unrostered -- which is the
    waiver window, the single event this is built to record. Benson on 25 Aug
    read `team: None` with 30 carries still projected, so a roster-only rule
    would have made him VANISH on exactly the day he was waived, and a
    vanishing row is indistinguishable from one that was never there. Keying on
    the projection alone fails the other way, dropping him once the volume is
    zeroed. A player is kept if EITHER clock says he is worth watching, so both
    transitions read as value changes against stable membership.
    """
    from robo import rankings
    sc = season.scoring()
    out = {}
    for r in rankings.load_projections():
        p = r.get("player") or {}
        pid = str(r.get("player_id") or p.get("player_id") or "")
        if not pid:
            continue
        st = r.get("stats") or {}
        vol = {k: st[k] for k in VOLUME_KEYS if st.get(k)}
        if not (p.get("team") or vol or p.get("injury_status")):
            continue
        out[pid] = {"pts": rankings.custom_points(st, sc), "vol": vol,
                    "team": p.get("team"),
                    "inj": p.get("injury_status")}
    return out


def capture(season_yr: str = season.SEASON, weeks=None) -> dict:
    """Snapshot every remaining week, plus the season projection behind them.

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
    # A failed season read leaves the block absent rather than empty, so a
    # later diff skips the pair instead of reporting every player as moved.
    try:
        sb = season_block(season_yr)
    except Exception as e:
        sb = None
        print(f"  season block: FAILED - {type(e).__name__}: {e}")
    snap = {"schema": SCHEMA, "season": str(season_yr),
            "captured": time.time(),
            "captured_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "current_week": now, "weeks": out}
    if sb is not None:
        snap["season_proj"] = sb
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

    An OLDER schema is still readable. Rejecting it would have thrown away the
    only captures we hold on the day the schema moved, which is the opposite of
    what an archive is for; callers that need the newer block check for it.
    """
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d if d.get("schema") in READABLE_SCHEMAS else {}


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


def _season_of(snap: dict) -> dict:
    return snap.get("season_proj") or {}


def season_diff(first=None, last=None, min_move: float = 1.0,
                season_yr: str = season.SEASON) -> dict:
    """What moved in the SEASON projection between two captures.

    Roster changes are reported ALONGSIDE volume changes rather than filtered
    out, because the interesting rows are the ones where only one of them moved
    -- a designation going Doubtful -> IR over an untouched projection is the
    signal that the slow clock has not caught up yet.
    """
    # Default to the oldest and newest captures that actually CARRY a season
    # block, rather than to the oldest and newest full stop. Schema 1 has no
    # season block, so anchoring on snaps[0] would report "nothing to compare"
    # for as long as one pre-schema-2 file remains on disk -- an archive that
    # goes blind because it has old data in it.
    have = [p for p in snapshots(season_yr) if _season_of(load(p))]
    if first is None and last is None and len(have) < 2:
        return {"error": f"need two captures with a season block, "
                         f"hold {len(have)}", "moves": []}
    a = load(first) if first else load(have[0])
    b = load(last) if last else load(have[-1])
    if not a or not b:
        return {"error": "a capture was unreadable", "moves": []}
    sa, sb = _season_of(a), _season_of(b)
    if not sa or not sb:
        return {"error": "no season block in one of these captures "
                         "(schema 1 predates it)", "moves": []}
    moves = []
    for pid in set(sa) & set(sb):
        x, y = sa[pid], sb[pid]
        vol = {k: (x["vol"].get(k), y["vol"].get(k))
               for k in VOLUME_KEYS
               if (x["vol"].get(k) or 0) != (y["vol"].get(k) or 0)}
        dp = round((y["pts"] or 0) - (x["pts"] or 0), 2)
        roster = {k: (x.get(k), y.get(k)) for k in ("team", "inj")
                  if x.get(k) != y.get(k)}
        if not roster and abs(dp) < min_move and not vol:
            continue
        moves.append({"player_id": pid, "was": x["pts"], "now": y["pts"],
                      "move": dp, "vol": vol, "roster": roster,
                      "kind": ("both" if roster and (vol or abs(dp) >= min_move)
                               else "roster" if roster else "volume")})
    moves.sort(key=lambda m: -abs(m["move"]))
    return {"from": a["captured_iso"], "to": b["captured_iso"],
            "compared": len(set(sa) & set(sb)), "moved": len(moves),
            "moves": moves}


def lag(season_yr: str = season.SEASON) -> dict:
    """How long the projection takes to catch up to a roster event.

    For each player, the earliest capture where `team` or `inj` changed and the
    earliest where the projected volume changed. The gap between them is the
    number ros.py's staleness guard needs, and it is measured here rather than
    assumed -- the one hand-traced case (Benson, waived 25 Aug) came out at two
    to three days, from a single event in a preseason week. One event is an
    existence proof, not a rate.
    """
    snaps = snapshots(season_yr)
    blocks = [(p, load(p)) for p in snaps]
    blocks = [(p, d) for p, d in blocks if _season_of(d)]
    if len(blocks) < 2:
        return {"error": f"need two captures with a season block, "
                         f"hold {len(blocks)}", "events": []}
    events = {}
    for (_, pa), (_, pb) in zip(blocks, blocks[1:]):
        sa, sb, when = _season_of(pa), _season_of(pb), pb["captured"]
        for pid in set(sa) & set(sb):
            x, y = sa[pid], sb[pid]
            e = events.setdefault(pid, {"roster_at": None, "vol_at": None})
            if e["roster_at"] is None and (x.get("team") != y.get("team")
                                           or x.get("inj") != y.get("inj")):
                e["roster_at"] = when
            if e["vol_at"] is None and x["vol"] != y["vol"]:
                e["vol_at"] = when
    out = []
    for pid, e in events.items():
        if e["roster_at"] and e["vol_at"]:
            out.append({"player_id": pid,
                        "lag_h": round((e["vol_at"] - e["roster_at"]) / 3600, 1)})
    out.sort(key=lambda r: r["lag_h"])
    both = [r["lag_h"] for r in out]
    return {"captures": len(blocks), "span_h": round(
                (blocks[-1][1]["captured"] - blocks[0][1]["captured"]) / 3600, 1),
            "with_both": len(out),
            "roster_only": sum(1 for e in events.values()
                               if e["roster_at"] and not e["vol_at"]),
            "vol_only": sum(1 for e in events.values()
                            if e["vol_at"] and not e["roster_at"]),
            "median_lag_h": (sorted(both)[len(both) // 2] if both else None),
            "events": out}


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


def season_report(min_move: float = 1.0, season_yr: str = season.SEASON) -> str:
    from robo import sleeper_read as api
    d = season_diff(min_move=min_move, season_yr=season_yr)
    if d.get("error"):
        return f"  {d['error']}"
    pl = api.players()
    L = [f"SEASON PROJECTION DRIFT - {d['from']} -> {d['to']}",
         f"  {d['moved']} of {d['compared']} players moved", ""]
    L.append(f"  {'player':<22}{'pos':<5}{'was':>7}{'now':>7}{'move':>8}  kind / what")
    for m in d["moves"][:40]:
        p = pl.get(m["player_id"]) or {}
        vol = " ".join(f"{k} {a or 0:g}->{b or 0:g}" for k, (a, b) in m["vol"].items())
        # Kickers and defences carry no volume key we track, so a real points
        # move would otherwise render as a bare "volume:" with nothing after it.
        if not vol and m["kind"] != "roster":
            vol = "points only (no tracked volume key)"
        ros = " ".join(f"{k} {a}->{b}" for k, (a, b) in m["roster"].items())
        L.append(f"  {api.player_name(pl, m['player_id'])[:21]:<22}"
                 f"{str(p.get('position')):<5}{m['was'] or 0:>7.1f}"
                 f"{m['now'] or 0:>7.1f}{m['move']:>+8.1f}  "
                 f"{m['kind']}: {(ros + ' ' + vol).strip()[:60]}")
    L += ["", "  A `roster` row is the fast clock moving with the slow one still",
          "  behind it -- which is the window ros.py cannot yet see across."]
    return "\n".join(L)


def lag_report(season_yr: str = season.SEASON) -> str:
    from robo import sleeper_read as api
    d = lag(season_yr)
    if d.get("error"):
        return f"  {d['error']}"
    pl = api.players()
    L = [f"ROSTER EVENT -> PROJECTION LAG",
         f"  {d['captures']} captures spanning {d['span_h']:.0f}h", "",
         f"  both clocks moved:        {d['with_both']}",
         f"  roster moved, volume not: {d['roster_only']}   <- the stale window",
         f"  volume moved, roster not: {d['vol_only']}"]
    if d["median_lag_h"] is not None:
        L.append(f"  median lag:               {d['median_lag_h']:.1f}h")
    L += ["", f"  {'player':<22}{'lag':>9}"]
    for e in d["events"][:25]:
        L.append(f"  {api.player_name(pl, e['player_id'])[:21]:<22}"
                 f"{e['lag_h']:>8.1f}h")
    L += ["", "  A negative lag is the projection moving FIRST, which is Sleeper",
          "  pricing news the roster has not recorded yet."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--capture", action="store_true")
    g.add_argument("--diff", action="store_true")
    g.add_argument("--season-diff", action="store_true",
                   help="what moved in the SEASON projection")
    g.add_argument("--lag", action="store_true",
                   help="how long the projection trails a roster event")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--min-move", type=float, default=0.5)
    args = ap.parse_args()

    if args.capture:
        snap = capture()
        n = sum(len(v) for v in snap["weeks"].values())
        sb = len(snap.get("season_proj") or {})
        print(f"captured {len(snap['weeks'])} week(s), {n} player-weeks, "
              f"{sb} season rows -> {snap['_path'].name}")
        return
    if args.list:
        for p in snapshots():
            d = load(p)
            n = sum(len(v) for v in (d.get("weeks") or {}).values())
            sb = len(d.get("season_proj") or {})
            print(f"  {p.name}  schema {d.get('schema', '?')}  "
                  f"{n} player-weeks  {sb} season rows  {d.get('captured_iso', '?')}")
        return
    if args.season_diff:
        print(season_report(args.min_move))
        return
    if args.lag:
        print(lag_report())
        return
    print(report(args.week, args.min_move))


if __name__ == "__main__":
    main()
