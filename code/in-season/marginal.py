"""What a roster move is worth, across the worlds where it matters.

WHAT WAS WRONG. moves.py scores a move as `add_value - drop_value`, two absolute
rest-of-season totals across different positions. A defence that starts all
seventeen weeks sums to about 118 and a fourth receiver sums to 45, so the wire
it shops from is almost entirely kickers and defences -- DEF 14, K 11, WR 3, TE 2
in the top thirty -- and the two moves it proposes today are mis-sized by an
order of magnitude and ranked in the wrong order.

THE FIRST FIX WAS ALSO WRONG, and the correction is the whole design here.
Pricing a move as the change in our optimal lineup kills the cross-position
nonsense, but run against a FULL-STRENGTH roster it prices three rookies drafted
as late lottery tickets at exactly zero to drop -- because Boston loses to
McLaurin, Collins and Egbuka, and Black loses to four healthy backs. The world in
which you need Boston is the world in which one of those three is out.

So the bench is not a portfolio INSTEAD of a lineup problem. It is a lineup
problem evaluated over the CONTINGENT worlds: the man who fills in for whoever
you lose in week 2, and the round-11 rookie who is an every-week starter by week
8 because he outplayed the men ahead of him. Both are lineup contributions.
Neither happens in the median world, which is the only world a point estimate
looks at.

HOW. Draw a season, optimise each week of it, repeat. robo/playoffs.py is the
precedent and proves the machinery -- it already simulates the remaining schedule
with lineup.optimize as team strength.

  * VACANCIES ARE DRAWN PER ROOM, not per player. When Christian McCaffrey's job
    opens it removes him AND promotes the men behind him in the same draw. That
    single choice is what makes insurance and lottery tickets the same mechanism
    instead of two terms that have to be weighted against each other: our own
    starter going down and our own backup's door opening are the same event seen
    from two sides.
  * COMMON RANDOM NUMBERS. The draws are made once and every hypothetical is
    scored against the identical worlds. We are measuring a DIFFERENCE, so shared
    noise cancels; without this the sim error would swamp the gaps we are trying
    to rank, and a few hundred draws would not be enough.
  * K AND DEF ARE NOT SIMULATED. Both slots are freely refillable from the wire
    every week -- streaming.expected() prices a real matchup swing for defences,
    while streaming.py refuses to rank kickers on measured grounds and the top
    ten span about a third of a point. So both contribute identically to every
    hypothetical and cancel out of every comparison. A second defence is worth
    zero by construction rather than by a rule.

WHAT COMES OUT. One number per candidate move, with the standard error beside
it, because a difference inside its own noise is not a ranking.

    python -m robo.marginal --roster        # what each man we hold is worth
    python -m robo.marginal --moves         # every live proposal, old vs new
"""

import argparse
import json
from functools import lru_cache

from robo import DATA, LEAGUE_ID_2026, lineup, playoffs, roles, ros, season
from robo import settings
from robo import sleeper_read as api

# Slots the simulation actually optimises. K and DEF are deliberately absent --
# see the header. Slicing SLOTS rather than restating them keeps this honest if
# the league ever changes shape.
SKILL_SLOTS = [s for s in lineup.SLOTS if s not in ("K", "DEF")]

# How many seasons to draw. With common random numbers this is measuring a
# difference between two rosters scored on identical worlds, which needs far
# fewer draws than an absolute total would -- the standard error is reported so
# the number can be checked rather than trusted.
SIMS = 200

# Playoff odds above which the bot optimises the MEAN. Below it, the objective
# moves to UPSIDE_PCTL: a long-shot needs variance, and the ticket that wins a
# league is the one that lifts the upper tail while leaving the mean flat. This
# is bench.py's INSURANCE_WEIGHT, expressed as an objective instead of a weight,
# and driven by the same odds ros.week_weights already uses.
CONTENDER_ODDS = 0.45
UPSIDE_PCTL = 75

# What counts as a bench player actually MATTERING in a given season -- the bar
# p_hit is measured against. NOT a guess: 7.0 is the median realised starting
# contribution of the 1,861 completed adds in this league's own history, so
# "matters" means "does at least what a typical add does". See calibrate().
#
# The shape behind that number is the real lesson and it belongs on the wall:
# 36% of adds in this league NEVER START A SINGLE GAME, the median returns 7
# points and the ninetieth percentile returns 62. Most adds do nothing. That is
# the normal outcome here, not a failure of one, and a bot that demands a high
# expected gain before it will move is refusing the only distribution on offer.
HIT_POINTS = 7.0

settings.apply(__name__, globals())


# ------------------------------------------------------------------- the data

def series(league_id: str = LEAGUE_ID_2026) -> dict:
    """player_id -> {pos, team, k, rank, room, weeks: {w: (s1, s2, avail)}}.

    Read from the cached artefacts rather than recomputed, the rule ros.explain
    already follows: a trace or a decision must narrate the numbers the bot
    acted on, not a fresh calculation that drifted since this morning.
    """
    ex = json.loads((DATA / "expected.json").read_text(encoding="utf-8"))
    out = {}
    for pid, r in (ex.get("players") or {}).items():
        wk = {}
        for w, d in (r.get("by_week") or {}).items():
            wk[int(w)] = (d.get("s1") or 0.0, d.get("s2") or 0.0,
                          d.get("a") or 0.0, d.get("final") or 0.0,
                          d.get("miss") or 0.0)
        out[pid] = {"name": r.get("name"), "pos": r.get("pos"), "team": r.get("team"),
                    # season-only rows carry no shape, so they are scaled at 1.0
                    # and priced on the flat series expected.py already wrote.
                    "k": r.get("k") or 1.0, "rank": r.get("rank"),
                    # A SEASON-ONLY ROW HAS NO SHAPE TO REBUILD FROM. expected.py
                    # spends the market's number flat across the games left when
                    # the structural model gives it nothing to scale, so s1 is
                    # near zero and only `final` carries the value. Reconstructing
                    # k*s1 for those 38 players priced them at nothing -- Michael
                    # Penix, a startable quarterback sitting unowned at 141, read
                    # as worthless and took the QB wire floor down with him.
                    "flat": r.get("k_source") == "season-only",
                    # The MEAN fraction expected.py folded into s2. Kept so the
                    # simulation can recover the lead's own number and redraw the
                    # fraction, instead of spending the average every week.
                    "absorbs": r.get("absorbs") or 0.0,
                    "room": (r.get("team"), r.get("pos")), "weeks": wk}
    return {"players": out, "week": ex["week"], "weights": {int(k): v for k, v in
                                                            ex["weights"].items()}}


def _rooms_of(ids: list[str], S: dict) -> set:
    return {S[p]["room"] for p in ids if p in S and all(S[p]["room"])}


def replacement(S: dict, weeks: list[int], league_id: str = LEAGUE_ID_2026) -> dict:
    """{pos: {week: points}} for the best man on the wire at that position.

    THE WIRE IS A FLOOR UNDER EVERY SLOT, and in a twelve-team league it is a
    high one. Measured on this roster in week 5: the best free-agent receiver is
    worth 6.3 points and our own WR3 is worth 6.3, the best free-agent back is
    worth 3.5 and our RB6 is worth 2.3. A spot starter is simply available.

    So a hurt starter does not cost us the drop to our own bench -- it costs the
    drop to whoever we claim on Tuesday, which is much less. Leaving the wire out
    of the simulation overstates every depth piece we hold and understates the
    case for spending a bench spot on a man who might become more than that.
    A bench body worth less than this line is worth exactly nothing, and the only
    thing that justifies the roster spot is a CEILING the wire cannot supply.

    One body per position per week, which is what a waiver claim actually buys.
    The pool is treated as fixed: our own adds and drops would move it slightly,
    and that second-order effect is not modelled.
    """
    held = season.rostered_ids(league_id)
    out: dict = {}
    for pid, p in S.items():
        if pid in held:
            continue
        for w in weeks:
            cell = p["weeks"].get(w)
            if cell is None:
                continue
            pts = (cell[3] / cell[2] if p.get("flat") and cell[2] > 0
                   else p["k"] * cell[0])
            cur = out.setdefault(p["pos"], {}).get(w, 0.0)
            if pts > cur:
                out[p["pos"]][w] = pts
    return out


def draws(ids: list[str], S: dict, weeks: list[int], sims: int, seed: int = 0):
    """The worlds. Fixed once and reused for every hypothetical.

    A ROOM VACANCY IS DRAWN PER WEEK, INDEPENDENTLY, because that is exactly what
    miss_rate measures -- "chance per week that an established starter at this
    position sits out", counted over weeks-a-role-was-held. Carrying a vacancy
    forward for the rest of the season instead compounds a 7% weekly rate into a
    71% chance the room is gone by week 17, and priced Carson Beck at 300 points
    to drop because he was the last quarterback standing in most worlds.

    EACH MAN IS DRAWN FOR ONCE, not twice. A player who holds his room's job IS
    the room's vacancy -- rolling separately for him as well would charge him for
    the same injury in two places. So rank 1 takes the room draw, everyone else
    takes his own, and a known absence overrides both.
    """
    import random
    rng = random.Random(seed)
    vac = {}
    for (tm, pos) in sorted(_rooms_of(ids, S)):
        rate = roles.miss_rate(pos)
        for w in weeks:
            for s in range(sims):
                vac[(tm, pos, w, s)] = rng.random() < rate
    avail, share = {}, {}
    for pid in ids:
        p = S.get(pid)
        if not p:
            continue
        rate = roles.miss_rate(p["pos"])
        lead = p.get("rank") == 1
        for w in weeks:
            a = (p["weeks"].get(w) or (0.0, 0.0, 0.0, 0.0, 0.0))[2]
            # A(w) below 1 is a KNOWN absence the injury feed already priced, so
            # the ordinary hazard must not be stacked on top of it.
            hit = a if a < 1.0 else (1.0 if lead else 1.0 - rate)
            for s in range(sims):
                avail[(pid, w, s)] = rng.random() < hit
        # HOW MUCH HE TAKES IS DRAWN, NOT AVERAGED, and drawn ONCE per season so
        # a man who wins the job keeps it. These distributions are bimodal --
        # roles.py says so outright, and the fit shows RB3 at mean 0.257 with an
        # sd of 0.439 and a median of 0.187 -- so spending the mean every week
        # deletes precisely the tail that makes a late-round back worth holding.
        # Clipping at zero reproduces the mass the median already reports.
        m, sd = _absorb_dist(p["pos"], p.get("rank"))
        for s, v in enumerate(_draw_shares(rng, m, sd, sims)):
            share[(pid, s)] = v
    return vac, avail, share


def _draw_shares(rng, m: float, sd: float, sims: int) -> list[float]:
    """`sims` draws of the absorbed fraction, spread restored and mean preserved.

    THE CLIP CANNOT BE ALLOWED TO MOVE THE MEAN. A quarterback's rank-2 cell is
    mean 0.840 with an sd of 0.470, so a third of the normal sits above 1.0 and
    clipping it drags the average down to around 0.75 -- which would price every
    backup in the league below the number expected.py calibrated against, quietly
    and everywhere. Rescaling to the fitted mean keeps both properties: the
    average is the one that was measured, and the spread is the one that makes a
    lottery ticket different from a steady contributor.
    """
    if not sd:
        return [m] * sims
    v = [min(1.0, max(0.0, rng.gauss(m, sd))) for _ in range(sims)]
    if m <= 0:
        return v
    # Iterated, because rescaling pushes values through the ceiling and they are
    # clipped again. One pass leaves a quarterback's rank-2 cell at 0.776 against
    # a fitted 0.840; a few passes converge, and where the ceiling makes the mean
    # genuinely unreachable this stops rather than looping.
    for _ in range(24):
        got = sum(v) / len(v)
        if got <= 0 or abs(got - m) < 1e-4:
            break
        v = [min(1.0, x * m / got) for x in v]
    return v


def _absorb_dist(pos: str, rank) -> tuple[float, float]:
    """(mean, sd) of the fraction this rank absorbs. sd 0 where it is unfitted."""
    if not rank or rank <= 1:
        return 0.0, 0.0
    cell = ((roles.load_fit().get("curve") or {}).get(pos) or {}).get(str(rank))
    if not cell or cell.get("n", 0) < roles.MIN_EVENTS:
        return roles.absorption(pos, rank)[0], 0.0
    return float(cell["mean"]), float(cell.get("sd") or 0.0)


# -------------------------------------------------------------- the simulation

def season_totals(ids: list[str], S: dict, weeks: list[int], weights: dict,
                  vac, avail, share, sims: int, repl: dict | None = None) -> list[float]:
    """One optimal-lineup season total per simulated world."""
    out = []
    for s in range(sims):
        tot = 0.0
        for w in weeks:
            cands = []
            for pid in ids:
                p = S.get(pid)
                if not p:
                    continue
                cell = p["weeks"].get(w)
                if cell is None:            # no game: a bye needs no draw
                    continue
                if not avail.get((pid, w, s), True):
                    continue
                s1, s2, a, final, miss = cell
                tm, pos = p["room"]
                opened = bool(tm) and vac.get((tm, pos, w, s), False)
                if opened and p.get("rank") == 1:
                    continue                # it is HIS job that came open
                if p.get("flat"):
                    # No shape: spend the flat number, undoing the availability
                    # the draw has already decided for us.
                    cands.append({"player_id": pid, "name": p["name"],
                                  "pos": p["pos"],
                                  "pts": final / a if a > 0 else final,
                                  "has_game": True, "injury": None, "locked": False})
                    continue
                gain = 0.0
                if opened and p.get("absorbs"):
                    # s2 is the lead's own number times the MEAN fraction, so
                    # dividing it back out recovers what the vacated job is
                    # worth and this world's draw decides how much of it he
                    # takes. Averaging here is what priced a lottery ticket at
                    # the same number every week and therefore at nothing.
                    gain = (s2 / p["absorbs"]) * share.get((pid, s), 0.0)
                pts = p["k"] * (s1 + gain)
                cands.append({"player_id": pid, "name": p["name"], "pos": p["pos"],
                              "pts": pts, "has_game": True, "injury": None,
                              "locked": False})
            # The wire, one body per position, always available. It is in every
            # hypothetical so it cancels -- what it changes is the FLOOR, which
            # is the whole point: a bench man below it adds nothing, and a hurt
            # starter costs only the drop to here.
            for pos, byweek in (repl or {}).items():
                if w in byweek:
                    cands.append({"player_id": f"wire-{pos}", "name": f"wire {pos}",
                                  "pos": pos, "pts": byweek[w], "has_game": True,
                                  "injury": None, "locked": False})
            tot += weights.get(w, 1.0) * _optimize(cands)
        out.append(tot)
    return out


def _optimize(cands: list[dict]) -> float:
    """Best legal assignment over the SKILL slots only."""
    saved = lineup.SLOTS
    try:
        lineup.SLOTS = SKILL_SLOTS
        return lineup.optimize(cands)[1]
    finally:
        lineup.SLOTS = saved


def _score(totals: list[float], contender: bool) -> float:
    """Mean for a contender, upper percentile for a long-shot."""
    if contender:
        return sum(totals) / len(totals)
    v = sorted(totals)
    i = min(len(v) - 1, int(len(v) * UPSIDE_PCTL / 100))
    return v[i]


class Board:
    """The simulated worlds plus our roster, so hypotheticals share both."""

    def __init__(self, league_id: str = LEAGUE_ID_2026, sims: int = SIMS,
                 extra: list[str] | None = None):
        d = series(league_id)
        self.S, self.weights = d["players"], d["weights"]
        self.week = d["week"]
        self.weeks = sorted(self.weights)
        self.sims = sims
        self.mine = [p for p in (season.mine(league_id).get("players") or [])
                     if p in self.S]
        # Every id that could appear in ANY hypothetical is drawn for up front,
        # so a candidate is scored against the same worlds our own men are.
        pool = sorted(set(self.mine) | set(extra or []))
        self.vac, self.avail, self.share = draws(pool, self.S, self.weeks, sims)
        self.repl = replacement(self.S, self.weeks, league_id)
        self.p_playoffs = playoffs.p_playoffs(league_id=league_id, default=1.0)
        self.contender = self.p_playoffs >= CONTENDER_ODDS
        self.base = self.totals(self.mine)

    def totals(self, ids):
        return season_totals(ids, self.S, self.weeks, self.weights,
                             self.vac, self.avail, self.share, self.sims, self.repl)

    def score(self, totals):
        return _score(totals, self.contender)

    def shape(self, totals) -> dict:
        """The whole distribution of a change, not just its middle.

        THE MEAN CANNOT RANK THE BOTTOM OF A BENCH, and that is not a tuning
        problem. Every candidate for the last bench spot is worth about nothing
        in expectation -- he does not play in the median world, by definition --
        so selecting on the mean is selecting on noise, and it systematically
        prefers a man who is marginally better than a replacement to one who
        could become more than just a guy. Measured on this roster: Cooper Kupp
        means 1.7 against Kaelon Black's 1.2, while Black's ninetieth percentile
        is 4.8 against Kupp's 4.1 and his ceiling is 17.2 against 11.6. The mean
        picks Kupp. The bench slot wants Black.

        So a starting-slot upgrade is judged on `mean` -- he plays every week and
        expected points is exactly the right question -- and a bench slot on
        `p90` and `p_hit`, which is where the difference between a lottery ticket
        and a singles hitter actually lives.
        """
        import statistics as st
        d = sorted(a - b for a, b in zip(totals, self.base))
        n = len(d)
        # BOTH tails are kept, because which one is the "upside" depends on the
        # question. Adding a man gives positive deltas and his ceiling is p90;
        # dropping one gives negative deltas and his ceiling is p10, the world
        # where losing him hurt most. Reading max for a drop returns the world
        # where he did not matter at all, which is always zero.
        return {"mean": sum(d) / n,
                "se": st.stdev(d) / (n ** 0.5) if n > 1 else 0.0,
                "p10": d[max(0, int(0.1 * n))],
                "p90": d[min(n - 1, int(0.9 * n))],
                "min": d[0], "max": d[-1],
                "p_hit": sum(1 for x in d if abs(x) > HIT_POINTS) / n}

    def _paired(self, totals) -> tuple[float, float]:
        """(delta, standard error) against the baseline, paired by world.

        PAIRED because the worlds are shared. The standard error of the
        difference is what says whether a gap is real, and computing it from the
        two totals separately would report the season's own spread -- tens of
        points -- and drown every gap this exists to rank.
        """
        import statistics as st
        d = [a - b for a, b in zip(totals, self.base)]
        se = st.stdev(d) / (len(d) ** 0.5) if len(d) > 1 else 0.0
        return self.score(totals) - self.score(self.base), se

    def drop_price(self, pid: str) -> tuple[float, float]:
        """What we give up by cutting him -- delta and its standard error."""
        d, se = self._paired(self.totals([p for p in self.mine if p != pid]))
        return -d, se

    def move_value(self, add: str, drop: str) -> tuple[float, float]:
        ids = [p for p in self.mine if p != drop] + [add]
        return self._paired(self.totals(ids))


# ---------------------------------------------------------------- reporting

def _base(p: dict, w: int) -> float:
    """His ordinary weekly number, before any door opens and before availability."""
    cell = p["weeks"].get(w)
    if not cell:
        return 0.0
    s1, _, a, final, _ = cell
    if p.get("flat"):
        return final / a if a > 0 else final
    return p["k"] * s1


def replaceability(b, league_id: str = LEAGUE_ID_2026) -> list[tuple]:
    """(pos, our starters, wire 1st, wire 2nd, wire as % of ours), per position.

    WHICH BENCH SPOTS ARE WORTH SPENDING ON A TICKET, and the answer is not the
    same at every position. Measured on this roster over weeks 2-14: the wire's
    best tight end is 88% of ours and its best back is 26%. Depth is nearly
    worthless at tight end and receiver, because a spot starter is simply there
    every Tuesday, and it is close to irreplaceable at running back.

    The second column is the shape, and quarterback has a different one from
    everything else: 11.3 then 4.0, a cliff rather than a slope. There is exactly
    one startable quarterback unowned, so in a two-quarterback league a single
    claim cannot cover both slots -- which is why the wire floor moved every
    other bench player toward zero and left Carson Beck untouched.
    """
    import statistics as st
    held = season.rostered_ids(league_id)
    mine = set(b.mine)
    starts = {"QB": 2, "RB": 2, "WR": 2, "TE": 1}
    out = []
    for pos, n in starts.items():
        ours, w1, w2 = [], [], []
        for w in b.weeks:
            o = sorted((_base(b.S[p], w) for p in mine
                        if b.S.get(p, {}).get("pos") == pos), reverse=True)
            f = sorted((_base(p, w) for pid, p in b.S.items()
                        if pid not in held and p["pos"] == pos and w in p["weeks"]),
                       reverse=True)
            if o[:n]:
                ours.append(st.mean(o[:n]))
            if f:
                w1.append(f[0])
                w2.append(f[1] if len(f) > 1 else f[0])
        if ours and w1:
            m = st.mean(ours)
            out.append((pos, m, st.mean(w1), st.mean(w2), st.mean(w1) / max(m, 1e-9)))
    return sorted(out, key=lambda t: -t[4])


def roster_report(league_id: str = LEAGUE_ID_2026, sims: int = SIMS) -> str:
    b = Board(league_id, sims)
    rows = []
    for pid in b.mine:
        sh = b.shape(b.totals([p for p in b.mine if p != pid]))
        r = b.S[pid]
        rows.append((r["name"], r["pos"], r.get("rank"), -sh["mean"], sh["se"],
                     -sh["p10"], sh["p_hit"]))
    rows.sort(key=lambda t: t[3])
    L = [f"WHAT EACH MAN IS WORTH TO KEEP - {sims} simulated seasons, "
         f"weeks {b.weeks[0]}-{b.weeks[-1]}",
         f"  objective: {'mean' if b.contender else f'p{UPSIDE_PCTL} (upside)'}"
         f"  (P(playoffs) {b.p_playoffs:.0%})",
         f"  baseline season total {b.score(b.base):.0f}"
         f"  (the eight skill slots; K and DEF are refillable every week and"
         f" cancel out of every comparison)", "",
         f"  {'player':<22}{'pos':<4}{'rk':>3}{'cost to drop':>13}{'+/-':>6}"
         f"{'worst case':>12}{'P(matters)':>12}"]
    for n, pos, rk, v, se, ceil, ph in rows:
        L.append(f"  {n[:22]:<22}{pos:<4}{str(rk or '-'):>3}{v:>13.1f}{se:>6.1f}"
                 f"{ceil:>12.1f}{ph:>11.0%}")
    L += ["", "  cost to drop is the season points we lose across the simulated",
          "  worlds -- which is where a backup earns his place, since he only",
          "  ever plays in the ones where somebody ahead of him is gone.",
          "",
          "  READ THE LAST TWO COLUMNS FOR THE BOTTOM OF THE BENCH. Down there",
          "  every man is worth about nothing on average, so the mean is ranking",
          "  noise; the ceiling and P(matters) are what separate a lottery ticket",
          f"  from somebody marginally better than the wire. Worst case is the",
          "  tenth percentile -- what losing him costs in the seasons where it",
          "  actually bites. P(matters) is the",
          f"  share of simulated seasons he moves us by more than {HIT_POINTS:.0f} points."]
    L += ["", "HOW REPLACEABLE EACH POSITION IS -- which is where a bench spot is",
          "better spent on a man who might become more than just a guy:", "",
          f"  {'pos':<5}{'our starters':>13}{'wire 1st':>10}{'wire 2nd':>10}"
          f"{'wire as % of ours':>19}"]
    for pos, m, w1, w2, pct in replaceability(b, league_id):
        L.append(f"  {pos:<5}{m:>13.1f}{w1:>10.1f}{w2:>10.1f}{pct:>18.0%}")
    L += ["", "  a high percentage means the wire already supplies that slot, so depth",
          "  there is a wasted roster spot. The second column is the SHAPE, and",
          "  quarterback has a different one: a cliff rather than a slope, so one",
          "  claim cannot fill two QB slots the way it can fill a second receiver."]
    return "\n".join(L)


def starts_in_the_median_world(b, ids: list[str], pid: str) -> bool:
    """Would he be in the optimal ten if nobody got hurt?

    WHICH TAIL TO READ DEPENDS ON THIS, and nothing else in the output does. A
    man who starts is judged on his mean, because he plays every week and
    expected points is exactly the question. A man who does not is judged on his
    ceiling, because down there every candidate is worth about nothing in
    expectation and the mean is ranking noise -- it prefers somebody marginally
    better than the wire to somebody who could become more than that.
    """
    for w in b.weeks:
        cands = []
        for q in ids:
            p = b.S.get(q)
            if not p or w not in p["weeks"]:
                continue
            cands.append({"player_id": q, "name": p["name"], "pos": p["pos"],
                          "pts": _base(p, w), "has_game": True,
                          "injury": None, "locked": False})
        for pos, byweek in (b.repl or {}).items():
            if w in byweek:
                cands.append({"player_id": f"wire-{pos}", "name": f"wire {pos}",
                              "pos": pos, "pts": byweek[w], "has_game": True,
                              "injury": None, "locked": False})
        saved = lineup.SLOTS
        try:
            lineup.SLOTS = SKILL_SLOTS
            filled, _ = lineup.optimize(cands)
        finally:
            lineup.SLOTS = saved
        if any(c and c.get("player_id") == pid for c in filled):
            return True
    return False


def price_options(b, drops: list[str], adds: list[str]) -> list[dict]:
    """Every (add, drop) pair, priced and labelled by which slot it fills.

    `excess` is what makes the channel comparison possible in Phase 4: a waiver
    claim is only worth FAAB if it beats what the free board would have given us
    for the same slot, and that comparison needs both channels priced the same
    way against the same drop.
    """
    out = []
    for drop in drops:
        if drop not in b.S:
            continue
        for add in adds:
            if add not in b.S or add == drop:
                continue
            ids = [p for p in b.mine if p != drop] + [add]
            sh = b.shape(b.totals(ids))
            out.append({"add": add, "drop": drop,
                        "gain": sh["mean"], "se": sh["se"],
                        "ceiling": sh["p90"], "p_hit": sh["p_hit"],
                        "starter": starts_in_the_median_world(b, ids, add)})
    return out


@lru_cache(maxsize=2)
def board(league_id: str = LEAGUE_ID_2026, sims: int = SIMS):
    """The simulated worlds, built once per process.

    Cached because the callers are chatty: droppables() asks for a drop price
    per man on the roster and the planner then asks for a price per candidate,
    and each cold build draws every world again. One build, many questions.
    """
    return Board(league_id, sims)


def drop_price(pid: str, league_id: str = LEAGUE_ID_2026) -> float:
    """What cutting this man costs us, over the simulated seasons.

    THE SEAM value.hold_of NOW SERVES. ros.hold priced Carson Beck at 0.4 and
    made him the cheapest man on our roster to drop; this prices him at 37.5 and
    puts him above six of our starters, because it can see the worlds where our
    other two quarterbacks are not there. The ordering was exactly inverted.

    Our roster only. Another manager's bench cannot be priced this way -- we do
    not know who he would start -- so the blocking test keeps ros.hold.
    """
    b = board(league_id)
    if pid not in b.mine:
        return 0.0
    return round(b.drop_price(pid)[0], 2)


def moves_report(league_id: str = LEAGUE_ID_2026, sims: int = SIMS,
                 top: int = 12) -> str:
    """Every add moves.py would consider, priced both ways.

    THE POINT IS THE DISAGREEMENT. moves.py subtracts two absolute season totals
    across different positions; this asks what the move does to the lineup we
    would actually field, in the worlds we might be in. Where they agree there is
    nothing to decide. Where they do not, one of them is wrong, and the gap is
    the argument for switching the seam.
    """
    from robo import moves, value
    ctx = moves._context(league_id, mode="ros")
    pool = [c for c in moves.candidates(ctx, waivers=False)
            if (ctx["players"].get(c["row"]["player_id"]) or {}).get("position")
            in ("QB", "RB", "WR", "TE")][:top]
    adds = [c["row"]["player_id"] for c in pool]
    b = Board(league_id, sims, extra=adds)
    drops = [d["row"]["player_id"] for d in moves.droppables(ctx)][:3] or b.mine[:1]

    L = [f"EVERY ADD, PRICED BOTH WAYS - {sims} simulated seasons",
         f"  objective: {'mean' if b.contender else f'p{UPSIDE_PCTL} (upside)'}"
         f"  (P(playoffs) {b.p_playoffs:.0%})", "",
         f"  {'add':<22}{'pos':<5}{'drop':<20}{'moves.py':>10}{'marginal':>10}{'+/-':>7}"]
    rows = []
    for c in pool:
        add = c["row"]["player_id"]
        if add not in b.S:
            continue
        for drop in drops:
            old = round(c["value"] - value.hold_of(ctx["by_id"].get(drop)
                                                   or {"player_id": drop},
                                                   ctx["week"])[0], 1)
            new, se = b.move_value(add, drop)
            rows.append((b.S[add]["name"], b.S[add]["pos"],
                         b.S[drop]["name"], old, new, se))
    rows.sort(key=lambda t: -t[4])
    for n, pos, dn, old, new, se in rows[:top * 2]:
        L.append(f"  {n[:22]:<22}{pos:<5}{dn[:20]:<20}{old:>10.1f}{new:>10.1f}{se:>7.1f}")
    L += ["", "  moves.py is add_value minus drop_value, two absolute season totals",
          "  across different positions. marginal is what the move does to the",
          "  lineup we would actually field, averaged over the simulated worlds.",
          "  A gap inside its own +/- is not a ranking."]
    return "\n".join(L)


def calibrate() -> str:
    """What an add has actually been worth in this league. Where HIT_POINTS comes from.

    Measured the way faab.py measured bids: from what happened, not from a
    formula. For every completed add, the points the added player went on to
    score IN A STARTING SLOT for the manager who added him.

    READ THE POSITION TABLE CAREFULLY -- it measures how adds are USED, not what
    a position is worth. Kickers and defences are dead 7% of the time against
    60% for backs and receivers, which is not a statement that a kicker is more
    valuable; it is a statement that you add a kicker to start him on Sunday and
    you add a back on the chance he becomes something. The skill positions are
    where the lottery is, and the quarterback tail is the fattest of them -- p90
    of 72 -- which is the same cliff the wire table finds from the other side.
    """
    import collections
    import sqlite3
    db = DATA / "history.db"
    if not db.exists():
        return "no data/history.db -- run `python -m robo.history`"
    players = api.players()
    c = sqlite3.connect(str(db))
    started: dict = collections.defaultdict(float)
    for s, w, r, ss, sp in c.execute(
            "select season,week,roster_id,starters,starters_points from matchups"):
        try:
            S, P = json.loads(ss), json.loads(sp)
        except Exception:
            continue
        for pid, pts in zip(S, P):
            if pid and pid != "0":
                started[(s, int(r), str(pid))] += float(pts or 0)
    allv, bypos = [], collections.defaultdict(list)
    for s, w, adds in c.execute("select season,week,adds from transactions "
                                "where status='complete' and adds is not null "
                                "and adds!='null'"):
        try:
            A = json.loads(adds) or {}
        except Exception:
            continue
        for pid, rid in A.items():
            v = started.get((s, int(rid), str(pid)), 0.0)
            allv.append(v)
            bypos[(players.get(str(pid)) or {}).get("position") or "DEF"].append(v)
    allv.sort()
    n = len(allv)
    if not n:
        return "no completed adds in history"

    def q(v, p):
        return sorted(v)[min(len(v) - 1, int(p / 100 * len(v)))]
    L = [f"WHAT AN ADD HAS BEEN WORTH - {n} completed adds, this league only", "",
         "  realised points the added player scored IN A STARTING SLOT for the",
         "  manager who added him, over the rest of that season", "",
         f"  {'median':>15}{q(allv, 50):>9.1f}",
         f"  {'p75':>15}{q(allv, 75):>9.1f}",
         f"  {'p90':>15}{q(allv, 90):>9.1f}",
         f"  {'never started':>15}{100 * sum(1 for v in allv if v == 0) / n:>8.0f}%", "",
         f"  {'pos':<5}{'n':>7}{'median':>9}{'p75':>9}{'p90':>9}{'dead':>8}"]
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        v = bypos.get(pos) or []
        if len(v) < 25:
            continue
        L.append(f"  {pos:<5}{len(v):>7}{q(v, 50):>9.1f}{q(v, 75):>9.1f}{q(v, 90):>9.1f}"
                 f"{100 * sum(1 for x in v if x == 0) / len(v):>7.0f}%")
    L += ["", f"  HIT_POINTS is set to the overall median ({HIT_POINTS:.0f}), so P(matters)",
          "  reads as 'at least what a typical add in this league does'.", "",
          "  The position rows measure how adds are USED, not what a position is",
          "  worth: a kicker is added to be started on Sunday, a running back is",
          "  added on the chance he becomes something. Which is why the skill",
          "  positions are dead 60% of the time and carry all of the upside."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="what a roster move is really worth")
    ap.add_argument("--calibrate", action="store_true",
                    help="what an add has been worth in this league's history")
    ap.add_argument("--roster", action="store_true", help="what each man we hold is worth")
    ap.add_argument("--moves", action="store_true", help="every live proposal, old vs new")
    ap.add_argument("--sims", type=int, default=SIMS)
    a = ap.parse_args()
    if a.calibrate:
        print(calibrate())
    else:
        print(moves_report(sims=a.sims) if a.moves else roster_report(sims=a.sims))


if __name__ == "__main__":
    main()
