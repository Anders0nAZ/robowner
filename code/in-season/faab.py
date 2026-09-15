"""What a waiver claim costs in THIS league, from six seasons of real auctions.

moves.bid_for() used to price a claim with an invented formula -- an aggression
constant times the gain, divided by the weeks left. The shape was reasonable and
the numbers were made up. This module replaces the numbers with the league's own
1,032 recorded claims, 2020-2025.

READ `notes`, NOT `drops`. Sleeper states why a claim failed, in
`metadata.notes`, and that field is the only thing separating a claim that LOST
ON PRICE from one that bounced off a full roster:

     403  "Unfortunately, your roster will have too many players..."
     180  "This player was claimed by another owner."
       5  "You are over the budget for this transaction."
       3  "One of the players you are trying to drop has already started playing."

Inferring the same thing from the `drops` column gets it exactly backwards. Every
failed claim on record shows `drops: null`, which looks like nobody ever lost
while naming a drop -- and that reading is an artifact: a failed claim never
executes its drop, so it never records one. The three claims that failed because
"one of the players you are trying to drop has already started" prove it, since
they necessarily named a drop and still show none. Any measurement built on
"claims that named a drop" is therefore circular, because a recorded drop is a
consequence of winning rather than a cause.

WHY A NAIVE P(win | bid) CURVE IS WORSE THAN NOTHING. Taken over all genuine
auction claims it is FLAT -- 71% at $0, 74% at $1-3, 74% at $4-7, 66% at $8-15,
71% at $16-30, 69% at $31+. Bidding more does not appear to help, because bid
size is chosen in RESPONSE to expected competition: people pay up on exactly the
players other people want. Fitting that curve would conclude that money does not
matter.

SO IT IS DECOMPOSED INSTEAD, WHICH IS BOTH ESTIMABLE AND HONEST:

    P(win at bid B) = P(nobody else wants him) + P(contested) x P(top rival < B)

Both halves come straight from the record. Of 440 winning claims, 120 had at
least one rival who lost on price, so a claimed player is contested about 27% of
the time -- falling from 38% in weeks 1-3 to 20% from week 12. And the bid that
has to be beaten, across those 120 real auctions, has median $2, p75 $10, p90
$29, with 46 of 120 top rivals bidding nothing at all.

    $0 -> 73%     $2 -> 85%     $10 -> 92%     $40 -> 98%
    $1 -> 83%     $5 -> 89%     $20 -> 96%

THE FIRST DOLLAR IS THE WHOLE GAME. It buys ten points of win probability;
everything past about $10 buys almost nothing. Winners in this league bid a
median $12 against a median top rival of $2, which is to say they routinely
overpay by an order of magnitude -- and 25 of 120 auctions were won by a dollar
or less.

    python -m robo.faab --report
    python -m robo.faab --week 2 --gain 80
"""

import argparse
import json
import math
import sqlite3
from functools import lru_cache

from robo import DATA, settings

DB = DATA / "history.db"

# Sleeper's own wording for the only failure that is a PRICE loss. Matched on a
# stable prefix rather than the whole sentence, so a full stop moving does not
# silently reclassify every auction in the record as a roster-space bounce.
LOST_ON_PRICE = "this player was claimed by another owner"

# Week buckets for the reference price, chosen off the observed decline in both
# how often a player is contested and what it takes to win him.
BUCKETS = ((1, 3), (4, 7), (8, 11), (12, 18))

# What a FAAB dollar is worth in the same units `gain` is measured in, at an
# even spending pace. This is the whole bid policy in one number: at $0 the
# first dollar buys ten points of win probability and past about $10 a dollar
# buys a fraction of one, so where this sits decides where on that curve we
# stop. Lower it to bid harder.
#
# MEASURED, from this league's own 55 team-seasons: a team wins a median 5
# auction claims (mean 8.0) for a median $89 of spend, and marginal.HIT_POINTS
# puts a typical add's realised starting contribution at 7.0. So $89 buys about
# 35 points and a dollar buys about 0.4 of one.
#
# IT WAS 1.0, WHICH WAS THE OLD UNITS. `gain` used to be a difference of two
# absolute season totals -- tens to hundreds of points -- and is now the change
# in our optimal starting lineup, where the wire is a floor under every slot and
# the same move prices at 1.5 instead of 51. Carrying the old number across
# priced a dollar at two and a half times what the history says it buys, which
# does not merely shade the bids down: with the cost term that large the argmax
# sits at $0 for almost anything the simulator will pass, so the ladder collapses
# to a column of zeroes and the bot never competes for a contested player.
#
# 0.4 is the CONSERVATIVE end of its own derivation. HIT_POINTS is a gross
# starting contribution rather than a gain net of the man he replaced, so the
# true marginal points per dollar is lower still -- and erring high here errs
# toward under-bidding, which is the direction an autonomous bot should miss in.
#
# THE BUDGET REALLY IS SCARCE, which is why the number is not near zero: the
# median team spends $89 of $100 and 24 of 55 team-seasons on record exhausted
# it outright. It is also WORTHLESS AT THE WHISTLE -- nothing carries over -- so
# hoarding is its own failure, which is what the pacing below is for.
MIN_POINTS_PER_DOLLAR = 0.4

# How far the pace adjustment may push the price of a dollar. Unclamped, a team
# that has spent nothing by week 14 would price dollars at almost zero and empty
# the budget on the first player it saw.
PACE_BOUNDS = (0.25, 4.0)

# RETIRED IN PLACE. A $0 claim is a real claim in this league -- 18 of 120
# contested auctions were won with one, and from week 12 the median winning bid
# IS zero -- so a floor is not a validity rule, it is an opinion about a dollar
# of separation from everyone who left the field blank.
#
# It was removed from best_bid() and left in ladder() on the reasoning that a
# priority list needs a floor so two rungs do not tie at nothing. That does not
# survive contact: the ladder it produced was $1 and $1, so the floor relocated
# the tie rather than preventing it, and ties cost nothing anyway because
# Sleeper works a slot's claims in the SEQ order we submit, never by price. What
# it did do was make the same player cost $0 as a single claim and $1 inside a
# list, so his price depended on who else happened to make the shortlist.
#
# Kept as a settable constant so a floor can be reimposed deliberately, and read
# by nothing while it is zero.
MIN_LIVE_BID = 0

# Retained as a compatibility setting, but no longer a live ceiling.  A fixed
# percentage cap can veto a rational league-winning bid even when the modeled
# gain remains positive after paying it.  best_bid() instead stops at the lower
# of the remaining balance and the gain's reservation price.
MAX_SINGLE_BID_PCT = 0.5

# How many claims a POSITION needs before its own contest rate is trusted over
# the pooled one. Competition is not uniform across positions and in a superflex
# league it is least uniform exactly where it matters: measured over 797 claimed
# players, QB is contested 32% of the time and RB 30%, against WR 18%, DEF 17%,
# TE 15% and K 6%, versus a pooled 21%. Pricing a startable quarterback's
# competition off the same number as a bye-week kicker understates the field on
# the one claim most likely to be fought over. Same thin-cell pooling roles.py
# uses -- a rate is either measured on enough events or it is not used.
MIN_POS_CLAIMS = 40

# How many CONTESTED claims a position needs before its own rival-bid level is
# trusted. Lower than MIN_POS_CLAIMS because only the contested subset carries a
# rival bid at all -- a position can have 93 claims and 18 contests. Below this
# the pooled level stands: tight end (11) and kicker (2) pool, quarterback (25),
# receiver (21), defence (18) and running back (43) do not.
MIN_POS_RIVALS = 15

# Only pay more than the cheapest good bid when it buys at least this share of
# what is at stake. The objective is close to flat over wide stretches -- an
# 80-point upgrade in week 2 scores 54.0 at $2 and 55.4 at $11 -- so without a
# tolerance the bid flips between two very different numbers on a rounding
# change in the valuation, and a published decision that swings $9 on noise
# cannot be explained to anybody. Ties break CHEAP, which is also where this
# league's evidence points: winners bid a median $12 against a median top rival
# of $2.
BID_TOLERANCE_PCT = 0.02

settings.apply(__name__, globals())


# --------------------------------------------------------------------- the data

@lru_cache(maxsize=1)
def claims() -> tuple:
    """Every recorded waiver claim: (season, week, player_id, bid, won, price_loss)."""
    if not DB.exists():
        return ()
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return ()
    out = []
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
        if "notes" not in cols:
            return ()   # pre-migration database; re-harvest with robo.history
        q = ("select season, week, adds, waiver_bid, status, notes "
             "from transactions where type='waiver' and waiver_bid is not null")
        for season, week, adds, bid, status, notes in con.execute(q):
            try:
                ids = list(json.loads(adds) or {})
            except (TypeError, ValueError):
                ids = []
            won = status == "complete"
            loss = (notes or "").strip().lower().startswith(LOST_ON_PRICE)
            for pid in ids:
                out.append((str(season), int(week or 0), str(pid),
                            int(bid or 0), won, loss))
    except sqlite3.Error:
        return ()
    finally:
        con.close()
    return tuple(out)


def bucket_of(week: int) -> tuple:
    for lo, hi in BUCKETS:
        if lo <= week <= hi:
            return (lo, hi)
    return BUCKETS[-1]


@lru_cache(maxsize=8)
def auctions() -> tuple:
    """One row per WON claim: (week, pos, top rival bid or None if uncontested).

    Keyed on the winner because that is the position we will be in: we are
    asking what it would have taken to beat the field for a player somebody did
    in fact win.

    The POSITION rides along because competition is not uniform across them --
    see MIN_POS_CLAIMS. Resolved through the player dump, and anyone it cannot
    name keeps a "?" rather than being dropped: he still counts in the pooled
    rate, which is what a thin or unknown cell falls back to.
    """
    from collections import defaultdict
    try:
        from robo import sleeper_read as _api
        players = _api.players()
    except Exception:
        players = {}
    g = defaultdict(list)
    for season, week, pid, bid, won, loss in claims():
        g[(season, week, pid)].append((bid, won, loss))
    out = []
    for (_, week, pid), rows in g.items():
        if not any(w for _, w, _ in rows):
            continue
        rivals = [b for b, _, l in rows if l]
        pos = (players.get(pid) or {}).get("position") or "?"
        out.append((week, pos, max(rivals) if rivals else None))
    return tuple(out)


@lru_cache(maxsize=8)
def _model(week: int, pos: str | None = None) -> tuple:
    """(P(contested), sorted top-rival bids) for this week's bucket.

    Falls back to the whole record when a bucket is too thin to say anything,
    which matters most in the late-season buckets where contests are rare.

    TWO SEPARATE QUESTIONS, ANSWERED FROM DIFFERENT SLICES. How OFTEN a claim is
    contested depends on the position -- a quarterback in this superflex league
    is fought over half again as often as a receiver -- while how MUCH the top
    rival bid is a question about the week, which is where the budget is. So the
    rate comes from the positional cell when it is thick enough and the rival
    distribution stays on the week's bucket, which also keeps the ladder's price
    curve on the sample size it was fitted with.
    """
    lo, hi = bucket_of(week)
    sub = [a for a in auctions() if lo <= a[0] <= hi]
    if len(sub) < 20:
        sub = list(auctions())
    if not sub:
        return 0.0, ()
    rivals = sorted(a[2] for a in sub if a[2] is not None)
    rate = len(rivals) / len(sub)
    if pos and rivals:
        # HOW MUCH the field bids is positional too, and pooling it understates
        # exactly the claim worth winning: a contested quarterback cost a median
        # $5 and a p75 of $13 against a pooled $2 and $10, while a kicker's
        # whole distribution is $1. Scaled rather than substituted, for the same
        # reason the rate is -- the bucket carries the week (budgets deplete and
        # late contests are cheap) and the position carries the level.
        every = [a for a in auctions() if a[2] is not None]
        cell = [a[2] for a in every if a[1] == pos]
        if len(cell) >= MIN_POS_RIVALS and every:
            base = sum(a[2] for a in every) / len(every)
            mine = sum(cell) / len(cell)
            if base > 0:
                k = mine / base
                rivals = sorted(int(round(r * k)) for r in rivals)
    if pos:
        # A MULTIPLIER ON THE WEEK'S RATE, NOT A REPLACEMENT FOR IT. Both
        # signals are real and they are not the same signal: contests run 38% in
        # weeks 1-3 against 20% from week 12, and across all weeks a QB is
        # fought over 1.5x as often as the average claim. Slicing week AND
        # position directly would be right and there is not the data for it --
        # 91 quarterback claims over four buckets is ~23 a cell -- so the
        # positional cell supplies a ratio and the bucket keeps the level.
        # Substituting the all-weeks positional rate outright would have quietly
        # thrown the week away: in week 2 that swaps a bucket rate of 38% for a
        # season-average 37% and calls it an improvement.
        every = auctions()
        cell = [a for a in every if a[1] == pos]
        if len(cell) >= MIN_POS_CLAIMS and every:
            base = sum(1 for a in every if a[2] is not None) / len(every)
            mine = sum(1 for a in cell if a[2] is not None) / len(cell)
            if base > 0:
                rate = max(0.0, min(1.0, rate * mine / base))
    return rate, tuple(rivals)


def p_win(bid: int, week: int, pos: str | None = None) -> float:
    """P(nobody else wants him) + P(contested) x P(top rival bids less than us).

    Ties go to the OTHER owner. Sleeper breaks an equal-bid tie on waiver
    priority, which we do not control and cannot see, so assuming we lose it is
    the assumption that cannot flatter us.
    """
    p_contested, rivals = _model(week, pos)
    if not rivals:
        return 1.0
    beat = sum(1 for r in rivals if r < bid) / len(rivals)
    return round((1.0 - p_contested) + p_contested * beat, 4)


def quote(gain: float, week: int, faab_left: int,
          pos: str | None = None, field: dict | None = None) -> dict:
    """A complete integer-bid utility curve and its cheapest near-optimum.

    FAAB IS PAID ONLY WHEN THE CLAIM WINS.  The expected utility is therefore
    P(win) * (gain - bid * dollar_price), not P(win) * gain minus an
    unconditional bid cost.  The latter silently charged failed claims and was
    the main reason realistic marginal gains collapsed to $0 bids.

    When an opponent field is available it supplies the integer win curve,
    including manager-specific need, balances and tie priority.  Otherwise the
    measured pooled curve remains the explicit fallback.
    """
    faab_left = max(0, int(faab_left))
    if gain <= 0:
        return {"bid": 0, "reason": "no positive roster gain to buy",
                "gain": gain, "curve": [], "near_optimal": [0, 0]}
    lam = dollar_price(week, faab_left)
    reservation = faab_left if lam <= 0 else max(0, int(gain / lam))
    cap = min(faab_left, reservation)
    supplied = list((field or {}).get("win_curve") or [])

    def chance(bid: int) -> float:
        if bid < len(supplied):
            return float(supplied[bid])
        return p_win(bid, week, pos)

    curve = []
    for bid in range(0, cap + 1):
        pw = chance(bid)
        utility = pw * (gain - bid * lam)
        curve.append({"bid": bid, "p_win": round(pw, 6),
                      "net_if_won": round(gain - bid * lam, 6),
                      "expected_utility": round(utility, 6)})
    if not curve:
        curve = [{"bid": 0, "p_win": chance(0), "net_if_won": gain,
                  "expected_utility": chance(0) * gain}]
    peak = max(r["expected_utility"] for r in curve)
    tol = max(1e-9, BID_TOLERANCE_PCT * abs(peak))
    near = [r for r in curve if r["expected_utility"] >= peak - tol]
    chosen = min(near, key=lambda r: r["bid"])
    source = "opponent model" if supplied else "pooled history fallback"
    expected = (field or {}).get("expected_highest")
    expected_plus = (None if expected is None else
                     min(faab_left, max(0, int(math.floor(float(expected))) + 1)))
    reason = (f"P(win) {chosen['p_win']:.0%} at ${chosen['bid']} via {source}; "
              f"a dollar is priced at {lam:.2f} lineup pts; reservation ${cap}")
    return {"bid": int(chosen["bid"]), "reason": reason, "gain": round(gain, 4),
            "p_win": chosen["p_win"], "expected_utility": chosen["expected_utility"],
            "dollar_price": round(lam, 6), "reservation_bid": cap,
            "near_optimal": [min(r["bid"] for r in near), max(r["bid"] for r in near)],
            "expected_highest": expected, "expected_highest_plus_one": expected_plus,
            "highest_quantiles": (field or {}).get("highest_quantiles"),
            "model_version": (field or {}).get("version"),
            "shadow_price": {
                # THE REALISED PRICE, NOT THE FLOOR CONSTANT. Every number in
                # the curve above uses `lam`, and this reported
                # MIN_POINTS_PER_DOLLAR instead -- 0.40 against an actual
                # 0.3765 in week 2 at a full budget. An audit that prints a
                # different price from the one it charged cannot be used to
                # check the bid by hand, which is the only thing it is for.
                "points_per_dollar": round(lam, 6),
                "floor_constant": MIN_POINTS_PER_DOLLAR,
                "status": "provisional",
                "basis": ("league spend and gross starting-contribution proxy; "
                          "not yet calibrated to realized paired lineup gain"),
            },
            "curve": curve}


def best_bid(gain: float, week: int, faab_left: int,
             pos: str | None = None, field: dict | None = None) -> tuple[int, str]:
    """Compatibility wrapper around :func:`quote`."""
    q = quote(gain, week, faab_left, pos, field)
    return q["bid"], q["reason"]


def dollar_price(week: int, faab_left: int) -> float:
    """What one FAAB dollar costs us, adjusted for how the budget is pacing.

    A fixed price is wrong in both directions, because the budget is scarce AND
    expires worthless. Holding $90 in week 12 is not thrift, it is $90 that will
    never buy anything; spending $80 by week 3 leaves nothing for the injuries
    that have not happened yet. So the price of a dollar scales with how much
    budget is left relative to how much season is left -- ahead of pace it gets
    cheaper and we bid harder, behind pace it gets dearer and we stop.
    """
    from robo import season as _season
    budget = max(1, _season.FAAB_BUDGET)
    # This league ends in week 17.  Using the NFL's week 18 made late-season
    # dollars look scarcer than they really were and fought the expires-worthless
    # part of the policy.
    total = 17
    weeks_left = max(1, total - max(0, week) + 1)
    have = max(0.0, min(1.0, faab_left / budget))
    ahead = weeks_left / total
    if have <= 0:
        return PACE_BOUNDS[1] * MIN_POINTS_PER_DOLLAR
    pace = have / ahead
    pace = max(PACE_BOUNDS[0], min(PACE_BOUNDS[1], pace))
    return MIN_POINTS_PER_DOLLAR / pace


def ladder_quotes(week: int, gains: list[float], faab_left: int,
                  positions: list | None = None,
                  fields: list[dict | None] | None = None) -> list[dict]:
    """Full quotes for one priority ladder, with non-increasing live bids."""
    out, ceiling = [], None
    pos = list(positions or []) + [None] * len(gains)
    models = list(fields or []) + [None] * len(gains)
    for gain, ps, field in zip(gains, pos, models):
        q = quote(gain, week, faab_left, ps, field)
        bid = int(q["bid"])
        if MIN_LIVE_BID:
            bid = max(MIN_LIVE_BID, bid)
        if ceiling is not None:
            bid = min(bid, ceiling)
        ceiling = bid
        if bid != q["bid"]:
            row = next((r for r in q["curve"] if r["bid"] == bid), None)
            q = {**q, "bid": bid, "ladder_limited": True}
            if row:
                q.update({"p_win": row["p_win"],
                          "expected_utility": row["expected_utility"]})
            q["reason"] += f"; limited to ${bid} by higher-priority rung"
        out.append(q)
    return out


def ladder(week: int, gains: list[float], faab_left: int,
           positions: list | None = None,
           fields: list[dict | None] | None = None) -> list[int]:
    """Bids for one slot's priority list, top rung first.

    Each rung is priced on ITS OWN gain and then held to the rung above it, so
    the list is non-increasing: Sleeper works our claims in the order we give
    them and the first winner takes the slot, so a cheap rung sitting above an
    expensive one would spend the slot on the lesser player before the better
    one was ever reached.

    NO SEPARATE DECAY SCHEDULE. A lower rung is a worse player, its gain is
    already smaller, and best_bid already charges less for it -- multiplying by
    a step fraction on top of that discounted the same fact twice and collapsed
    every rung below the first to the floor.
    """
    return [q["bid"] for q in ladder_quotes(week, gains, faab_left,
                                             positions, fields)]


def failure_reasons() -> dict:
    """Why claims fail here, which is the fact the whole module turns on."""
    from collections import Counter
    c = Counter()
    for _, _, _, _, won, loss in claims():
        if won:
            c["won"] += 1
        elif loss:
            c["lost on price"] += 1
        else:
            c["failed for another reason"] += 1
    return dict(c)


def report(week: int | None = None, gain: float = 80.0,
           faab_left: int = 100) -> str:
    c = claims()
    if not c:
        return ("no waiver history with failure reasons on file. Rebuild with "
                "`python -m robo.history` -- the notes column is a migration.")
    L = [f"FAAB MODEL - {len(c)} recorded claims", ""]
    for k, v in sorted(failure_reasons().items(), key=lambda kv: -kv[1]):
        L.append(f"  {v:>5}  {k}")
    L += ["", "  P(win | bid) IS DECOMPOSED, because the raw curve is flat --",
          "  people bid more on exactly the players other people want.", "",
          f"  {'weeks':<8}{'auctions':>10}{'contested':>11}{'p50':>6}{'p75':>6}{'p90':>6}"]

    def q(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))]

    for lo, hi in BUCKETS:
        sub = [a for a in auctions() if lo <= a[0] <= hi]
        riv = [a[2] for a in sub if a[2] is not None]
        if len(sub) < 5:
            continue
        L.append(f"  {f'{lo}-{hi}':<8}{len(sub):>10}{len(riv) / len(sub):>10.0%}"
                 f"{q(riv, 50) if riv else 0:>6}{q(riv, 75) if riv else 0:>6}"
                 f"{q(riv, 90) if riv else 0:>6}")
    wk = week or 1
    L += ["", f"  P(win) by bid, week {wk}:  " +
          "  ".join(f"${b}->{p_win(b, wk):.0%}" for b in (0, 1, 2, 5, 10, 20, 40))]
    b, why = best_bid(gain, wk, faab_left)
    L += ["", f"  for a {gain:.0f}-point upgrade in week {wk} with ${faab_left} left: "
              f"bid ${b}", f"    {why}"]
    L.append(f"  ladder for four rungs: "
             f"{ladder(wk, [gain, gain * .8, gain * .6, gain * .4], faab_left)}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="print the historical model report (the default)")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--gain", type=float, default=80.0)
    ap.add_argument("--faab", type=int, default=100)
    args = ap.parse_args()
    print(report(args.week, args.gain, args.faab))


if __name__ == "__main__":
    main()
