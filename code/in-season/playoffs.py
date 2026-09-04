"""P(this team makes the playoffs) -- the weight ros.py hangs the future on.

WHY A ROSTER MODULE NEEDS THIS. The rest-of-season value of a player is not the
sum of his remaining weeks; it is the sum of the weeks that still MATTER to us.
This league takes 6 of 12 and starts its playoffs in week 15, so weeks 15-17 are
worth what our chances of being there are worth. At 6-2 in week 8 a week-16
point is as good as a week-9 point. At 2-6 it is worth almost nothing, and the
bot should be renting whatever helps it win now.

Without this the alternative is a flat horizon, which quietly asserts that a
3-10 team in week 14 is still playing for something.

HOW IT SIMULATES. Every remaining week, each team scores
Normal(its own projected lineup that week, WEEKLY_SD). Both numbers are measured
rather than assumed:

  * the mean is that roster's OPTIMAL lineup under the same projections
    lineup.py uses, recomputed per week -- so byes and a thin bench show up on
    their own, without a bye term anywhere in here;
  * WEEKLY_SD is this league's own within-team week-to-week spread, 23.8 points
    over 72 team-seasons in history.db. The decomposition checks out: within-team
    23.8 and between-team 11.6 combine to 26.5 against an observed all-scores
    26.8, so the two halves are not double-counting.

Seeding is wins, then points for, which is what this league's settings say.

WHAT THESE ODDS ARE WORST AT, STATED RATHER THAN CORRECTED. Projections are
regressed: run in week 1 the projected strength spread across twelve teams is
about sd 4.8, while the realised between-team spread in this league's history is
11.6. So before any games are played every team looks nearer average than it is
and the odds sit closer to 50% than they should. It is not fudged with a scaling
factor, because 11.6 is a spread of REALISED averages and carries a season of
luck inside it -- the true talent spread is somewhere between the two and
nothing here can say where. The error also fixes itself: from week 3 or so the
simulation is carrying real records, which know things projections do not. The
report prints both numbers every run so the gap stays visible.

THE CIRCULARITY, AND HOW IT IS BROKEN. ros.py weights playoff weeks by these
odds, and these odds come from projected lineups. If lineup strength here used
the WEIGHTED rest-of-season number, the two would define each other. It does
not: strength is this week's optimal lineup, a quantity that needs no horizon at
all. One pass, no fixed point, and nothing here imports ros.

    python -m robo.playoffs
    python -m robo.playoffs --sims 50000 --json
"""

import argparse
import json
import random
import time

from robo import DATA, LEAGUE_ID_2026, ROBOWNER_USER_ID, lineup, season, settings
from robo import sleeper_read as api

CACHE = DATA / "playoff_odds.json"
SCHEMA = 1

# This league's own within-team weekly spread: the standard deviation of a
# team's score around its own average, median across 72 team-seasons of
# history.db. NOT the spread of all scores (26.8), which also contains the real
# difference between good and bad teams -- and that difference is already in the
# projected lineup, so using 26.8 would count it twice.
WEEKLY_SD = 23.8

SIMS = 10000

# Odds older than this are stale enough to recompute. They move on results, and
# results arrive weekly.
MAX_AGE_H = 30.0

settings.apply(__name__, globals())


# --------------------------------------------------------------- league state

def shape(league_id: str = LEAGUE_ID_2026) -> dict:
    """Playoff shape, read from the league rather than declared."""
    s = season.league(league_id)["settings"]
    start = int(s.get("playoff_week_start") or 15)
    return {"teams": int(s.get("playoff_teams") or 6),
            "playoff_week_start": start,
            "last_regular_week": start - 1}


def standings(league_id: str = LEAGUE_ID_2026) -> dict:
    """roster_id -> {wins, losses, ties, fpts, owner_id}."""
    out = {}
    for r in season.live_rosters(league_id):
        st = r.get("settings") or {}
        out[int(r["roster_id"])] = {
            "wins": int(st.get("wins") or 0), "losses": int(st.get("losses") or 0),
            "ties": int(st.get("ties") or 0),
            "fpts": float(st.get("fpts") or 0) + float(st.get("fpts_decimal") or 0) / 100.0,
            "owner_id": r.get("owner_id"), "players": r.get("players") or []}
    return out


def remaining_schedule(from_week: int, last_week: int,
                       league_id: str = LEAGUE_ID_2026) -> dict:
    """week -> [(roster_id, roster_id), ...] for the weeks still to play."""
    out = {}
    for w in range(from_week, last_week + 1):
        try:
            rows = api.matchups(league_id, w)
        except Exception:
            continue
        pairs, by_mid = [], {}
        for r in rows:
            mid = r.get("matchup_id")
            if mid is None:
                continue
            by_mid.setdefault(mid, []).append(int(r["roster_id"]))
        for mid in sorted(by_mid):
            side = by_mid[mid]
            if len(side) == 2:
                pairs.append((side[0], side[1]))
        if pairs:
            out[w] = pairs
    return out


def strength(weeks: list[int], league_id: str = LEAGUE_ID_2026) -> dict:
    """roster_id -> {week: expected points from that week's optimal lineup}.

    Uses lineup.optimize, the same exact DP that sets our own lineup, so a
    team's strength is what it would score if it started its best eleven --
    not the sum of everyone it rosters. A bye week shows up as a real dip
    because the bye players are unstartable and the optimizer leaves the slot
    to whoever is left.
    """
    players = api.players()
    rosters = standings(league_id)
    out = {}
    for rid, r in rosters.items():
        per_week = {}
        for w in weeks:
            cands, _ = lineup.project_roster(r["players"], season.SEASON, w,
                                             players, league_id)
            _, total = lineup.optimize(cands)
            per_week[w] = total
        out[rid] = per_week
    return out


# ----------------------------------------------------------------- simulation

def simulate(league_id: str = LEAGUE_ID_2026, sims: int = SIMS,
             seed: int = 20260904) -> dict:
    sh = shape(league_id)
    now = season.current_week()
    first = min(now, sh["last_regular_week"])
    st = standings(league_id)
    sched = remaining_schedule(first, sh["last_regular_week"], league_id)
    weeks = sorted(sched)
    stg = strength(weeks, league_id) if weeks else {}

    rids = sorted(st)
    made = {r: 0 for r in rids}
    seeds = {r: [0] * len(rids) for r in rids}
    rng = random.Random(seed)

    for _ in range(sims):
        wins = {r: st[r]["wins"] + 0.5 * st[r]["ties"] for r in rids}
        pts = {r: st[r]["fpts"] for r in rids}
        for w in weeks:
            score = {r: rng.gauss(stg.get(r, {}).get(w, 100.0), WEEKLY_SD)
                     for r in rids}
            for a, b in sched[w]:
                pts[a] += score[a]
                pts[b] += score[b]
                if score[a] > score[b]:
                    wins[a] += 1
                elif score[b] > score[a]:
                    wins[b] += 1
                else:
                    wins[a] += 0.5
                    wins[b] += 0.5
        order = sorted(rids, key=lambda r: (-wins[r], -pts[r]))
        for i, r in enumerate(order):
            seeds[r][i] += 1
            if i < sh["teams"]:
                made[r] += 1

    odds = {r: round(made[r] / sims, 4) for r in rids}
    users = {u["user_id"]: (u.get("display_name") or "?")
             for u in api.users(league_id)}
    return {"schema": SCHEMA, "computed": time.time(),
            "season": season.SEASON, "week": now, "sims": sims,
            "playoff_teams": sh["teams"],
            "playoff_week_start": sh["playoff_week_start"],
            "weeks_simulated": weeks,
            "odds": {str(r): odds[r] for r in rids},
            "seeds": {str(r): [round(c / sims, 4) for c in seeds[r]] for r in rids},
            "strength": {str(r): round(sum(v.values()) / len(v), 1)
                         for r, v in stg.items()} if stg else {},
            "names": {str(r): users.get(st[r]["owner_id"] or "", "?") for r in rids},
            "ours": next((str(r) for r in rids
                          if st[r]["owner_id"] == ROBOWNER_USER_ID), None)}


# ---------------------------------------------------------------- the cache

def load(refresh: bool = False, league_id: str = LEAGUE_ID_2026) -> dict:
    """Cached odds, recomputed when stale. Never raises.

    A failure here must not take a roster run down: ros.py falls back to
    treating the playoff weeks at full weight, which is the pre-existing flat
    behaviour rather than a silent zero.
    """
    if not refresh and CACHE.exists():
        try:
            d = json.loads(CACHE.read_text(encoding="utf-8"))
            if (d.get("schema") == SCHEMA
                    and (time.time() - d.get("computed", 0)) / 3600.0 < MAX_AGE_H):
                return d
        except Exception:
            pass
    try:
        d = simulate(league_id)
    except Exception:
        return {}
    try:
        CACHE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass
    return d


def p_playoffs(roster_id: int | None = None, league_id: str = LEAGUE_ID_2026,
               default: float = 1.0) -> float:
    """Our odds, or a given team's. `default` when there is nothing to read.

    The default is 1.0 ON PURPOSE: with no odds, every remaining week counts in
    full, which is the flat horizon this module set out to improve on rather
    than a value that would zero the future out.
    """
    d = load(league_id=league_id)
    if not d:
        return default
    key = str(roster_id) if roster_id is not None else d.get("ours")
    if key is None:
        return default
    v = (d.get("odds") or {}).get(key)
    return default if v is None else float(v)


# ------------------------------------------------------------------- reports

def report(refresh: bool = False, league_id: str = LEAGUE_ID_2026) -> str:
    d = load(refresh, league_id)
    if not d:
        return "could not compute playoff odds"
    st = standings(league_id)
    L = [f"PLAYOFF ODDS - {d['season']} week {d['week']}, "
         f"{d['sims']:,} sims, top {d['playoff_teams']} of {len(d['odds'])}",
         f"  simulating weeks {d['weeks_simulated'][0] if d['weeks_simulated'] else '-'}"
         f"-{d['weeks_simulated'][-1] if d['weeks_simulated'] else '-'}; "
         f"weekly sd {WEEKLY_SD}", "",
         f"  {'team':<18}{'rec':>8}{'proj/wk':>9}{'playoffs':>10}{'top seed':>10}"]
    rows = sorted(d["odds"].items(), key=lambda kv: -kv[1])
    for rid, p in rows:
        s = st.get(int(rid), {})
        rec = f"{s.get('wins', 0)}-{s.get('losses', 0)}"
        mark = "  <- us" if rid == d.get("ours") else ""
        L.append(f"  {d['names'].get(rid, '?')[:18]:<18}{rec:>8}"
                 f"{d['strength'].get(rid, 0):>9.1f}{p:>9.1%}"
                 f"{d['seeds'][rid][0]:>10.1%}{mark}")
    strengths = list(d["strength"].values())
    if len(strengths) > 2:
        import statistics as _st
        L += ["", f"  projected strength spread: sd {_st.stdev(strengths):.1f} "
                  f"(history says real between-team spread is ~11.6)"]
        L.append("  a much smaller spread here means these odds are closer to a "
                 "coin flip than the league really is")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.sims:
        d = simulate(sims=args.sims)
        CACHE.write_text(json.dumps(d, indent=1), encoding="utf-8")
        print(json.dumps(d, indent=1) if args.json else report())
        return
    if args.json:
        print(json.dumps(load(args.refresh), indent=1))
        return
    print(report(args.refresh))


if __name__ == "__main__":
    main()
