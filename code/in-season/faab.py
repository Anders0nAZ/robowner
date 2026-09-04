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

# What a FAAB dollar is worth in rest-of-season points, at an even spending
# pace. This is the whole bid policy in one number: at $0 the first dollar buys
# ten points of win probability and past about $10 a dollar buys a fraction of
# one, so where this sits decides where on that curve we stop. Lower it to bid
# harder.
#
# THE BUDGET REALLY IS SCARCE HERE, which is why the number is not near zero:
# the median team spends $89 of $100 and 24 of 55 team-seasons on record
# exhausted it outright. It is also WORTHLESS AT THE WHISTLE -- nothing carries
# over -- so hoarding is its own failure, which is what the pacing below is for.
MIN_POINTS_PER_DOLLAR = 1.0

# How far the pace adjustment may push the price of a dollar. Unclamped, a team
# that has spent nothing by week 14 would price dollars at almost zero and empty
# the budget on the first player it saw.
PACE_BOUNDS = (0.25, 4.0)

# Never bid below this on a rung we actually want. A $0 claim is a real claim
# here and 18 of 120 contested auctions were won with one -- but a dollar is the
# single best-value dollar in the whole budget, so it is the floor.
MIN_LIVE_BID = 1

# Fraction of the remaining budget a single claim may ever commit. A backstop
# against one bad valuation spending the season.
MAX_SINGLE_BID_PCT = 0.5

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
    """One row per WON claim: (week, top rival bid or None if uncontested).

    Keyed on the winner because that is the position we will be in: we are
    asking what it would have taken to beat the field for a player somebody did
    in fact win.
    """
    from collections import defaultdict
    g = defaultdict(list)
    for season, week, pid, bid, won, loss in claims():
        g[(season, week, pid)].append((bid, won, loss))
    out = []
    for (_, week, _), rows in g.items():
        if not any(w for _, w, _ in rows):
            continue
        rivals = [b for b, _, l in rows if l]
        out.append((week, max(rivals) if rivals else None))
    return tuple(out)


@lru_cache(maxsize=8)
def _model(week: int) -> tuple:
    """(P(contested), sorted top-rival bids) for this week's bucket.

    Falls back to the whole record when a bucket is too thin to say anything,
    which matters most in the late-season buckets where contests are rare.
    """
    lo, hi = bucket_of(week)
    sub = [a for a in auctions() if lo <= a[0] <= hi]
    if len(sub) < 20:
        sub = list(auctions())
    if not sub:
        return 0.0, ()
    rivals = sorted(a[1] for a in sub if a[1] is not None)
    return len(rivals) / len(sub), tuple(rivals)


def p_win(bid: int, week: int) -> float:
    """P(nobody else wants him) + P(contested) x P(top rival bids less than us).

    Ties go to the OTHER owner. Sleeper breaks an equal-bid tie on waiver
    priority, which we do not control and cannot see, so assuming we lose it is
    the assumption that cannot flatter us.
    """
    p_contested, rivals = _model(week)
    if not rivals:
        return 1.0
    beat = sum(1 for r in rivals if r < bid) / len(rivals)
    return round((1.0 - p_contested) + p_contested * beat, 4)


def best_bid(gain: float, week: int, faab_left: int) -> tuple[int, str]:
    """The bid maximising P(win) x gain, less MIN_POINTS_PER_DOLLAR per dollar.

    The price of a dollar is what it would have bought on some later claim, so
    the objective carries a linear cost term rather than being pure expected
    value -- taking the argmax of P(win) x gain alone would bid the cap on the
    first decent player of September.

    IT IS AN ARGMAX AND NOT A WALK UP THE MARGIN. The rival distribution is an
    ECDF over 120 real auctions, so it is a step function with flat stretches
    between the bids anyone actually made. Stopping the first time a dollar buys
    nothing quit at $2 in week 2 while $10 was worth eight points more, because
    no rival in the record ever bid $3. Ties go to the CHEAPER bid: two bids
    worth the same expected points are not worth the same money.
    """
    cap = max(0, int(MAX_SINGLE_BID_PCT * max(0, faab_left)))
    if cap <= 0 or gain <= 0:
        return 0, "no budget to spend"
    lam = dollar_price(week, faab_left)
    tol = max(1e-9, BID_TOLERANCE_PCT * gain)
    best, best_v = 0, p_win(0, week) * gain
    for b in range(1, cap + 1):
        v = p_win(b, week) * gain - b * lam
        if v > best_v + tol:
            best, best_v = b, v
    bid = min(cap, max(MIN_LIVE_BID, best)) if cap >= MIN_LIVE_BID else cap
    return int(bid), (f"P(win) {p_win(bid, week):.0%} at ${bid}; a dollar is "
                      f"priced at {lam:.2f} pts here (cap ${cap})")


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
    total = max(1, _season.SEASON_WEEKS)
    weeks_left = max(1, total - max(0, week) + 1)
    have = max(0.0, min(1.0, faab_left / budget))
    ahead = weeks_left / total
    if have <= 0:
        return PACE_BOUNDS[1] * MIN_POINTS_PER_DOLLAR
    pace = have / ahead
    pace = max(PACE_BOUNDS[0], min(PACE_BOUNDS[1], pace))
    return MIN_POINTS_PER_DOLLAR / pace


def ladder(week: int, gains: list[float], faab_left: int) -> list[int]:
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
    out, ceiling = [], None
    for g in gains:
        b, _ = best_bid(g, week, faab_left)
        b = max(MIN_LIVE_BID, b)
        if ceiling is not None:
            b = min(b, ceiling)
        ceiling = b
        out.append(int(b))
    return out


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
        riv = [a[1] for a in sub if a[1] is not None]
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
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--gain", type=float, default=80.0)
    ap.add_argument("--faab", type=int, default=100)
    args = ap.parse_args()
    print(report(args.week, args.gain, args.faab))


if __name__ == "__main__":
    main()
