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
# 2: each player carries his per-week rate series and the source of each week.
# Schema 1 kept only the count, which forced explain() to recompute the whole
# spine to print it -- narrating a fresh calculation rather than the cached one
# the bot actually acted on.
SCHEMA = 2

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

# Column the per-line source tags start at in a trace. Every printed value says
# which file it came from, because the point of a trace is to send you to the
# right file when a number looks wrong -- a trailing block that lists the files
# read tells you nothing about which of them produced the line you are
# squinting at.
SRC_COL = 62

SOURCES = {
    "sleeper": "Sleeper's weekly projection feed, scored under this league",
    "model": "data/model_week.json  (the NFL Model's simulated week)",
    "vegas": r"C:\NFL Model\data\parquet\schedules.parquet  (posted lines)",
    "fallback": "data/board_2026.csv  season rate over the games left",
    "roles": r"C:\NFL Model\data\parquet\player_stats_*.parquet  (usage)",
    "roles fit": "data/roles_fit.json  (1,242 measured vacancies)",
    "odds": "data/playoff_odds.json",
    "scout": "data/news_verdicts.json",
    "cached": "data/ros.json  -- the number the bot actually used",
    "league": "the league's own settings, read from Sleeper",
    "derived": "computed here from the lines above",
}

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


def week_weights(week: int, league_id: str = LEAGUE_ID_2026,
                 record: dict | None = None) -> dict:
    """w -> the weight that week's points carry.

    `record` separates the two factors a playoff week's weight is made of --
    our odds of being there, and the round discount -- and says whether the odds
    are REAL. p_playoffs falls back to 1.0 when it cannot compute, which is
    indistinguishable in the output from a genuine 100% and quietly sets the
    whole playoff horizon. That distinction only exists here.
    """
    sh = playoffs.shape(league_id)
    odds = playoffs.load(league_id=league_id)
    p = playoffs.p_playoffs(league_id=league_id, default=1.0)
    out = {}
    for w in range(week, last_week(league_id) + 1):
        if w <= sh["last_regular_week"]:
            out[w] = 1.0
        else:
            rnd = w - sh["playoff_week_start"]
            out[w] = round(p * PLAYOFF_WEIGHTS.get(rnd, 0.0), 4)
    if record is not None:
        record.update({
            "shape": sh, "p_playoffs": p,
            # False means the odds file was missing or unreadable and every
            # playoff week is being weighted as a certainty by default.
            "odds_are_real": bool(odds),
            "odds_computed": (odds or {}).get("computed"),
            "odds_source": str(playoffs.CACHE),
            "first_week": week, "last_week": last_week(league_id),
            "rounds": {w: (w - sh["playoff_week_start"]) for w in out
                       if w > sh["last_regular_week"]},
            "round_factor": {w: PLAYOFF_WEIGHTS.get(w - sh["playoff_week_start"], 0.0)
                             for w in out if w > sh["last_regular_week"]},
            "weights": dict(out)})
    return out


# --------------------------------------------------------------------- rates

def _def_rate(team: str, week: int, season_yr, fallback: float,
              record: dict | None = None) -> tuple[float, str]:
    """A defence's week, from the market where it is priced."""
    tm = vegas.team_code(team or "")
    lines = {} if record is None else {}
    imp = vegas.implied_totals(int(season_yr), week,
                               record=lines if record is not None else None).get(tm)
    if imp:
        curve = {} if record is None else {}
        e = streaming.expected("DEF", imp["opp"],
                               record=curve if record is not None else None)
        if e is not None:
            if record is not None:
                record.update({"mode": "vegas", "team": tm, "implied": imp,
                               "line": (lines.get("lines") or {}).get(tm),
                               "curve": curve, "pts": e, "fallback": fallback})
            return e, "vegas"
    if record is not None:
        record.update({"mode": "fallback", "team": tm, "implied": imp,
                       "pts": fallback,
                       "why": "no posted line for this week; using his own season "
                              "rate over the games left, which knows no matchup"})
    return fallback, "fallback"


def weekly_rates(week: int, season_yr=None, league_id: str = LEAGUE_ID_2026,
                 record: dict | None = None) -> dict:
    """{pid: {w: points}} for every remaining week, under our exact scoring.

    Absent from a week means NO GAME that week, which is not the same as zero
    and must not be collapsed into it -- a bye is one week missing from a sum,
    while a zero would say he played and scored nothing.

    `detail` is always built and carries, per player-week, the number BEFORE the
    22-key correction and the correction separately, the source, and the
    opponent. Summing those on one line and keeping only the total is what made
    "why is this 18.4" unanswerable without re-running the whole spine. It costs
    one small dict per player-week, and build() persists a trimmed copy so a
    trace reads the numbers the bot used rather than recomputing them.
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

    out, sources, detail = {}, {}, {}
    weeks = list(range(week, last_week(league_id) + 1))
    for w in weeks:
        wp = season.week_points(w, yr, league_id)
        raw = {r["player_id"]: (r.get("stats") or {}) for r in season.weekly_raw(w, yr)}
        for pid, v in wp.items():
            if not v.get("has_game"):
                continue
            p = players.get(pid) or {}
            pos = p.get("position") or "DEF"
            d = {"opp": v.get("opponent")}
            if pos == "DEF":
                dr: dict = {}
                pts, src = _def_rate(p.get("team") or pid, w, yr,
                                     board.get(pid, 0.0), record=dr)
                d["def"] = dr
            else:
                base = v["pts"]
                corr = rankings.missing_key_rate(pid, raw.get(pid, {}), sc, s25)
                pts, src = base + corr, "sleeper"
                d.update({"raw": round(base, 3), "corr": round(corr, 3)})
            d.update({"pts": round(pts, 3), "src": src})
            out.setdefault(pid, {})[w] = round(pts, 3)
            sources.setdefault(pid, []).append(src)
            detail.setdefault(pid, {})[w] = d

    # The current week is the one the NFL Model actually simulated, and it scores
    # all 57 keys off 4,000 stat lines rather than 35 keys off a point estimate.
    # Absence means keep Sleeper's number, never zero -- the rule lineup.py
    # documents, for the same reason.
    mp, prov = model_proj.week_projections(week, yr, league_id)
    for pid, m in mp.items():
        if pid in out and week in out[pid] and "mean" in m:
            d = detail[pid][week]
            # Keep what the model REPLACED. Overwriting destroyed the only copy
            # of the disagreement between the two engines for the one week they
            # both cover, which is exactly the comparison worth auditing.
            d["replaced"] = d["pts"]
            d.update({"pts": round(float(m["mean"]), 3), "src": "model",
                      "p10": m.get("p10"), "p90": m.get("p90")})
            out[pid][week] = d["pts"]
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
    if record is not None:
        record.update({"weeks": weeks, "provenance": prov,
                       "board_rows": len(board), "model_players": len(mp),
                       "scoring_keys": len(sc)})
    return {"rates": out, "sources": label, "detail": detail,
            "provenance": prov, "weeks": weeks}


# -------------------------------------------------------------------- upside

def upside_of(pid: str, pos: str, team: str, week: int, rates: dict,
              weights: dict, season_yr=None,
              record: dict | None = None) -> tuple[float, str]:
    """Expected points from inheriting the job ahead of him, over the weeks left.

        upside = SUM over w of  W(w) x P(the man ahead misses) x absorbs x his rate

    Every factor is measured: the miss rate and the absorption curve come from
    1,242 real vacancies in roles.py, and the man ahead is whoever actually has
    the touches rather than whoever a depth chart lists.
    """
    yr = int(season_yr or season.SEASON)
    rr: dict = {}
    r = roles.role(pid, team, pos, yr, week, record=rr)
    if record is not None:
        record.update({"role": r, "roles": rr})
    if not r["ahead_id"] or r["absorbs"] <= 0:
        why = r["why"] or "nothing to inherit"
        if record is not None:
            record["outcome"] = why
        return 0.0, why
    ahead = rates.get(r["ahead_id"]) or {}
    if not ahead:
        why = f"behind {r['ahead_of']}, who has no projection to inherit"
        if record is not None:
            record["outcome"] = why
        return 0.0, why
    miss = roles.miss_rate(pos)
    absorb = min(MAX_ABSORB, r["absorbs"])
    per_week = {w: weights.get(w, 0.0) * miss * absorb * pts
                for w, pts in ahead.items()}
    total = sum(per_week.values())
    if record is not None:
        record.update({"ahead_rates": dict(ahead), "per_week": per_week,
                       "miss_rate": miss, "absorbs_raw": r["absorbs"],
                       "absorbs": absorb, "clamped": absorb != r["absorbs"],
                       "pre_weight": round(total, 3),
                       "upside_weight": UPSIDE_WEIGHT, "outcome": "inherits"})
    return (round(UPSIDE_WEIGHT * total, 2),
            f"rank {r['rank']} behind {r['ahead_of']}; absorbs {absorb:.0%} "
            f"at {miss:.1%}/wk [{r['tier']}]")


# --------------------------------------------------------------------- build

def _persistable(d: dict) -> dict:
    """One player-week, trimmed to what is worth keeping on disk.

    WHAT IS PERSISTED IS WHAT IS EXPENSIVE TO GET BACK. The Sleeper spine costs
    a network fetch per remaining week, so it is kept. A defence's betting line
    and the bucket it interpolated between cost an lru_cached parquet lookup, so
    they are recomputed on demand instead -- keeping them made every defence
    7.5KB against a skill player's 1.5KB, five times the size for the cheapest
    thing in the file. The live recorder still carries the full detail; only the
    cache is trimmed.
    """
    out = {k: v for k, v in d.items() if k != "def"}
    dr = d.get("def")
    if dr:
        out["mode"] = dr.get("mode")
        imp = dr.get("implied") or {}
        out["opp_implied"] = imp.get("opp")
    return out


def build(week: int | None = None, league_id: str = LEAGUE_ID_2026,
          with_upside: bool = True) -> dict:
    """The rest-of-season table. {pid: {mean, upside, hold, ...}}."""
    from robo import scout
    wk = week or season.current_week()
    weights = week_weights(wk, league_id)
    wr = weekly_rates(wk, season.SEASON, league_id)
    rates, sources, detail = wr["rates"], wr["sources"], wr["detail"]
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
        raw_mult = 1.0
        if verdicts and NEWS_APPLY_FUTURE:
            try:
                raw_mult = scout.trust_multiplier(pid)
            except Exception:
                raw_mult = 1.0
            # Rounded here, not at display time. Storing a rounded multiplier
            # while multiplying by the full-precision one is the same defect as
            # the terms above: the published factor no longer reproduces the
            # published total.
            mult = round(1.0 + (raw_mult - 1.0) * NEWS_APPLY_FUTURE, 3)
        # The two terms are kept apart because only one of them is touched by
        # news. Reported as one number, "the multiplier is 1.13" cannot be
        # reconciled against the total by anyone reading it.
        # ROUNDED FIRST, THEN COMBINED, so the arithmetic a trace prints adds up
        # by hand. Combining full precision and rounding once left the published
        # terms disagreeing with the published total by up to 0.07 on 27 of 908
        # players -- harmless as a number and fatal as an audit, because the one
        # check meant to build confidence would have reported a mismatch on a
        # perfectly good row.
        now_term = round(weights.get(wk, 1.0) * now_pts, 2)
        future_term = round(sum(weights.get(w, 0.0) * pts
                                for w, pts in byweek.items() if w != wk), 2)
        mean = now_term + mult * future_term
        up, why = (0.0, "")
        if with_upside and pos in roles.OPPORTUNITY:
            up, why = upside_of(pid, pos, p.get("team") or "", wk, rates,
                                weights, season.SEASON)
        det = detail.get(pid) or {}
        rows[pid] = {"player_id": pid, "name": api.player_name(players, pid),
                     "pos": pos, "team": p.get("team"),
                     "mean": round(mean, 2), "upside": round(up, 2),
                     "hold": round(mean + up, 2),
                     "weeks": len(byweek), "now": round(now_pts, 2),
                     "now_term": round(now_term, 2),
                     "future_term": round(future_term, 2),
                     "news_mult": round(mult, 3),
                     "news_raw": round(raw_mult, 3),
                     "source": sources.get(pid, "?"), "upside_why": why,
                     # The spine, persisted. A trace reads this rather than
                     # rebuilding it, so it narrates the numbers that were
                     # actually used instead of whatever Sleeper says now.
                     "by_week": {str(w): _persistable(det.get(w, {}))
                                 for w in sorted(byweek)}}
    return {"schema": SCHEMA, "computed": time.time(), "week": wk,
            "season": season.SEASON, "weights": weights,
            "upside_included": bool(with_upside),
            "news_apply_future": NEWS_APPLY_FUTURE,
            "upside_weight": UPSIDE_WEIGHT,
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

def find(name: str, table: dict | None = None) -> list[str]:
    """Player ids matching a name, the ones we can actually price first.

    Matched through api.player_name, not `full_name`: Sleeper's dump gives a
    team defence no full_name at all, so a full_name search cannot find one --
    and a defence is exactly the row worth explaining, since it is the only one
    priced off the betting market rather than the projection feed.

    NAMES ARE NOT UNIQUE and the collisions are not exotic. "Josh Allen" is a
    quarterback and a linebacker; sorting by name length alone picked the
    linebacker and reported "no remaining games" underneath a full table for the
    quarterback. Anyone in the rest-of-season table sorts ahead of anyone who is
    not, and callers that already know the id should pass it instead of a name.
    """
    players = api.players()
    q = (name or "").strip().lower()
    if not q:
        return []
    rows = (table or {}).get("players") or {}
    hits = [pid for pid in players if q in api.player_name(players, pid).lower()]
    hits.sort(key=lambda p: (p not in rows, len(api.player_name(players, p))))
    return hits


def weight_of(d: dict, w: int) -> float:
    """A week's weight out of a build, from either an int or a string key.

    `week_weights()` returns int keys and JSON hands them back as strings, so
    every reader has to accept both. It is a small thing that breaks silently:
    the miss returns 0.0 and a whole week quietly stops counting.
    """
    wts = d.get("weights") or {}
    v = wts.get(str(w))
    return float(v if v is not None else wts.get(w, 0.0))


def _line(text: str, src: str = "") -> None:
    """One data line and where it came from."""
    if not src:
        print(f"    {text}")
        return
    print(f"    {text:<{SRC_COL}} <- {src}")


def explain(name: str = "", league_id: str = LEAGUE_ID_2026, limit: int = 1,
            player_id: str | None = None, reasons: bool = False) -> None:
    """Walk one player's rest-of-season number forward, stage by stage.

    `reasons` OFF BY DEFAULT, and that default is the safe one. A scout reason
    quotes the reporting it was formed from -- injuries, and in one case a named
    player's criminal charge -- and this walk is reachable from skills.py, which
    answers in the league chat. The magnitude of a verdict is publishable and
    always shown; the sentence behind it is for the local audit app, which
    passes reasons=True and is the same local/published split status.py draws
    between report() and _scrub().

    READ IN THE ORDER THE MODEL RUNS. A season-long total is hard to sanity
    check by eye -- a stale projection or a dead playoff weighting still
    produces something shaped like a number, and reading the answer backwards
    makes it easy to nod along. Forwards, every step has to make sense before
    the next one arrives.

    THE NUMBERS COME FROM THE CACHE, not from a fresh computation. ros.json is
    what the bot actually acted on; recomputing here would narrate a different
    calculation any time Sleeper repriced between the daily build and the audit,
    and the reader would have no way to tell. Stage [4] cross-checks the two.
    """
    players = api.players()
    d = load(league_id=league_id)
    # An id from a caller that already resolved one beats re-resolving a name
    # it may not resolve the same way.
    hits = [str(player_id)] if player_id else find(name, d)
    if not hits:
        print(f"no player matching {name!r}")
        return
    wk, sh = d["week"], playoffs.shape(league_id)
    for pid in hits[:max(1, limit)]:
        row = (d.get("players") or {}).get(pid)
        if not row:
            print(f"{api.player_name(players, pid)}: no remaining games")
            continue
        _explain_one(pid, row, d, wk, sh, league_id, players, reasons=reasons)


def _explain_one(pid, row, d, wk, sh, league_id, players, reasons=False) -> None:
    by = row.get("by_week") or {}
    weeks = sorted(int(w) for w in by)
    horizon = list(range(wk, last_week(league_id) + 1))
    byes = [w for w in horizon if w not in weeks]
    used = set()

    print(f"{row['name']}  ({row['pos']} {row['team'] or '-'})   "
          f"mean {row['mean']}   upside {row['upside']}   hold {row['hold']}")
    print(f"  as of week {wk} of {d['season']}, "
          f"cached {time.strftime('%a %d %b %H:%M', time.localtime(d['computed']))}")

    print()
    print("[1] THE HORIZON -- how many weeks are left, and what each is worth")
    _line(f"weeks {horizon[0]}-{horizon[-1]}, {len(weeks)} with a game"
          + (f", bye week {', '.join(map(str, byes))}" if byes else ""), "derived")
    _line(f"regular season runs through week {sh['last_regular_week']}, "
          f"every week weighted 1.00", "league")
    p = playoffs.p_playoffs(league_id=league_id, default=1.0)
    odds = playoffs.load(league_id=league_id)
    if odds:
        _line(f"playoff odds {p:.1%}  ({sh['teams']} of 12 qualify)", "odds")
        used.add("odds")
    else:
        _line(f"playoff odds UNAVAILABLE -- every playoff week defaults to 1.00, "
              f"which is a flat horizon and not a 100% claim", "derived")
    for w in horizon:
        if w <= sh["last_regular_week"]:
            continue
        rnd = w - sh["playoff_week_start"]
        f = PLAYOFF_WEIGHTS.get(rnd, 0.0)
        _line(f"week {w}  weight {weight_of(d, w):.3f} = {p:.3f} odds "
              f"x {f:.2f} round-{rnd + 1} factor", "derived")

    print()
    print("[2] EACH WEEK -- the rate, and where it came from")
    _line(f"{'wk':<4}{'rate':>7}{'weight':>8}{'gives':>8}  {'source':<9} opponent")
    for w in weeks:
        c = by[str(w)]
        wt = weight_of(d, w)
        src = c.get("src", "?")
        used.add(src)
        _line(f"{w:<4}{c.get('pts', 0):>7.2f}{wt:>8.2f}"
              f"{c.get('pts', 0) * wt:>8.2f}  {src:<9} {c.get('opp') or '-'}")
        if c.get("raw") is not None and c.get("corr"):
            _line(f"      {c['raw']:.2f} scored from the feed, "
                  f"{c['corr']:+.2f} for the 22 keys it omits", "derived")
        if c.get("replaced") is not None:
            _line(f"      the model's {c['pts']:.2f} replaced Sleeper's "
                  f"{c['replaced']:.2f} for this week"
                  + (f"  (p10 {c['p10']} - p90 {c['p90']})"
                     if c.get("p10") is not None else ""), "model")
        if c.get("opp_implied") is not None:
            _line(f"      opponent implied total {c['opp_implied']:.1f}", "vegas")
    for w in byes:
        _line(f"{w:<4}{'--':>7}  no game; absent from the sum, never a zero", "derived")

    print()
    print("[3] NEWS -- applied once, to the future block only")
    if row.get("news_raw", 1.0) == 1.0:
        _line("no verdict on file, or none that moves him; multiplier 1.000", "scout")
    else:
        v = _verdict(pid)
        if v:
            _line(f"verdict {v.get('verdict')} at {float(v.get('confidence', 0)):.0%} "
                  f"confidence", "scout")
            if reasons:
                _line(f"reason: {v.get('reason', '')}", "scout")
        _line(f"raw multiplier {row['news_raw']:.3f}, applied at "
              f"NEWS_APPLY_FUTURE={d.get('news_apply_future', NEWS_APPLY_FUTURE)} "
              f"-> {row['news_mult']:.3f}", "derived")
        used.add("scout")
    _line("never applied to the current week, where the model and the live feed "
          "have already priced the news", "derived")

    print()
    print("[4] THE VALUE -- what he is worth to us")
    _line(f"this week    {row['now']:>8.2f} x {weight_of(d, wk):.2f} "
          f"= {row['now_term']:>8.2f}", "derived")
    _line(f"future weeks {row['future_term']:>8.2f} x {row['news_mult']:.3f} "
          f"= {row['future_term'] * row['news_mult']:>8.2f}", "derived")
    _line(f"mean         {row['mean']:>8.2f}", "derived")
    check = round(row["now_term"] + row["future_term"] * row["news_mult"], 2)
    recomputed = round(sum(by[str(w)]["pts"] * weight_of(d, w) for w in weeks), 2)
    ok = abs(check - row["mean"]) < 0.02
    _line(f"cross-check: terms sum to {check:.2f} against the cached "
          f"{row['mean']:.2f}  [{'OK' if ok else 'MISMATCH'}]", "cached")
    _line(f"unweighted-by-news sum of the table above is {recomputed:.2f}", "derived")

    print()
    print("[5] INHERITANCE -- what he stands to pick up if the man ahead goes down")
    rec: dict = {}
    if row["pos"] in roles.OPPORTUNITY:
        rates = {p2: {int(w2): c2["pts"] for w2, c2 in (r2.get("by_week") or {}).items()}
                 for p2, r2 in (d.get("players") or {}).items()}
        weights = {w: weight_of(d, w) for w in horizon}
        upside_of(pid, row["pos"], row["team"] or "", wk, rates, weights,
                  d["season"], record=rec)
    _inheritance(row, rec, used)

    print()
    print("[6] THE ANSWER")
    _line(f"hold = mean {row['mean']:.2f} + upside {row['upside']:.2f} "
          f"= {row['hold']:.2f}", "derived")
    _line("an ADD is judged on `mean` -- what he is worth to us", "derived")
    _line("a DROP is judged on `hold` -- what it costs to lose him", "derived")

    _sources(used)
    print()


def _verdict(pid: str) -> dict:
    from robo import scout
    try:
        # The verdicts live one level down, under "verdicts" -- indexing the
        # file dict by player id returned nothing for every player ever traced,
        # so the news line has been silently blank since it was written.
        return ((scout.load_verdicts() or {}).get("verdicts") or {}).get(str(pid)) or {}
    except Exception:
        return {}


def _inheritance(row: dict, rec: dict, used: set) -> None:
    if not rec:
        _line(f"{row['pos']} has no opportunity model; nothing to inherit", "derived")
        return
    r = rec.get("role") or {}
    rr = rec.get("roles") or {}
    if rr.get("tried"):
        for t in rr["tried"]:
            _line(f"tried {t['tier']} ({t['season']}): "
                  + ("found him" if t["found"] else t["why"]), "roles")
        used.add("roles")
    room = rr.get("room") or []
    if room:
        _line(f"the {r.get('team')} {r.get('pos')} room, "
              f"{rr.get('room_season')} usage:", "roles")
        for m in room[:6]:
            mark = "*" if rr.get("me") and m["gsis"] == rr["me"]["gsis"] else " "
            _line(f"    {mark} {m['rank']}. {m['name'][:24]:<24} "
                  f"{m['prior']:.1%} of the position's work", "roles")
        # These do not sum to 100% and should not: each man's share is read from
        # HIS OWN most recent week, not from one shared week, so a man who
        # missed the finale is still in the room at the share he last held.
        # Taking one common week instead drops exactly the players a roster
        # question is about -- see roles._rooms.
        _line("  * him.  shares are each man's own latest week, so they do not "
              "sum to 100%", "derived")
    if rec.get("outcome") != "inherits":
        _line(f"no upside term: {rec.get('outcome', 'nothing to inherit')}", "derived")
        return
    cell = rr.get("cell") or {}
    _line(f"rank {r.get('rank')} behind {r.get('ahead_of')} [{r.get('tier')}]", "roles")
    _line(f"absorbs {rec['absorbs']:.1%} of the vacated role"
          + (f"  (n={cell.get('n')}, sd {cell.get('sd')})" if cell else "")
          + ("  CLAMPED" if rec.get("clamped") else ""), "roles fit")
    _line(f"an established {r.get('pos')} misses {rec['miss_rate']:.2%} of weeks",
          "roles fit")
    used.add("roles fit")
    top = sorted(rec.get("per_week", {}).items())[:4]
    for w, v in top:
        _line(f"      week {w}: {rec['ahead_rates'].get(w, 0):.2f} his "
              f"x {rec['miss_rate']:.3f} x {rec['absorbs']:.3f} "
              f"x weight = {v:.3f}", "derived")
    if len(rec.get("per_week", {})) > 4:
        _line(f"      ... {len(rec['per_week']) - 4} more weeks", "derived")
    _line(f"summed {rec['pre_weight']:.2f}, x UPSIDE_WEIGHT "
          f"{rec['upside_weight']} = {row['upside']:.2f}", "derived")


def _sources(used: set) -> None:
    """Only the tags this player's branch actually produced.

    Listing every source for every player would have a skill player claiming a
    betting line it never opened. A source list that names things it did not use
    is worse than none: it sends you to the wrong file when a number looks wrong.
    """
    print()
    print("[SOURCES] what each tag above resolves to")
    for tag in [t for t in SOURCES if t in used] + ["cached", "league", "derived"]:
        print(f"    {tag:<14}{SOURCES[tag]}")


def trace(name: str = "", league_id: str = LEAGUE_ID_2026,
          player_id: str | None = None, reasons: bool = False) -> str:
    """The same walkthrough the CLI prints, returned as text.

    A five-line adapter over the print-based walk rather than a second renderer:
    the CLI is the thing that gets exercised, so there is no web version to
    drift away from it.
    """
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        explain(name, league_id, player_id=player_id, reasons=reasons)
    return buf.getvalue()


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
    ap.add_argument("--limit", type=int, default=1,
                    help="how many name matches to walk (default 1)")
    args = ap.parse_args()
    if args.refresh:
        load(refresh=True)
    if args.explain:
        explain(args.explain, limit=args.limit)
        return
    print(report(args.top, args.pos, args.mine))


if __name__ == "__main__":
    main()
