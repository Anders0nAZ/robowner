"""Roster moves: free-agent adds and FAAB waiver claims. One policy, two channels.

Adding a free agent and claiming a player off waivers are the SAME decision --
"is this man worth more to us than the worst player we hold" -- differing only
in whether Sleeper lets us just take him or makes us bid. Splitting them into
two modules would fork that judgement in two places, which is the thing this
project keeps saying not to do. So there is one evaluator and two submission
channels.

    python -m robo.moves --free                # instant adds off the wire
    python -m robo.moves --claims              # the Tuesday-night FAAB slate
    python -m robo.moves --free --mode patch   # fix an illegal lineup, now

THE PRIORITY CHAIN IS LEXICOGRAPHIC, NOT A WEIGHTED SUM. Fielding a legal
starting lineup this week beats improving the roster, which beats denying an
opponent, and no amount of the lower thing adds up to the higher one. That is
the same shape as lineup.illegal_starters() bypassing MIN_GAIN_TO_CHANGE:
legality is not a matter of degree. It is implemented as four modes:

    patch   a starting slot is empty or unstartable and the bench cannot cover
            it. May cut into a rising-role player, because a hole in the lineup
            is a certain loss and an inheritance is a maybe. Allowed at any hour.
    fill    the roster is UNDER 17 and a spot is sitting empty. Not a legality
            emergency, so it does not get patch's exemptions -- but an empty spot
            scores zero every week it stays empty, so it does not face the
            upgrade bar either. Nobody is displaced, so there is no incumbent to
            beat. Judged on the CEILING, because an empty spot is exactly where a
            lottery ticket belongs. This is the step that catches the slot an IR
            move just freed.
    ros     ordinary upgrades on the rest-of-season number. The default.
    block   nothing cleared for us, we have a spare turnable slot, and somebody
            on the wire would visibly improve an opponent.

ADDS AND DROPS ARE PRICED OFF DIFFERENT NUMBERS, ON PURPOSE. An add is judged
on `mean` -- what he is worth to us. A drop is judged on `hold` -- `mean` plus
what he stands to inherit if the man ahead of him goes down. Using one number
for both is exactly how a bot cuts a rookie in October and watches somebody else
start him in December. See robo/ros.py.

NO LONG-HORIZON MOVE NEAR KICKOFF. A rest-of-season swap made forty minutes
before the early games is a decision taken on this week's panic with the season's
consequences, and there is no reason it could not have been made on Tuesday. So
`ros` and `block` refuse inside ROS_MOVE_BLACKOUT_H of the next kickoff, and say
so out loud -- a silent no-op would be indistinguishable from "nothing cleared".
`patch` is exempt, because that is the emergency the hour actually justifies.

THE WAIVER MECHANIC THIS IS BUILT AROUND. A losing claim costs nothing: no FAAB,
no penalty, and FAAB leagues have no rolling priority to burn. So BREADTH IS
FREE: several claims naming the SAME drop form a priority list, the first one
that wins consumes the slot, and the rest fail harmlessly against a player who
is no longer on our roster. Submitting one claim instead of a ranked slate
throws that away for nothing.

We are at 17/17, so every claim names a drop -- a no-drop claim on a full roster
is a guaranteed no-op, and 403 of this league's recorded failures are exactly
that. Note that "claims naming a drop always win" is NOT evidence for this and
must not be cited as such: Sleeper only records a drop on a claim that executed,
so the statement is true by construction. See robo/faab.py.
"""

import argparse
import json

from robo import LEAGUE_ID_2026, faab, lineup, season, settings, value, vegas
from robo import sleeper_read as api

# How much a candidate must add to our STARTING LINEUP, across simulated
# seasons, before a transaction is worth making. POLICY, NOT MEASUREMENT, and
# marked as such: this league's history records what an add went on to score in
# a starting slot, which is not a marginal quantity -- the man he displaced would
# have scored too, and that counterfactual is not in the data. Recovering it
# needs the simulator run against a past roster state, which needs the projection
# snapshots projarchive only began taking in September 2026. Re-derive around
# week 6. Until then this is a deliberate reluctance to churn, not a fitted bar.
MIN_GAIN_TO_ADD = 15.0

# How many standard errors a gain must clear before it counts as a gain at all.
# The simulator reports the paired standard error of every option; a difference
# inside its own noise is not a ranking, and acting on one is how a bot makes a
# move a coin flip would have made.
NOISE_MULTIPLE = 2.0

# Never drop anyone whose value is above this. RETIRED IN PLACE and kept only
# as a backstop: it existed so a broken valuation could not cut a genuine
# starter, and the simulator now answers that directly -- a real starter prices
# at 60 to 175 points to drop and a spare part at 0 to 4, with a standard error
# under half a point. The guard that does the work is the hard rule that nobody
# in the current optimal lineup is droppable.
DROP_FLOOR = 120.0

# How many roster spots we are willing to turn over in one waiver run. This caps
# SLOTS, never claims -- capping claims would throw away the free optionality
# that makes a priority list worth submitting in the first place.
MAX_SLOTS_TO_TURN_OVER = 2

# How deep each slot's priority list goes. Losing costs nothing, so this is
# bounded by how many candidates are plausibly worth the slot, not by risk.
SLATE_DEPTH = 5

# Hours before the next kickoff inside which `ros` and `block` stop running.
# Six covers a Sunday morning: the daily job fires at 07:00 local and the early
# games start at 10:00 Phoenix time.
ROS_MOVE_BLACKOUT_H = 6.0

# How far ahead to look for a week we cannot field a legal lineup in. Three is
# about how far a bye is worth pre-empting -- further out and the wire will have
# turned over before it matters.
BYE_LOOKAHEAD_WEEKS = 3

# How much a free agent must improve an OPPONENT before denying him is worth a
# roster spot, and the most we will ever bid to do it. Blocking is the third
# priority and must stay cheap: a slot spent on a player we will never start is
# a slot we do not have when our own need appears.
BLOCK_MIN_DENY = 40.0
BLOCK_MAX_BID = 3

settings.apply(__name__, globals())

MODES = ("patch", "fill", "ros", "block")


# ------------------------------------------------------------------ evaluation

def _context(league_id: str = LEAGUE_ID_2026, mode: str = "ros") -> dict:
    from robo.rankings import build_board
    board = build_board()
    by_id = {r["player_id"]: r for r in board}
    r = season.mine(league_id)
    week = season.current_week()
    secs = vegas.next_kickoff(season.SEASON, week)
    return {
        "board": board, "by_id": by_id, "roster": r, "week": week, "mode": mode,
        "reserve": set(r.get("reserve") or []), "starters": set(r.get("starters") or []),
        "players": api.players(),
        "available": season.free_agents(board, league_id),
        "on_waivers": season.on_waivers(league_id),
        "faab": season.faab_left(league_id),
        "slots": season.slots(league_id),
        "league_id": league_id,
        # None means the schedule could not be read, and unknown is treated as
        # too close rather than plenty of time -- see vegas.next_kickoff.
        "hours_to_kickoff": None if secs is None else round(secs / 3600.0, 2),
    }


def blacked_out(ctx: dict) -> str:
    """Why a long-horizon move must not run right now, or "" if it may.

    `patch` is never blacked out. Everything else is, close to kickoff, and an
    unreadable schedule counts as close.
    """
    if ctx["mode"] == "patch":
        return ""
    h = ctx["hours_to_kickoff"]
    if h is None:
        return ("cannot read kickoff times, so the blackout cannot be cleared; "
                "treating unknown as too close")
    if h < ROS_MOVE_BLACKOUT_H:
        return (f"{h:.1f}h to the next kickoff, inside the "
                f"{ROS_MOVE_BLACKOUT_H:.0f}h blackout for long-horizon moves")
    return ""


def holes(ctx: dict) -> list[dict]:
    """Weeks in the near future we cannot field a legal starting lineup in.

    Reuses lineup.optimize, so a "hole" means the exact DP that sets our lineup
    could not fill a slot -- not a count of bodies by position, which would miss
    that FLEX and SUPER_FLEX draw from the same pool.
    """
    out = []
    ids = [p for p in (ctx["roster"].get("players") or [])
           if p not in ctx["reserve"]]
    for w in range(ctx["week"], min(ctx["week"] + BYE_LOOKAHEAD_WEEKS,
                                    season.SEASON_WEEKS) + 1):
        try:
            cands, _ = lineup.project_roster(ids, season.SEASON, w,
                                             ctx["players"], ctx["league_id"])
            filled, _ = lineup.optimize(cands)
        except Exception:
            continue
        empty = [lineup.SLOTS[i] for i, p in enumerate(filled) if not p]
        unstartable = [lineup.SLOTS[i] for i, p in enumerate(filled)
                       if p and not lineup.startable(p)]
        if empty or unstartable:
            out.append({"week": w, "empty": empty, "unstartable": unstartable})
    return out


def hold_value(row: dict, ctx: dict, mine: bool = True) -> float:
    """What we give up by cutting him.

    OUR OWN MEN ARE PRICED BY SIMULATION; everyone else's are not. ros.hold made
    Carson Beck the cheapest man on our roster to drop at 0.4 while the simulator
    prices him at 37.5, above six of our starters -- the ordering was inverted,
    and that is the bug that produced "the bot tried to drop Carson Beck".

    Another manager's bench cannot be priced the same way, because we do not know
    who he would start, so the blocking test keeps the old number and that is a
    limit rather than an oversight.
    """
    v, _ = value.hold_of(row, ctx["week"], mine=mine)
    return v


def droppables(ctx: dict, roster: dict | None = None) -> list[dict]:
    """Who could be cut, worst first, priced on HOLD.

    Two independent guards on our own roster, because they fail differently. A
    current starter is excluded outright -- if the optimizer is starting him this
    week, cutting him is incoherent regardless of what any number says.
    DROP_FLOOR is the backstop for the number itself being wrong.

    `roster` prices somebody else's bench for the blocking test. Their starters
    are not knowable week to week, so their top ten by value stand in; the
    question there is only "how bad is their worst spare", which does not need
    to be exact.
    """
    mine = roster is None
    r = roster or ctx["roster"]
    ids = list(r.get("players") or [])
    reserve = set(r.get("reserve") or [])
    if mine:
        protected = ctx["starters"] | reserve
    else:
        ranked = sorted((p for p in ids if p not in reserve),
                        key=lambda p: -hold_value(ctx["by_id"].get(p) or {"player_id": p},
                                                  ctx, mine=False))
        protected = set(ranked[:len(lineup.SLOTS)]) | reserve

    out = []
    for pid in ids:
        if pid in protected:
            continue
        row = ctx["by_id"].get(pid)
        if not row:
            continue
        v = hold_value(row, ctx, mine=mine)
        # In patch mode a hole in the lineup is a certain loss this week and an
        # inheritance is a maybe, so the floor and the rising-role premium both
        # yield -- but only far enough to reach the cheapest bodies we hold.
        if mine and ctx["mode"] != "patch" and v > DROP_FLOOR:
            continue
        out.append({"row": row, "value": v})
    return sorted(out, key=lambda d: d["value"])


def candidates(ctx: dict, waivers: bool, pos: set[str] | None = None) -> list[dict]:
    """Available players, best first, restricted to one channel.

    `waivers=True` returns only players still sitting on waivers; False returns
    only those free for the taking. Getting this partition right is what stops
    the bot bidding FAAB on somebody it could have had for nothing -- and in
    2025 this league ran 306 free-agent adds against 93 waiver wins, so the free
    side is where most of the volume actually is.
    """
    onw = ctx["on_waivers"]
    out = []
    for row in ctx["available"]:
        if (row["player_id"] in onw) != waivers:
            continue
        if pos and (row.get("pos") or "") not in pos:
            continue
        v, real = value.value_of(row, ctx["week"])
        out.append({"row": row, "value": v, "real": real})
    return sorted(out, key=lambda d: (-d["value"], d["row"]["player_id"]))


def deny_value(ctx: dict, pid: str) -> tuple[float, str]:
    """How much a free agent would improve the best-placed opponent.

    The blocking test, and the reason it is priority THREE: this is a benefit to
    us only in the sense that it is a cost to somebody else, and it is priced on
    their bench, which we can only estimate.
    """
    row = ctx["by_id"].get(pid)
    if not row:
        return 0.0, ""
    v, _ = value.value_of(row, ctx["week"])
    best, who = 0.0, ""
    for r in season.live_rosters(ctx["league_id"]):
        if r.get("owner_id") == ctx["roster"].get("owner_id"):
            continue
        theirs = droppables(ctx, roster=r)
        if not theirs:
            continue
        gain = v - theirs[0]["value"]
        if gain > best:
            best, who = gain, f"roster {r['roster_id']}"
    return round(best, 1), who


# -------------------------------------------------------------------- channels

def _need_positions(ctx: dict) -> set[str]:
    """Positions a patch has to fill, from the actual empty and unstartable slots."""
    need = set()
    for h in holes(ctx):
        for slot in h["empty"] + h["unstartable"]:
            need |= set(lineup.SLOT_ELIGIBLE.get(slot, set()))
    return need


def plan_free(ctx: dict) -> list[dict]:
    """Instant adds. One proposal per roster slot we are willing to turn over."""
    mode = ctx["mode"]
    need = _need_positions(ctx) if mode == "patch" else None
    if mode == "patch" and not need:
        return []
    drops = droppables(ctx)
    pool = candidates(ctx, waivers=False, pos=need)
    if mode == "patch":
        # ONE ADD PER HOLE, not one per eligible POSITION. A single SUPER_FLEX
        # gap is eligible for QB, RB, WR and TE, so counting positions would
        # turn over four roster spots to fill one slot -- and patch is the mode
        # that runs unattended every morning and is exempt from the blackout,
        # which makes it the worst possible place to be over-eager.
        gaps = max((len(h["empty"]) + len(h["unstartable"]) for h in holes(ctx)),
                   default=0)
        slots = min(gaps, MAX_SLOTS_TO_TURN_OVER)
    elif mode == "fill":
        slots = ctx["slots"]["open"]
    else:
        slots = MAX_SLOTS_TO_TURN_OVER
    used, out = set(), []

    if mode == "fill" and slots <= 0:
        return []

    if mode == "block":
        for d in drops[:1]:  # blocking only ever spends the LAST turnable slot
            for c in pool:
                pid = c["row"]["player_id"]
                if pid in used:
                    continue
                deny, who = deny_value(ctx, pid)
                if deny < BLOCK_MIN_DENY:
                    continue
                used.add(pid)
                out.append({"add": c["row"], "drop": d["row"],
                            "gain": round(deny, 1), "add_value": c["value"],
                            "drop_value": d["value"], "real": c["real"],
                            "why": f"denies {who} a {deny:.0f}-point upgrade"})
                break
        return out

    if mode == "patch":
        # A patch is not an upgrade decision: an empty slot scores zero, so
        # anyone startable beats it and neither the ordinary bar nor the
        # simulator applies. Legality is not a matter of degree, and this is the
        # branch that runs unattended every morning -- it stays cheap and blunt.
        for d in drops[:max(1, slots)]:
            for c in pool:
                pid = c["row"]["player_id"]
                if pid in used:
                    continue
                used.add(pid)
                out.append({"add": c["row"], "drop": d["row"],
                            "gain": round(c["value"] - d["value"], 1),
                            "add_value": c["value"], "drop_value": d["value"],
                            "real": c["real"],
                            "why": "fills an unfillable starting slot"})
                break
        return out

    # ORDINARY UPGRADES ARE PRICED BY SIMULATION, not by subtracting two absolute
    # season totals across different positions. That arithmetic put a defence and
    # a fourth receiver on the same axis and scored a +1.6 swap at +87.
    P = priced(ctx)
    for drop in P["drops"][:max(1, slots)]:
        o = best_free([x for x in P["free"] if x["add"] not in used], drop,
                      fill=(mode == "fill"))
        if not o:
            continue
        used.add(o["add"])
        out.append(_option_row(ctx, P["board"], o))
    return out


def _option_row(ctx: dict, board, o: dict, why: str = "") -> dict:
    """One priced option in the shape render_free and run() already expect."""
    add_row = ctx["by_id"].get(o["add"]) or {"player_id": o["add"],
                                             "name": board.S[o["add"]]["name"],
                                             "pos": board.S[o["add"]]["pos"]}
    if o["drop"] is None:
        drop_row = {"player_id": None, "name": "(open roster spot)", "pos": "--"}
        drop_val = 0.0
    else:
        drop_row = ctx["by_id"].get(o["drop"]) or {"player_id": o["drop"],
                                                   "name": board.S[o["drop"]]["name"],
                                                   "pos": board.S[o["drop"]]["pos"]}
        drop_val = round(board.drop_price(o["drop"])[0], 1)
    kind = "starting slot" if o["starter"] else "bench, judged on the ceiling"
    return {"add": add_row, "drop": drop_row,
            "gain": round(o["gain"], 1),
            "add_value": round(o["gain"], 1),
            "drop_value": drop_val,
            "real": True,
            "se": round(o["se"], 2), "ceiling": round(o["ceiling"], 1),
            "why": why or f"{kind}; +/- {o['se']:.1f}, ceiling {o['ceiling']:.1f}"}


SHORTLIST = 10


def priced(ctx: dict) -> dict:
    """Both channels priced against the same drops, by the same simulator.

    THE FREE BOARD IS THE OPPORTUNITY COST OF A BID, and this is the only place
    that can see it. Planning the two channels separately is how the bot came to
    bid $8 on the forty-first-best free agent while the best one sat there for
    nothing: plan_claims only ever looked at players on waivers, so "is there a
    better man available for free" was a question nobody asked.

    Returns {"free": [...], "wire": [...], "board": Board}, every option carrying
    the gain, its standard error, and whether it fills a starting slot -- which
    decides whether it is judged on its mean or its ceiling.
    """
    from robo import marginal
    b0 = marginal.board(ctx["league_id"])

    def shortlist(waivers: bool) -> list[str]:
        """The top of one channel, restricted to men the simulator can price.

        K AND DEF ARE FILTERED OUT HERE, not silently dropped later. They rank
        at the top of any absolute-value ordering -- a defence that starts all
        season sums to 118 -- so an unfiltered top ten was eight kickers and
        defences and left the planner comparing exactly two real candidates.
        They are refillable from the wire every week and cancel out of the
        simulation by design; taking a shortlist slot as well was the same
        mistake twice.
        """
        out = []
        for c in candidates(ctx, waivers=waivers):
            pid = c["row"]["player_id"]
            if pid in b0.S and len(out) < SHORTLIST:
                out.append(pid)
        return out

    if ctx["mode"] == "fill":
        # An open roster spot has no incumbent, so there is nothing to price the
        # candidate against and nothing to give up. None carries that through.
        drops = [None] * max(1, ctx["slots"]["open"])
    else:
        drops = [d["row"]["player_id"] for d in droppables(ctx)][:MAX_SLOTS_TO_TURN_OVER]
    free, wire = shortlist(False), shortlist(True)
    b = marginal.Board(ctx["league_id"], extra=free + wire)
    return {"board": b, "drops": drops,
            "free": marginal.price_options(b, drops, free),
            "wire": marginal.price_options(b, drops, wire)}


def clears(o: dict, fill: bool = False) -> bool:
    """Is this option worth a transaction at all?

    Two bars, and which applies depends on the slot. A STARTING upgrade is judged
    on the mean, and must beat MIN_GAIN_TO_ADD. A BENCH spot is judged on the
    ceiling against HIT_POINTS -- the median realised contribution of an add in
    this league -- because down there the mean is ranking noise and would always
    prefer a safe body to a man who might become something.

    Both are also required to beat the simulator's own noise. A gap inside its
    standard error is not a ranking, whatever it is a ranking of.

    FILLING AN EMPTY SPOT IS NOT AN UPGRADE and does not face the upgrade bar.
    Nobody is being displaced, so there is no incumbent to beat and no cost to
    weigh -- an empty roster spot scores zero every week it stays empty. The
    ceiling bar still applies, because the question of WHICH man to put there is
    still a bench question, and the noise gate still applies because a number
    inside its own error is not a reason.
    """
    from robo import marginal
    if o["gain"] <= NOISE_MULTIPLE * o["se"]:
        return False
    if fill or not o["starter"]:
        return o["ceiling"] >= marginal.HIT_POINTS
    return o["gain"] >= MIN_GAIN_TO_ADD


def best_free(opts: list[dict], drop, fill: bool = False) -> dict | None:
    """The best thing available for nothing, for this slot."""
    fits = [o for o in opts if o["drop"] == drop and clears(o, fill=fill)]
    return max(fits, key=lambda o: o["ceiling"] if fill else o["gain"]) if fits else None


def plan_claims(ctx: dict) -> list[dict]:
    """The FAAB slate: a ranked priority list per slot, not a single claim.

    Every claim in one slot's list names the SAME drop. Sleeper works them in
    seq order; the first winner takes the slot and the rest bounce off a player
    who is no longer on our roster, at no cost. That is the whole point -- we
    get our best AVAILABLE outcome instead of our best guess.

    The rungs are priced as a DESCENDING LADDER off this league's own bid
    history rather than as one bid repeated: the top rung pays a real price for
    the man we want, and the cheap rungs sit where the record says claims still
    convert. See robo/faab.py, including why P(win | bid) is not estimated.
    """
    P = priced(ctx)
    board = P["board"]
    slates, used = [], set()
    for drop in P["drops"]:
        # WHAT THE FREE BOARD WOULD HAVE GIVEN US FOR THIS SLOT is the price of
        # bidding at all. FAAB buys the DIFFERENCE between the best claim and the
        # best free agent, never the claim's whole value, and a claim that cannot
        # beat a free man is not worth a dollar however good he looks alone.
        alt = best_free(P["free"], drop)
        floor_gain = alt["gain"] if alt else 0.0
        picks = []
        for o in sorted((x for x in P["wire"] if x["drop"] == drop),
                        key=lambda x: -x["gain"]):
            if len(picks) >= SLATE_DEPTH or o["add"] in used:
                continue
            excess = o["gain"] - floor_gain
            if not clears(o) or excess <= NOISE_MULTIPLE * o["se"]:
                continue
            row = _option_row(ctx, board, o)
            row["gain"] = round(excess, 1)
            row["over_free"] = alt["add"] if alt else None
            picks.append(row)
        if not picks:
            continue
        bids = faab.ladder(ctx["week"], [p["gain"] for p in picks], ctx["faab"])
        for p, b in zip(picks, bids):
            p["bid"] = int(b)
        used |= {p["add"]["player_id"] for p in picks}
        slates.append({"drop": picks[0]["drop"],
                       "drop_value": picks[0]["drop_value"], "claims": picks})

    # seq is assigned across ALL slates by descending bid, matching what this
    # league's own transactions show. Sleeper works our claims in that order, so
    # the most valuable one gets first refusal on the budget.
    flat = [(s, c) for s in slates for c in s["claims"]]
    flat.sort(key=lambda sc: (-sc[1]["bid"], -sc[1]["gain"], sc[1]["add"]["player_id"]))
    for i, (_, c) in enumerate(flat):
        c["seq"] = i
    return slates


# ------------------------------------------------------------------- payloads

def free_payload(add_id: str, drop_id, roster_id: int) -> dict:
    """Exactly what sleeper_write.free_agent_transaction would send.

    A None drop is an ADD INTO AN EMPTY SPOT and must send no drop keys at all
    rather than a null one -- fill mode has no incumbent, and a null in the drop
    array is not the same request as an absent array.
    """
    out = {"k_adds": [add_id], "v_adds": [roster_id]}
    if drop_id is not None:
        out.update({"k_drops": [drop_id], "v_drops": [roster_id]})
    return out


def claim_payload(add_id: str, drop_id: str, roster_id: int, bid: int) -> dict:
    """Exactly what sleeper_write.submit_waiver_claim would send.

    The `waiver_bid` key is confirmed -- it was read off this league's own
    completed waiver transactions. What has never executed is whether this
    parallel-array form reaches Sleeper's settings blob intact, which is why
    every submitted claim is read back (see verify_bid).
    """
    return {"k_adds": [add_id], "v_adds": [roster_id],
            "k_drops": [drop_id], "v_drops": [roster_id],
            "k_settings": ["waiver_bid"], "v_settings": [bid]}


def verify_bid(add_id: str, expected: int, ctx: dict) -> tuple[bool, str]:
    """Did the bid we sent actually land on the claim Sleeper recorded?

    A submitted claim is PENDING until Sleeper processes waivers, and pending
    claims DO NOT APPEAR in the REST /transactions/<week> feed -- that feed only
    carries settled ones. Reading it back therefore reported "no recorded claim
    contains this player" about a claim that had landed perfectly, which is the
    worst possible false alarm: this check exists to stop a slate, so a false
    negative kills every remaining claim on a healthy run. Verified 4 Sep 2026
    on a real accidental submission, which the GraphQL query below found
    immediately and the REST feed never saw at all.

    The bid encoding itself is CONFIRMED by that same submission -- the
    parallel-array k_settings/v_settings form came back as
    `settings: {"waiver_bid": 1}`, so it does reach Sleeper intact. The check
    stays because the failure it guards against is silent: a mis-encoded bid
    reads as 0, the claim still looks submitted, and the player goes to anyone
    who bid a dollar.
    """
    from robo import sleeper_write as sw
    q = ('query league_transactions { league_transactions(league_id: "%s", '
         'roster_id: %d, status: "pending", limit: 25) '
         '{ transaction_id status type adds drops settings } }'
         % (ctx["league_id"], ctx["roster"]["roster_id"]))
    try:
        rows = sw.gql("league_transactions", q, {})["league_transactions"] or []
    except Exception as e:
        return False, f"could not read the claim back: {type(e).__name__}"
    for t in rows:
        if t.get("type") != "waiver" or add_id not in (t.get("adds") or {}):
            continue
        got = (t.get("settings") or {}).get("waiver_bid")
        if got is None:
            return False, "claim recorded with NO waiver_bid in its settings"
        if int(got) != int(expected):
            return False, f"bid encoded as {got}, sent {expected}"
        return True, f"bid {got} confirmed on the pending claim"
    return False, "no pending claim contains this player"


# --------------------------------------------------------------------- output

def _tag(real: bool) -> str:
    return "" if real else "  [PROVISIONAL VALUATION]"


def _header(ctx: dict) -> list[str]:
    sl = ctx["slots"]
    L = [f"  mode {ctx['mode'].upper()}   week {ctx['week']}   "
         f"roster {sl['active']}/{sl['roster_max']} ({sl['open']} open), "
         f"IR {sl['ir_used']}/{sl['ir_slots']}, ${ctx['faab']} FAAB"]
    h = ctx["hours_to_kickoff"]
    L.append(f"  next kickoff in {h:.1f}h" if h is not None
             else "  next kickoff unknown")
    hs = holes(ctx)
    for x in hs:
        gaps = ", ".join(x["empty"] + [f"{s} (unstartable)" for s in x["unstartable"]])
        L.append(f"  !! week {x['week']} cannot be filled: {gaps}")
    return L


def render_free(ctx: dict, plans: list[dict]) -> str:
    L = [f"FREE AGENTS - {len(ctx['available']) - len(ctx['on_waivers'])} available "
         f"now, {len(ctx['on_waivers'])} still on waivers"] + _header(ctx)
    block = blacked_out(ctx)
    if block:
        L.append(f"  BLACKED OUT: {block}")
        return "\n".join(L)
    if not plans:
        from robo import marginal
        L.append("  nothing clears the bar: a starting upgrade must add "
                 f"{MIN_GAIN_TO_ADD:g}+ points to the simulated lineup, a bench "
                 f"spot must reach a ceiling of {marginal.HIT_POINTS:g}, and both "
                 f"must beat {NOISE_MULTIPLE:g}x the simulator's own error"
                 if ctx["mode"] == "ros" else "  nothing to do in this mode")
    for p in plans:
        L.append(f"  ADD  {p['add']['name']:<24} {p['add']['pos']:<4} "
                 f"{p['add_value']:>7.1f}{_tag(p['real'])}")
        L.append(f"  DROP {p['drop']['name']:<24} {p['drop']['pos']:<4} "
                 f"{p['drop_value']:>7.1f}   gain {p['gain']:+.1f}"
                 + (f"   {p['why']}" if p.get("why") else ""))
    return "\n".join(L)


def render_claims(ctx: dict, slates: list[dict]) -> str:
    L = [f"WAIVER SLATE - {len(ctx['on_waivers'])} player(s) on waivers"] + _header(ctx)
    block = blacked_out(ctx)
    if block:
        L.append(f"  BLACKED OUT: {block}")
        return "\n".join(L)
    if not slates:
        L.append("  no claim clears the bar")
    for s in slates:
        L.append(f"  slot freed by dropping {s['drop']['name']} "
                 f"({s['drop_value']:.1f}) - priority list, first winner takes it:")
        for c in s["claims"]:
            L.append(f"    seq {c['seq']:<3} ${c['bid']:<4} "
                     f"{c['add']['name']:<24} {c['add']['pos']:<4} "
                     f"{c['add_value']:>7.1f}  gain {c['gain']:+.1f}{_tag(c['real'])}")
    total = sum(c["bid"] for s in slates for c in s["claims"])
    if slates:
        L.append(f"  worst case if every list's top claim wins: "
                 f"{len(slates)} move(s); a single slot can only ever spend once, "
                 f"so the ${total} above is not a total commitment")
    return "\n".join(L)


# ------------------------------------------------------------------------ run

def _record(kind: str, title: str, decision: str, why: str, data: dict) -> None:
    from robo import decisions
    try:
        decisions.record(kind, title, decision, why, data=data)
    except Exception as e:  # a published record must never cost us the move
        print(f"  ** decision log failed ({type(e).__name__}); the move stands")


def run(channel: str, apply: bool = False, league_id: str = LEAGUE_ID_2026,
        mode: str = "ros", verbose: bool = True) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    ctx = _context(league_id, mode)
    block = blacked_out(ctx)

    if channel == "free":
        plans = [] if block else plan_free(ctx)
        text = render_free(ctx, plans)
        payloads = [free_payload(p["add"]["player_id"], p["drop"]["player_id"],
                                 ctx["roster"]["roster_id"]) for p in plans]
    else:
        plans = [] if block else plan_claims(ctx)
        text = render_claims(ctx, plans)
        payloads = [claim_payload(c["add"]["player_id"], s["drop"]["player_id"],
                                  ctx["roster"]["roster_id"], c["bid"])
                    for s in plans for c in s["claims"]]

    if verbose:
        print(text)
    out = {"channel": channel, "mode": mode, "week": ctx["week"], "plans": plans,
           "payloads": payloads, "applied": False, "submitted": [],
           "gated": not value.may_submit(), "blackout": block}

    if not value.may_submit():
        # The gate. Not a flag, not a setting -- a constant in value.py, so
        # turning this bot loose on the roster takes a commit. It is SEPARATE
        # from whether the valuation is real, so this dry run prints the numbers
        # the bot would actually have acted on rather than a stand-in nobody
        # could sensibly review.
        if verbose:
            print("\n  ** " + value.GATE_MESSAGE)
            if apply:
                print("  ** --apply was requested and is REFUSED.")
        return out
    if not apply or block or not plans:
        return out

    from robo import sleeper_write as sw
    rid = ctx["roster"]["roster_id"]
    if channel == "free":
        for p in plans:
            add, drop = p["add"], p["drop"]
            try:
                sw.free_agent_transaction(
                    {add["player_id"]: rid},
                    {drop["player_id"]: rid} if drop.get("player_id") else None,
                    league_id)
            except Exception as e:
                print(f"  ** ADD FAILED {add['name']}: {type(e).__name__}: {e}")
                continue
            out["submitted"].append({"add": add["name"], "drop": drop["name"]})
            _record("free-agent", f"Signed {add['name']}, released {drop['name']}",
                    f"{add['name']} ({add['pos']}) in, {drop['name']} out.",
                    f"Rest-of-season value {p['add_value']:.1f} against "
                    f"{p['drop_value']:.1f} held, a gain of {p['gain']:+.1f} "
                    f"in {mode} mode."
                    + (f" {p['why']}." if p.get("why") else ""),
                    {"add": add["player_id"], "drop": drop["player_id"],
                     "mode": mode, "week": ctx["week"], "gain": p["gain"]})
    else:
        for s in plans:
            for c in sorted(s["claims"], key=lambda x: x["seq"]):
                add = c["add"]
                try:
                    sw.submit_waiver_claim({add["player_id"]: rid},
                                           {s["drop"]["player_id"]: rid},
                                           c["bid"], league_id)
                except Exception as e:
                    print(f"  ** CLAIM FAILED {add['name']}: {type(e).__name__}: {e}")
                    continue
                ok, why = verify_bid(add["player_id"], c["bid"], ctx)
                out["submitted"].append({"add": add["name"], "bid": c["bid"],
                                         "verified": ok, "why": why})
                _record("waiver", f"Claimed {add['name']} for ${c['bid']}",
                        f"${c['bid']} on {add['name']} ({add['pos']}), "
                        f"dropping {s['drop']['name']} if it wins.",
                        f"Rest-of-season gain {c['gain']:+.1f}; priority "
                        f"{c['seq']}. Bid check: {why}.",
                        {"add": add["player_id"], "drop": s["drop"]["player_id"],
                         "bid": c["bid"], "seq": c["seq"], "verified": ok,
                         "week": ctx["week"]})
                if not ok:
                    # Stop the whole run. The bid encoding is the one write in
                    # this project that has never executed, and if it is wrong
                    # every later claim in the slate is wrong the same way.
                    msg = (f"WAIVER BID NOT CONFIRMED on {add['name']}: {why}. "
                           "Stopping the slate; no further claims submitted.")
                    print(f"  ** {msg}")
                    try:
                        from robo import alerts
                        alerts.blast(msg, key="waiver-bid-unverified")
                    except Exception:
                        pass
                    out["applied"] = True
                    return out
    out["applied"] = True
    return out


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--free", action="store_true", help="instant wire adds")
    g.add_argument("--claims", action="store_true", help="the FAAB slate")
    ap.add_argument("--mode", default="ros", choices=MODES)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--league", default=LEAGUE_ID_2026)
    ap.add_argument("--payloads", action="store_true",
                    help="print the exact GraphQL variables that would be sent")
    args = ap.parse_args()
    res = run("free" if args.free else "claims", apply=args.apply,
              league_id=args.league, mode=args.mode)
    if args.payloads:
        print("\nGraphQL variables that would be sent:")
        print(json.dumps(res["payloads"], indent=1))


if __name__ == "__main__":
    main()
