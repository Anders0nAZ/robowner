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
            wk[int(w)] = (d.get("s1") or 0.0, d.get("s2") or 0.0, d.get("a") or 0.0)
        out[pid] = {"name": r.get("name"), "pos": r.get("pos"), "team": r.get("team"),
                    # season-only rows carry no shape, so they are scaled at 1.0
                    # and priced on the flat series expected.py already wrote.
                    "k": r.get("k") or 1.0, "rank": r.get("rank"),
                    # The MEAN fraction expected.py folded into s2. Kept so the
                    # simulation can recover the lead's own number and redraw the
                    # fraction, instead of spending the average every week.
                    "absorbs": r.get("absorbs") or 0.0,
                    "room": (r.get("team"), r.get("pos")), "weeks": wk}
    return {"players": out, "week": ex["week"], "weights": {int(k): v for k, v in
                                                            ex["weights"].items()}}


def _rooms_of(ids: list[str], S: dict) -> set:
    return {S[p]["room"] for p in ids if p in S and all(S[p]["room"])}


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
            a = (p["weeks"].get(w) or (0.0, 0.0, 0.0))[2]
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
                  vac, avail, share, sims: int) -> list[float]:
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
                s1, s2, _ = cell
                tm, pos = p["room"]
                opened = bool(tm) and vac.get((tm, pos, w, s), False)
                if opened and p.get("rank") == 1:
                    continue                # it is HIS job that came open
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
        self.p_playoffs = playoffs.p_playoffs(league_id=league_id, default=1.0)
        self.contender = self.p_playoffs >= CONTENDER_ODDS
        self.base = self.totals(self.mine)

    def totals(self, ids):
        return season_totals(ids, self.S, self.weeks, self.weights,
                             self.vac, self.avail, self.share, self.sims)

    def score(self, totals):
        return _score(totals, self.contender)

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

def roster_report(league_id: str = LEAGUE_ID_2026, sims: int = SIMS) -> str:
    b = Board(league_id, sims)
    rows = []
    for pid in b.mine:
        v, se = b.drop_price(pid)
        r = b.S[pid]
        rows.append((r["name"], r["pos"], r.get("rank"), v, se))
    rows.sort(key=lambda t: t[3])
    L = [f"WHAT EACH MAN IS WORTH TO KEEP - {sims} simulated seasons, "
         f"weeks {b.weeks[0]}-{b.weeks[-1]}",
         f"  objective: {'mean' if b.contender else f'p{UPSIDE_PCTL} (upside)'}"
         f"  (P(playoffs) {b.p_playoffs:.0%})",
         f"  baseline season total {b.score(b.base):.0f}"
         f"  (the eight skill slots; K and DEF are refillable every week and"
         f" cancel out of every comparison)", "",
         f"  {'player':<24}{'pos':<5}{'rank':>5}{'cost to drop':>14}{'+/-':>8}"]
    for n, pos, rk, v, se in rows:
        L.append(f"  {n[:24]:<24}{pos:<5}{str(rk or '-'):>5}{v:>14.1f}{se:>8.1f}")
    L += ["", "  cost to drop is the season points we lose across the simulated",
          "  worlds -- which is where a backup earns his place, since he only",
          "  ever plays in the ones where somebody ahead of him is gone."]
    return "\n".join(L)


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


def main():
    ap = argparse.ArgumentParser(description="what a roster move is really worth")
    ap.add_argument("--roster", action="store_true", help="what each man we hold is worth")
    ap.add_argument("--moves", action="store_true", help="every live proposal, old vs new")
    ap.add_argument("--sims", type=int, default=SIMS)
    a = ap.parse_args()
    print(moves_report(sims=a.sims) if a.moves else roster_report(sims=a.sims))


if __name__ == "__main__":
    main()
