"""What a player is worth from here to the end -- the number moves.py was waiting on.

    value(pid, week) = SUM over w in [week..last] of  W(w) x rate(pid, w) x plays(w)

THE SPINE IS SLEEPER'S OWN WEEKLY FEED, and it took measuring to justify. Its
future weeks are FLAT: Josh Allen reads 23.1 / 22.8 / 23.0 for weeks 1 / 5 / 14,
because the number is a per-game rate wearing a bye mask, with no matchup in it
at all. What makes it the right spine anyway is the other axis -- it REPRICES.
Six captures of week 1 taken over two and a half days disagree on 22 of 460
players, and the movers move by four and five points, which is a backup being
handed a job. The preseason board cannot do that at any price; it was frozen in
August and has not heard a thing since.

That repricing is proven for the CURRENT week and assumed for the rest, which is
the one soft spot in here. robo/projarchive.py is collecting the evidence to
settle it; NEWS_APPLY_FUTURE is the dial that changes when it does.

WHAT W(w) IS, AND WHY IT IS NOT A DISCOUNT. The usual move is to decay the
future, which would be wrong twice over: it fights the rookie-hold rule this
module exists to enforce, and it has no idea what we are playing for. This
league takes 6 of 12 and starts its playoffs in week 15, so the regular season is
worth full freight and weeks 15-17 are worth exactly our chance of being there.
At 6-2 a week-16 point is as good as a week-9 point. At 2-6 the season really
does end in week 14 and the bot should be renting whatever wins now. Odds come
from robo/playoffs.py, and if they cannot be computed the weight falls back to
1.0 -- a flat horizon, never a zeroed future.

DEFENCES DO NOT RIDE THE SPINE. Every one of the six pts_allow tiers is among
the 22 scoring keys the weekly feed omits, so a defence scored off it comes back
near zero -- and the feed has no matchup, which for a defence is the whole
question. They are priced from the betting market instead, through
robo/streaming.py. See that module for why kickers are NOT given the same
treatment.

TWO NUMBERS COME OUT, AND THE ASYMMETRY IS THE POINT.

    mean   what he is worth to us            -> used to decide an ADD
    hold   mean + upside                     -> used to decide a DROP

`upside` is the rising-role term: how much he stands to inherit if the man ahead
of him goes down, priced off robo/roles.py's fitted absorption curve and its
measured per-week miss rate. A rookie backup is not worth more THIS week -- his
mean says so honestly -- he is simply more expensive to lose. Pricing adds and
drops off the same number is what makes a bot cut the rookie in October and
watch somebody else start him in December.

    python -m robo.ros --top 40
    python -m robo.ros --explain "Player Name"
    python -m robo.ros --mine
"""

import argparse
import json
import time

from robo import DATA, LEAGUE_ID_2026, model_proj, playoffs, rankings, roles
from robo import season, settings, streaming, vegas
from robo import sleeper_read as api

CACHE = DATA / "ros.json"
SCHEMA = 1

# What each playoff week is worth BEFORE multiplying by our odds of being there,
# keyed by rounds past the start. Round 1 is worth a full week; later rounds are
# discounted by roughly the chance of still playing in them, with 6 of 12
# qualifying and two first-round byes. Stated assumptions, not fitted -- and a
# uniform factor across all players, so it moves the scale rather than the order.
PLAYOFF_WEIGHTS = {0: 1.0, 1: 0.6, 2: 0.35}

# How much of a scout news verdict to apply to the FUTURE weeks. Halved on
# purpose while it is unknown whether Sleeper already reprices them: at 1.0 a
# repriced feed would count the same injury twice, at 0.0 a stale one would
# carry no news at all, and 0.5 is the least-wrong place to stand until
# `python -m robo.projarchive --diff` says which world we are in. Never applied
# to the current week, where the model and the live feed have already seen it.
NEWS_APPLY_FUTURE = 0.5

# How hard the rising-role term pulls. This is the rookie-hold dial: at 0 the
# bot drops a breakout-in-waiting for any established veteran, at 2 it hoards
# lottery tickets it will never start.
UPSIDE_WEIGHT = 1.0

# A man cannot inherit more than the whole job.
MAX_ABSORB = 1.0

# Cached values older than this are rebuilt. Sleeper reprices daily, so a
# rest-of-season number from two days ago is a stale one.
MAX_AGE_H = 20.0

settings.apply(__name__, globals())


# ------------------------------------------------------------------- weights

def last_week(league_id: str = LEAGUE_ID_2026) -> int:
    """The last week this league actually plays.

    Derived from the league's own playoff start plus the rounds it runs, NOT
    from season.SEASON_WEEKS. The NFL plays 18 weeks; this league's final is
    week 17, and summing an eighteenth week of projections into a valuation
    credits every player with a game nobody in the league will ever start him
    for.
    """
    sh = playoffs.shape(league_id)
    return min(season.SEASON_WEEKS,
               sh["playoff_week_start"] + max(PLAYOFF_WEIGHTS) )


def week_weights(week: int, league_id: str = LEAGUE_ID_2026) -> dict:
    """w -> the weight that week's points carry, and why."""
    sh = playoffs.shape(league_id)
    p = playoffs.p_playoffs(league_id=league_id, default=1.0)
    out = {}
    for w in range(week, last_week(league_id) + 1):
        if w <= sh["last_regular_week"]:
            out[w] = 1.0
        else:
            rnd = w - sh["playoff_week_start"]
            out[w] = round(p * PLAYOFF_WEIGHTS.get(rnd, 0.0), 4)
    return out


# --------------------------------------------------------------------- rates

def _def_rate(team: str, week: int, season_yr, fallback: float) -> tuple[float, str]:
    """A defence's week, from the market where it is priced."""
    imp = vegas.implied_totals(int(season_yr), week).get(vegas.team_code(team or ""))
    if imp:
        e = streaming.expected("DEF", imp["opp"])
        if e is not None:
            return e, "vegas"
    return fallback, "fallback"


def weekly_rates(week: int, season_yr=None, league_id: str = LEAGUE_ID_2026) -> dict:
    """{pid: {w: points}} for every remaining week, under our exact scoring.

    Absent from a week means NO GAME that week, which is not the same as zero
    and must not be collapsed into it -- a bye is one week missing from a sum,
    while a zero would say he played and scored nothing.
    """
    yr = season_yr or season.SEASON
    sc = season.scoring(league_id)
    s25 = rankings.stats_2025()
    players = api.players()
    # A defence with no posted line falls back to its own season rate over the
    # games left, which is a rate rather than a matchup and is labelled as such.
    board = {}
    try:
        import csv
        with open(DATA / "board_2026.csv", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                board[r["player_id"]] = float(r.get("proj_pts") or 0) / 17.0
    except Exception:
        pass

    out, sources = {}, {}
    for w in range(week, last_week(league_id) + 1):
        wp = season.week_points(w, yr, league_id)
        raw = {r["player_id"]: (r.get("stats") or {}) for r in season.weekly_raw(w, yr)}
        for pid, v in wp.items():
            if not v.get("has_game"):
                continue
            p = players.get(pid) or {}
            pos = p.get("position") or "DEF"
            if pos == "DEF":
                pts, src = _def_rate(p.get("team") or pid, w, yr, board.get(pid, 0.0))
            else:
                pts = v["pts"] + rankings.missing_key_rate(pid, raw.get(pid, {}), sc, s25)
                src = "sleeper"
            out.setdefault(pid, {})[w] = round(pts, 3)
            sources.setdefault(pid, []).append(src)

    # The current week is the one the NFL Model actually simulated, and it scores
    # all 57 keys off 4,000 stat lines rather than 35 keys off a point estimate.
    # Absence means keep Sleeper's number, never zero -- the rule lineup.py
    # documents, for the same reason.
    mp, prov = model_proj.week_projections(week, yr, league_id)
    for pid, m in mp.items():
        if pid in out and week in out[pid] and "mean" in m:
            out[pid][week] = round(float(m["mean"]), 3)
            sources.setdefault(pid, [])
            sources[pid] = ["model"] + sources[pid][1:]

    # One label per player describing the WHOLE series. Recording only the first
    # week's source said "vegas" for a defence that is market-priced in week 1
    # and on a season-average fallback from week 7 -- which is the opposite of
    # the disclosure a fallback is supposed to carry.
    label = {}
    for pid, series in sources.items():
        counts = {s: series.count(s) for s in set(series)}
        label[pid] = " + ".join(f"{k}x{v}" for k, v in
                                sorted(counts.items(), key=lambda kv: -kv[1]))
    return {"rates": out, "sources": label, "provenance": prov}


# -------------------------------------------------------------------- upside

def upside_of(pid: str, pos: str, team: str, week: int, rates: dict,
              weights: dict, season_yr=None) -> tuple[float, str]:
    """Expected points from inheriting the job ahead of him, over the weeks left.

        upside = SUM over w of  W(w) x P(the man ahead misses) x absorbs x his rate

    Every factor is measured: the miss rate and the absorption curve come from
    1,242 real vacancies in roles.py, and the man ahead is whoever actually has
    the touches rather than whoever a depth chart lists.
    """
    yr = int(season_yr or season.SEASON)
    r = roles.role(pid, team, pos, yr, week)
    if not r["ahead_id"] or r["absorbs"] <= 0:
        return 0.0, r["why"] or "nothing to inherit"
    ahead = rates.get(r["ahead_id"]) or {}
    if not ahead:
        return 0.0, f"behind {r['ahead_of']}, who has no projection to inherit"
    miss = roles.miss_rate(pos)
    absorb = min(MAX_ABSORB, r["absorbs"])
    total = sum(weights.get(w, 0.0) * miss * absorb * pts for w, pts in ahead.items())
    return (round(UPSIDE_WEIGHT * total, 2),
            f"rank {r['rank']} behind {r['ahead_of']}; absorbs {absorb:.0%} "
            f"at {miss:.1%}/wk [{r['tier']}]")


# --------------------------------------------------------------------- build

def build(week: int | None = None, league_id: str = LEAGUE_ID_2026,
          with_upside: bool = True) -> dict:
    """The rest-of-season table. {pid: {mean, upside, hold, ...}}."""
    from robo import scout
    wk = week or season.current_week()
    weights = week_weights(wk, league_id)
    wr = weekly_rates(wk, season.SEASON, league_id)
    rates, sources = wr["rates"], wr["sources"]
    players = api.players()

    try:
        verdicts = scout.load_verdicts()
    except Exception:
        verdicts = {}

    rows = {}
    for pid, byweek in rates.items():
        p = players.get(pid) or {}
        pos = p.get("position") or "DEF"
        now_pts = byweek.get(wk, 0.0)
        # News touches the FUTURE only, once, at NEWS_APPLY_FUTURE strength --
        # never per week, where a 1.35 multiplier compounded over fourteen weeks
        # would turn a good report into a 40x valuation.
        mult = 1.0
        if verdicts and NEWS_APPLY_FUTURE:
            try:
                m = scout.trust_multiplier(pid)
            except Exception:
                m = 1.0
            mult = 1.0 + (m - 1.0) * NEWS_APPLY_FUTURE
        mean = weights.get(wk, 1.0) * now_pts
        mean += mult * sum(weights.get(w, 0.0) * pts
                           for w, pts in byweek.items() if w != wk)
        up, why = (0.0, "")
        if with_upside and pos in roles.OPPORTUNITY:
            up, why = upside_of(pid, pos, p.get("team") or "", wk, rates,
                                weights, season.SEASON)
        rows[pid] = {"player_id": pid, "name": api.player_name(players, pid),
                     "pos": pos, "team": p.get("team"),
                     "mean": round(mean, 2), "upside": round(up, 2),
                     "hold": round(mean + up, 2),
                     "weeks": len(byweek), "now": round(now_pts, 2),
                     "news_mult": round(mult, 3), "source": sources.get(pid, "?"),
                     "upside_why": why}
    return {"schema": SCHEMA, "computed": time.time(), "week": wk,
            "season": season.SEASON, "weights": weights,
            "provenance": wr["provenance"], "players": rows}


def load(refresh: bool = False, league_id: str = LEAGUE_ID_2026) -> dict:
    if not refresh and CACHE.exists():
        try:
            d = json.loads(CACHE.read_text(encoding="utf-8"))
            if (d.get("schema") == SCHEMA
                    and d.get("week") == season.current_week()
                    and (time.time() - d.get("computed", 0)) / 3600.0 < MAX_AGE_H):
                return d
        except Exception:
            pass
    d = build(league_id=league_id)
    try:
        CACHE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass
    return d


def value(pid: str, week: int | None = None, field: str = "mean") -> float:
    """One player's rest-of-season number. 0.0 for anyone with no games left."""
    row = (load().get("players") or {}).get(str(pid))
    return float(row.get(field, 0.0)) if row else 0.0


# ------------------------------------------------------------------- reports

def explain(name: str, league_id: str = LEAGUE_ID_2026) -> str:
    players = api.players()
    # Matched through api.player_name, not full_name: Sleeper's dump gives a
    # team defence no full_name at all, so a full_name search cannot find one --
    # and a defence is exactly the row worth explaining, since it is the only
    # one priced off the betting market rather than the projection feed.
    hits = [pid for pid in players
            if name.lower() in api.player_name(players, pid).lower()]
    if not hits:
        return f"no player matching {name!r}"
    d = load()
    wk = d["week"]
    wr = weekly_rates(wk, season.SEASON, league_id)
    L = []
    for pid in hits[:3]:
        row = (d["players"] or {}).get(pid)
        if not row:
            L.append(f"{api.player_name(players, pid)}: no remaining games")
            continue
        L.append(f"{row['name']}  ({row['pos']} {row['team']})   "
                 f"mean {row['mean']}   upside {row['upside']}   hold {row['hold']}")
        L.append(f"  {'wk':<5}{'rate':>8}{'weight':>9}{'contributes':>13}")
        byweek = wr["rates"].get(pid) or {}
        for w in sorted(byweek):
            wt = d["weights"].get(str(w), d["weights"].get(w, 0.0))
            L.append(f"  {w:<5}{byweek[w]:>8.2f}{wt:>9.2f}{byweek[w] * wt:>13.2f}")
        miss = [w for w in range(wk, last_week(league_id) + 1) if w not in byweek]
        if miss:
            L.append(f"  no game: weeks {', '.join(map(str, miss))}")
        L.append(f"  source: {row['source']}   news multiplier: {row['news_mult']}")
        if row["upside_why"]:
            L.append(f"  upside: {row['upside_why']}")
        L.append("")
    return "\n".join(L)


def report(top: int = 40, pos: str | None = None, mine: bool = False,
           league_id: str = LEAGUE_ID_2026) -> str:
    d = load()
    rows = list((d.get("players") or {}).values())
    if pos:
        rows = [r for r in rows if r["pos"] == pos.upper()]
    if mine:
        held = set(season.mine(league_id).get("players") or [])
        rows = [r for r in rows if r["player_id"] in held]
    rows.sort(key=lambda r: -r["hold"])
    p = playoffs.p_playoffs(league_id=league_id, default=1.0)
    L = [f"REST OF SEASON - from week {d['week']}, {d['season']}",
         f"  playoff odds {p:.1%}; weeks 15/16/17 weighted "
         f"{d['weights'].get('15', d['weights'].get(15, 0)):.2f} / "
         f"{d['weights'].get('16', d['weights'].get(16, 0)):.2f} / "
         f"{d['weights'].get('17', d['weights'].get(17, 0)):.2f}",
         f"  {d['provenance']}", "",
         f"  {'player':<24}{'pos':<5}{'tm':<5}{'mean':>9}{'upside':>8}{'hold':>9}  note"]
    for r in rows[:top]:
        note = r["upside_why"] if r["upside"] >= 1.0 else ""
        if r["source"] == "vegas":
            note = note or "market-priced"
        L.append(f"  {r['name'][:24]:<24}{r['pos']:<5}{(r['team'] or '-'):<5}"
                 f"{r['mean']:>9.1f}{r['upside']:>8.1f}{r['hold']:>9.1f}  {note[:44]}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--pos", default=None)
    ap.add_argument("--mine", action="store_true")
    ap.add_argument("--explain", default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    if args.refresh:
        load(refresh=True)
    if args.explain:
        print(explain(args.explain))
        return
    print(report(args.top, args.pos, args.mine))


if __name__ == "__main__":
    main()
