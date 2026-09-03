"""Weekly lineup optimizer.

Fills QB/RB/RB/WR/WR/TE/FLEX/SUPER_FLEX/K/DEF from the roster to maximize
projected points under our league scoring, benching bye and injured-Out players.
Legality first: never leave a fillable slot empty, and never start an
injured-Out player while a healthy alternative exists.

The projection is the NFL Model's simulated mean where it has one, and
Sleeper's weekly number where it does not -- see robo/model_proj.py, which is
also where every reason for falling back is spelled out. Nothing else changes
with the source: bye, kickoff lock and injury designation all still come from
Sleeper, because the model has no notion of any of them.

THIS RANKS OUR OWN 17 PLAYERS ON THIS WEEK'S PROJECTIONS. It is not a
rest-of-season model and must not become one -- that engine has not been
designed (see robo/value.py). A weekly comparison between players we already
hold is a far smaller claim than "what is this man worth from here", and it is
reversible every week, which is why this runs live while moves.py does not.

python -m robo.lineup [--week N|auto] [--season 2026] [--apply]

    --apply     actually set starters via graphql (otherwise a dry run)
    --compare   both engines' numbers side by side, and whether the lineup
                they choose actually differs. Never writes.
"""

import argparse

from robo import LEAGUE_ID_2026, model_proj, season, settings
from robo import sleeper_read as api

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF"]
SLOT_ELIGIBLE = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
NEVER_START = {"Out", "Suspended", "IR", "PUP", "NA", "COV", "Sus", "DNR"}

# Don't rewrite the lineup for a rounding error. Every write is a public
# decision-log entry, and a lineup that churns by 0.1 points twice a day reads
# as indecision and buries the changes that mattered.
MIN_GAIN_TO_CHANGE = 0.5

# Honour per-player game locks (the league sets bench_lock=1). Off only for
# testing what the optimizer would do with a clean slate.
RESPECT_LOCKS = True

# Assignment weights. FILL dominates everything, so a filled slot always beats
# an empty one; UNSTARTABLE dominates points, so a startable player always beats
# an Out-or-bye player for the same slot, but the latter still gets used rather
# than leaving a hole. Both are far larger than any real weekly projection
# (~40 pts), which is what keeps the ordering strict.
_FILL = 1_000_000.0
_UNSTARTABLE = 100_000.0
# Exact ties between two startable players happen -- Sleeper's projections come
# back rounded to one decimal, so 9.9 against 9.9 was most weeks, and it happened
# in the very first live run (Egbuka, Questionable, against Lloyd, healthy). The
# model's means carry two decimals, which makes an exact tie rarer without making
# it impossible; either way this stays an order of magnitude below the finest
# real difference, so it can only ever separate two identical numbers.
# Both are legal starts and the points are identical, so nothing above decides
# it. Prefer the man carrying no designation: same projection, less chance of a
# Sunday-morning scratch we find out about too late. Smaller than one decimal
# place, so it can ONLY break an exact tie and never outvotes a real difference.
_HEALTHY_TIEBREAK = 0.001

settings.apply(__name__, globals())


def project_roster(player_ids: list[str], season_yr: str, week: int,
                   players_map: dict,
                   league_id: str = LEAGUE_ID_2026) -> tuple[list[dict], str]:
    """Our players, with this week's projection and their game's state.

    Returns (candidates, provenance) -- provenance being one line naming the
    engine behind `pts`, which the decision log publishes.

    ABSENT FROM THE MODEL MEANS "USE SLEEPER'S NUMBER", NEVER ZERO, and that is
    the load-bearing line in this function. simulate_week does not emit a row
    for a player it did not simulate: it skips anyone with no projection, and
    again anyone whose projected opportunity rounds to nothing. If absence
    meant 0.0 here, a startable player the model happened to skip would carry a
    guaranteed zero while has_game stayed True -- invisible to
    illegal_starters(), which only knows about byes and injuries -- and would
    silently drop out of the lineup with nothing anywhere saying why.
    """
    wp = season.week_points(week, season_yr, league_id)
    mp, provenance = model_proj.week_projections(week, season_yr, league_id)
    out = []
    for pid in player_ids:
        p = players_map.get(pid, {})
        w = wp.get(pid) or {}
        m = mp.get(pid) or {}
        sleeper_pts = w.get("pts", 0.0)
        out.append({
            "player_id": pid,
            "name": api.player_name(players_map, pid),
            "pos": p.get("position") or "DEF",
            "team": p.get("team"),
            # Sleeper's, always. The model deliberately does not apply news
            # verdicts to its numbers, so it has no injury opinion to offer.
            "injury": p.get("injury_status"),
            "pts": m["mean"] if "mean" in m else sleeper_pts,
            "pts_source": "model" if "mean" in m else "sleeper",
            "sleeper_pts": sleeper_pts,
            # Carried for the published reasoning, not optimized on. The
            # optimizer maximizes the mean; what a floor and a ceiling are
            # worth in a head-to-head week is a different question and a
            # different objective.
            "p10": m.get("p10"),
            "p90": m.get("p90"),
            # From game_id, not bool(stats): a bye player still gets a projection
            # row carrying a one-key stats blob, so the old bool(stats) test
            # called every bye player active. See season.week_points.
            "has_game": w.get("has_game", False),
            "locked": w.get("locked", False),
            "opponent": w.get("opponent"),
        })
    return out, provenance


def startable(c: dict) -> bool:
    return c["has_game"] and (c["injury"] or "") not in NEVER_START


def optimize(candidates: list[dict],
             pinned: dict[int, dict] | None = None) -> tuple[list, float]:
    """Best legal assignment of players to SLOTS. Exact, not greedy.

    An exact answer is affordable here and a greedy one was not obviously
    correct: with FLEX and SUPER_FLEX both drawing on the same pool, taking the
    best receiver for WR2 can be the wrong move if it strands the flex. The
    space is tiny -- at most 17 players over 10 slots -- so this is a DP over
    subsets of SLOTS (2^10 = 1024 states), which is exhaustive and still runs
    in milliseconds.

    `pinned` maps a slot index to a player who cannot be moved out of it,
    because his game has already kicked off.
    """
    pinned = pinned or {}
    base_mask = 0
    base_val = 0.0
    for i, p in pinned.items():
        base_mask |= 1 << i
        base_val += p["pts"]
    # Sorted, not roster-order. Ties are real here -- two players projecting the
    # same points are genuinely interchangeable between an eligible dedicated
    # slot and FLEX -- and which one the DP reaches first decides the labelling.
    # Fixing the exploration order makes that arbitrary-but-harmless choice
    # STABLE, so the same roster never produces two different published lineups.
    # Same reasoning as choose_pick's tie ordering in draft_agent.
    pool = sorted((c for c in candidates
                   if c["player_id"] not in {p["player_id"] for p in pinned.values()}),
                  key=lambda c: (-c["pts"], bool(c["injury"]), c["player_id"]))

    full = (1 << len(SLOTS)) - 1
    # dp[mask] -> (value, {slot_index: player})
    dp = {base_mask: (base_val, dict(pinned))}
    for c in pool:
        nxt = dict(dp)
        weight = (c["pts"] + _FILL
                  - (0.0 if startable(c) else _UNSTARTABLE)
                  + (0.0 if c["injury"] else _HEALTHY_TIEBREAK))
        for mask, (val, asg) in dp.items():
            for i, slot in enumerate(SLOTS):
                if mask & (1 << i) or c["pos"] not in SLOT_ELIGIBLE[slot]:
                    continue
                nm = mask | (1 << i)
                nv = val + weight
                if nv > nxt.get(nm, (float("-inf"), None))[0]:
                    nxt[nm] = (nv, {**asg, i: c})
        dp = nxt

    best_mask = max(dp, key=lambda m: dp[m][0])
    _, asg = dp[best_mask]
    final = [asg.get(i) for i in range(len(SLOTS))]
    total = round(sum(p["pts"] for p in final if p), 1)
    return final, total


def pin_locked(cands: list[dict], current: list[str]) -> dict[int, dict]:
    """Slots we are not allowed to touch, because the player in them is playing.

    The league sets bench_lock=1, so a player freezes at his own kickoff rather
    than at a league-wide deadline. Without this, a Sunday-afternoon run tries
    to rewrite a lineup that is already half-locked: Sleeper rejects the write
    and we would have no idea which half actually landed.
    """
    if not RESPECT_LOCKS:
        return {}
    by_id = {c["player_id"]: c for c in cands}
    out = {}
    for i, pid in enumerate(current[:len(SLOTS)]):
        c = by_id.get(pid)
        if c and c["locked"]:
            out[i] = c
    return out


def illegal_starters(current: list[str], cands: list[dict]) -> list[str]:
    """Names in the CURRENT lineup that should not be starting at all.

    Legality is not a matter of degree, so it has to bypass MIN_GAIN_TO_CHANGE.
    Otherwise a bye receiver projecting 0.0 survives in the lineup all week
    because the best replacement only gains 0.3 points -- the threshold, meant
    to stop churn, would instead be protecting a guaranteed zero.
    """
    by_id = {c["player_id"]: c for c in cands}
    bad = []
    for pid in current[:len(SLOTS)]:
        if pid in ("0", "", None):
            bad.append("empty slot")
            continue
        c = by_id.get(pid)
        if c is None:
            bad.append(f"{pid} no longer active on our roster")
        elif not c["has_game"]:
            bad.append(f"{c['name']} (bye)")
        elif (c["injury"] or "") in NEVER_START:
            bad.append(f"{c['name']} ({c['injury']})")
    return bad


def describe(lineup: list, total: float) -> str:
    parts = []
    for slot, p in zip(SLOTS, lineup):
        parts.append(f"{slot}: {p['name']}" if p else f"{slot}: EMPTY")
    return "; ".join(parts)


def report(lineup: list, cands: list[dict], total: float,
           pinned: dict[int, dict], provenance: str = "") -> str:
    L = [f"week lineup - {total} projected pts"]
    L.append(f"  source: {provenance or 'Sleeper weekly projections'}")
    starter_ids = [p["player_id"] for p in lineup if p]
    for i, (slot, p) in enumerate(zip(SLOTS, lineup)):
        if not p:
            L.append(f"  {slot:<11} EMPTY - no eligible player on the roster")
            continue
        flag = []
        if p["injury"]:
            flag.append(p["injury"])
        if not p["has_game"]:
            flag.append("BYE")
        if i in pinned:
            flag.append("locked")
        if p.get("pts_source") == "sleeper":
            flag.append("sleeper")
        band = (f"{p['p10']:>5.1f}..{p['p90']:<5.1f}"
                if p.get("p10") is not None else " " * 12)
        L.append(f"  {slot:<11} {p['name']:<24} {p['pts']:>6.1f}  {band}"
                 f"  {'[' + ', '.join(flag) + ']' if flag else ''}")
    bench = sorted((c for c in cands if c["player_id"] not in starter_ids),
                   key=lambda x: -x["pts"])
    L.append("  bench: " + ", ".join(
        f"{c['name']} {c['pts']:.1f}{'' if c['has_game'] else ' (BYE)'}"
        for c in bench))
    return "\n".join(L)


def run(week: int | None = None, season_yr: str = season.SEASON,
        league_id: str = LEAGUE_ID_2026, apply: bool = False,
        roster_id: int | None = None, verbose: bool = True) -> dict:
    week = week or season.current_week()
    players_map = api.players()
    if roster_id is None:
        roster = season.mine(league_id)
    else:
        roster = next(r for r in season.live_rosters(league_id)
                      if r["roster_id"] == roster_id)

    reserve = set(roster.get("reserve") or [])
    active = [p for p in (roster.get("players") or []) if p not in reserve]
    cands, provenance = project_roster(active, season_yr, week, players_map,
                                       league_id)
    current = [p for p in (roster.get("starters") or [])]
    pinned = pin_locked(cands, current)
    lineup, total = optimize(cands, pinned)

    starter_ids = [p["player_id"] if p else "0" for p in lineup]
    cur_total = round(sum(c["pts"] for c in cands
                          if c["player_id"] in set(current)), 1)
    gain = round(total - cur_total, 1)
    changed = starter_ids != current[:len(SLOTS)]

    bad = illegal_starters(current, cands) if current else []
    modelled = sum(1 for c in cands if c["pts_source"] == "model")
    out = {"week": week, "total": total, "current_total": cur_total, "gain": gain,
           "starters": starter_ids, "previous": current, "changed": changed,
           "pinned": sorted(pinned), "applied": False, "illegal": bad,
           "holes": [SLOTS[i] for i, p in enumerate(lineup) if not p],
           "provenance": provenance, "modelled": modelled,
           "of_roster": len(cands)}

    if verbose:
        print(report(lineup, cands, total, pinned, provenance))
        print(f"  current lineup projects {cur_total}; "
              f"{'no change' if not changed else f'gain {gain:+.1f}'}")
        if bad:
            print("  currently starting: " + "; ".join(bad))

    if not apply:
        return out
    if not changed:
        if verbose:
            print("lineup already optimal; no write")
        return out
    if gain < MIN_GAIN_TO_CHANGE and current and not bad:
        # A reshuffle worth less than the threshold is churn: it publishes a
        # decision-log entry that says nothing and costs a real write.
        if verbose:
            print(f"change worth only {gain:+.1f} pts (< {MIN_GAIN_TO_CHANGE}); "
                  "leaving the lineup alone")
        return out

    from robo.decisions import record
    from robo.sleeper_write import set_starters
    set_starters(roster["roster_id"], week, starter_ids, league_id)
    out["applied"] = True
    engine = (f"the NFL Model's simulated means for {modelled} of {len(cands)} "
              f"players" if modelled else "Sleeper's weekly projections")
    why = (f"Projected {total} points under league scoring from {engine}, "
           f"{gain:+.1f} versus the lineup as it stood. Bye-week and "
           f"injured-out players are benched")
    if bad:
        why += ", which the previous lineup was not: it had " + ", ".join(bad)
    if pinned:
        why += (f"; {len(pinned)} slot(s) had already kicked off and were left "
                f"alone")
    record("lineup", f"Week {week} lineup set", describe(lineup, total), why + ".",
           data={"starters": starter_ids, "previous": current,
                 "projected": total, "week": week,
                 "source": provenance, "modelled": modelled})
    if verbose:
        print("applied.")
    return out


def compare(week: int | None = None, season_yr: str = season.SEASON,
            league_id: str = LEAGUE_ID_2026) -> dict:
    """Both engines, side by side, and whether they choose a different lineup.

    The point is the LAST line, not the table. Two projections can disagree
    about every player and still start the same ten, which is the case where
    swapping engines was free; the case worth looking at is the one where the
    lineup actually moves. Never writes.
    """
    week = week or season.current_week()
    players_map = api.players()
    roster = season.mine(league_id)
    reserve = set(roster.get("reserve") or [])
    active = [x for x in (roster.get("players") or []) if x not in reserve]
    cands, provenance = project_roster(active, season_yr, week, players_map,
                                       league_id)

    # The same candidates priced the old way. Everything else -- bye, lock,
    # injury -- is identical, so the only thing that can move the lineup is
    # the number.
    shadow = [{**c, "pts": c["sleeper_pts"], "pts_source": "sleeper"}
              for c in cands]

    model_lu, model_total = optimize(cands)
    sleeper_lu, sleeper_total = optimize(shadow)

    print(f"week {week} - {provenance or 'model not in use'}")
    print()
    print(f"  {'player':<24}{'pos':<5}{'sleeper':>9}{'model':>9}{'delta':>9}"
          f"   {'p10':>7}{'p90':>7}")
    print("  " + "-" * 74)
    for c in sorted(cands, key=lambda x: -x["pts"]):
        d = c["pts"] - c["sleeper_pts"]
        band = (f"{c['p10']:>7.1f}{c['p90']:>7.1f}"
                if c["p10"] is not None else f"{'-':>7}{'-':>7}")
        mark = "" if c["pts_source"] == "model" else "  (no model row)"
        print(f"  {c['name'][:24]:<24}{c['pos']:<5}{c['sleeper_pts']:>9.1f}"
              f"{c['pts']:>9.1f}{d:>+9.1f}   {band}{mark}")

    m_ids = [x["player_id"] if x else "0" for x in model_lu]
    s_ids = [x["player_id"] if x else "0" for x in sleeper_lu]
    print()
    print(f"  model lineup   {model_total:>7.1f} pts")
    print(f"  sleeper lineup {sleeper_total:>7.1f} pts   "
          f"(scored the old way: "
          f"{round(sum(x['sleeper_pts'] for x in sleeper_lu if x), 1)})")
    if m_ids == s_ids:
        print()
        print("  SAME TEN STARTERS in the same slots - the swap "
              "changes nothing this week.")
    else:
        print()
        print("  THE LINEUP MOVES:")
        for i, slot in enumerate(SLOTS):
            a, b = model_lu[i], sleeper_lu[i]
            if (a or {}).get("player_id") != (b or {}).get("player_id"):
                print(f"    {slot:<11} model starts {(a or {}).get('name', 'EMPTY')}"
                      f"  <-  sleeper starts {(b or {}).get('name', 'EMPTY')}")
    return {"week": week, "model": m_ids, "sleeper": s_ids,
            "same": m_ids == s_ids, "provenance": provenance}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="auto",
                    help="week number, or 'auto' for Sleeper's current week")
    ap.add_argument("--season", default=season.SEASON)
    ap.add_argument("--league", default=LEAGUE_ID_2026)
    ap.add_argument("--roster", type=int, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="both engines side by side; never writes")
    args = ap.parse_args()
    week = None if args.week == "auto" else int(args.week)
    if args.compare:
        compare(week=week, season_yr=args.season, league_id=args.league)
        return
    res = run(week=week, season_yr=args.season, league_id=args.league,
              apply=args.apply, roster_id=args.roster)
    if res["holes"]:
        print("!! unfilled slots: " + ", ".join(res["holes"]))


if __name__ == "__main__":
    main()
