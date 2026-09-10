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

ADDS AND DROPS ARE PRICED OFF DIFFERENT NUMBERS, ON PURPOSE. An add is judged
on `mean` -- what he is worth to us. A drop is judged on `hold` -- `mean` plus
what he stands to inherit if the man ahead of him goes down. Using one number
for both is exactly how a bot cuts a rookie in October and watches somebody else
start him in December. See robo/ros.py.

NO LONG-HORIZON MOVE NEAR KICKOFF. A rest-of-season swap made forty minutes
before the early games is a decision taken on this week's panic with the season's
consequences, and there is no reason it could not have been made on Tuesday. So
Everything but `patch` refuses inside ROS_MOVE_BLACKOUT_H of the next kickoff,
and says so out loud -- a silent no-op would be indistinguishable from "nothing
cleared".
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
import time

from robo import DATA, LEAGUE_ID_2026, faab, lineup, season, settings, value, vegas
from robo import sleeper_read as api

# How much a candidate must add to our STARTING LINEUP, across simulated
# seasons, before a transaction is worth making -- ON TOP of being larger than
# the simulator's own error. At zero, NOISE_MULTIPLE x se is the only filter and
# the rule is simply: make the move when the gain is real.
#
# IT WAS 15.0, WHICH WAS THE OLD UNITS, and at that level it was not a bar but
# an off switch. `gain` used to be a difference of two absolute season totals --
# tens to hundreds of points -- and is now the change in our optimal starting
# lineup, where the same proposal prices at 1.5 instead of 51. Nothing the
# simulator produces for a wire add comes close to 15, so no starting upgrade
# could ever be made: with our quarterback concussed in a superflex league, his
# recalibrated backup priced at +2.55 and was refused.
#
# There is deliberately no fitted level here. This league's history records what
# an add went on to SCORE in a starting slot, which is not a marginal quantity --
# the man he displaced would have scored too, and that counterfactual is not in
# the data. Inventing a number to stand in for it is what produced the 15.
MIN_GAIN_TO_ADD = 0.0

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

# Hours before the next kickoff inside which `ros` stops running. Six covers a
# Sunday morning: the daily job fires at 07:00 local and the early games start
# at 10:00 Phoenix time.
ROS_MOVE_BLACKOUT_H = 6.0

# How far ahead to look for a week we cannot field a legal lineup in. Three is
# about how far a bye is worth pre-empting -- further out and the wire will have
# turned over before it matters.
BYE_LOOKAHEAD_WEEKS = 3

settings.apply(__name__, globals())

MODES = ("patch", "fill", "stream", "ros")
AUDIT_CACHE = DATA / "marginal_decision.json"
AUDIT_SCHEMA = 1


# ------------------------------------------------------------------ evaluation

def _context(league_id: str = LEAGUE_ID_2026, mode: str = "ros",
             board: list[dict] | None = None,
             expected_table: dict | None = None,
             p_playoffs: float | None = None) -> dict:
    if board is None:
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
        # Audit callers can pin the exact cached valuation under the whole run.
        # Production omits this and keeps expected.load()'s freshness gate.
        "expected_table": expected_table,
        "p_playoffs": p_playoffs,
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
            # A LOCKED STARTER STILL OCCUPIES HIS SLOT. optimize() refuses to
            # newly assign a locked man -- Sleeper would reject the write -- so
            # without the pins our own locked defence vanished from the board
            # and this reported the DEF slot as an unfillable hole, which sent
            # patch mode out to sign a second defence we did not need.
            # Future weeks are unaffected: nothing is locked in them, so
            # pin_locked returns {}.
            filled, _ = lineup.optimize(
                cands, lineup.pin_locked(cands, ctx["roster"].get("starters") or []))
        except Exception:
            continue
        empty = [lineup.SLOTS[i] for i, p in enumerate(filled) if not p]
        unstartable = [lineup.SLOTS[i] for i, p in enumerate(filled)
                       if p and not lineup.startable(p)]
        if w != ctx["week"]:
            # A KICKER OR DEFENCE BYE IS NOT WORTH PRE-EMPTING. Three weeks of
            # lookahead is right where the wire turns over and a replacement may
            # not be there later; it is wrong at these two, where twenty-one
            # defences and a shelf of kickers sit unowned all season. Left in, it
            # signs a second kicker in week 3 to cover a week-6 bye and pays a
            # bench spot for three weeks to solve a problem that solves itself.
            empty = [x for x in empty if x not in ("K", "DEF")]
            unstartable = [x for x in unstartable if x not in ("K", "DEF")]
        if empty or unstartable:
            out.append({"week": w, "empty": empty, "unstartable": unstartable})
    return out


def hold_value(row: dict, ctx: dict) -> float:
    """What we give up by cutting him, priced by SIMULATION.

    ros.hold made Carson Beck the cheapest man on our roster to drop at 0.4
    while the simulator prices him at 37.5, above six of our starters -- the
    ordering was inverted, and that is the bug that produced "the bot tried to
    drop Carson Beck".
    """
    v, _ = value.hold_of(row, ctx["week"])
    return v


def droppables(ctx: dict) -> list[dict]:
    """Cached, because the planners ask for it repeatedly.

    Each entry is a simulated drop price at ~6s, so seven of them is 39 seconds,
    and plan_free, plan_claims and priced all ask independently. The cache lives
    on ctx rather than in a module-level dict so it cannot outlive the run that
    built it -- a stale drop price would be a silently wrong decision, not a
    slow one.
    """
    if "_droppables" not in ctx:
        ctx["_droppables"] = _droppables(ctx)
    return ctx["_droppables"]


def _droppables(ctx: dict) -> list[dict]:
    """Who could be cut, worst first, priced on HOLD.

    Two independent guards, because they fail differently. A current starter is
    excluded outright -- if the optimizer is starting him this week, cutting him
    is incoherent regardless of what any number says. DROP_FLOOR is the backstop
    for the number itself being wrong.
    """
    r = ctx["roster"]
    ids = list(r.get("players") or [])
    reserve = set(r.get("reserve") or [])
    protected = ctx["starters"] | reserve

    out, audit = [], []
    for pid in ids:
        raw = ctx["players"].get(pid) or {}
        name = api.player_name(ctx["players"], pid)
        pos = raw.get("position") or (ctx["by_id"].get(pid) or {}).get("pos")
        if pid in protected:
            audit.append({"player_id": pid, "player": name, "pos": pos,
                          "cost_to_drop": None, "eligible": False,
                          "why": "current starter" if pid in ctx["starters"]
                                 else "on reserve"})
            continue
        row = ctx["by_id"].get(pid)
        if not row:
            audit.append({"player_id": pid, "player": name, "pos": pos,
                          "cost_to_drop": None, "eligible": False,
                          "why": "absent from the decision board"})
            continue
        v = hold_value(row, ctx)
        # In patch mode a hole in the lineup is a certain loss this week and an
        # inheritance is a maybe, so the floor and the rising-role premium both
        # yield -- but only far enough to reach the cheapest bodies we hold.
        eligible = ctx["mode"] == "patch" or v <= DROP_FLOOR
        audit.append({"player_id": pid, "player": row.get("name") or name,
                      "pos": row.get("pos") or pos,
                      "cost_to_drop": round(v, 3), "eligible": eligible,
                      "why": ("eligible drop candidate" if eligible else
                              f"above the {DROP_FLOOR:g} drop floor")})
        if not eligible:
            continue
        out.append({"row": row, "value": v})
    ctx["_drop_audit"] = audit
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
        v, real = value.value_of(row, ctx["week"],
                                 table=ctx.get("expected_table"))
        out.append({"row": row, "value": v, "real": real})
    return sorted(out, key=lambda d: (-d["value"], d["row"]["player_id"]))


# -------------------------------------------------------------------- channels

def _need_positions(ctx: dict) -> set[str]:
    """Positions a patch has to fill, from the actual empty and unstartable slots."""
    need = set()
    for h in holes(ctx):
        for slot in h["empty"] + h["unstartable"]:
            need |= set(lineup.SLOT_ELIGIBLE.get(slot, set()))
    return need


def submit_free(ctx: dict, plans: list[dict], out: dict,
                league_id: str = LEAGUE_ID_2026) -> dict:
    """Send free-agent adds and write the public record. ONE submission path.

    Factored out of run() so the defence streamer can reach it without growing a
    parallel one. Everything above it in run() -- the gate, the blackout, the
    refusal to act -- has already happened by the time this is called, and a
    second copy of any of that is the thing most likely to forget the gate.
    """
    from robo import sleeper_write as sw
    rid = ctx["roster"]["roster_id"]
    mode = ctx.get("mode", "ros")
    for p in plans:
        add, drop = p["add"], p["drop"]
        try:
            sw.free_agent_transaction(
                {add["player_id"]: rid} if add.get("player_id") else None,
                {drop["player_id"]: rid} if drop.get("player_id") else None,
                league_id)
        except Exception as e:
            print(f"  ** ADD FAILED {add['name']}: {type(e).__name__}: {e}")
            out.setdefault("failed", []).append(
                {"add": add["name"], "drop": drop["name"],
                 "why": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        out["submitted"].append({"add": add["name"], "drop": drop["name"]})
        _record("free-agent", f"Signed {add['name']}, released {drop['name']}",
                f"{add['name']} ({add['pos']}) in, {drop['name']} out.",
                f"Rest-of-season value {p['add_value']:.1f} against "
                f"{p['drop_value']:.1f} held, a gain of {p['gain']:+.1f} "
                f"in {mode} mode."
                + (f" {p['why']}." if p.get("why") else ""),
                {"add": add.get("player_id"), "drop": drop.get("player_id"),
                 "mode": mode, "week": ctx["week"], "gain": p["gain"]})
    # WHAT REACHED SLEEPER, NOT WHAT WE TRIED. This was an unconditional True
    # sitting outside the loop, so a run in which every mutation raised still
    # reported applied -- and cascade._moves renders that as a clean
    # "2 add(s): Name, Name" into inseason.log with `submitted` empty. The
    # decision log was never falsified (record() sits after the continue); the
    # summary a human actually reads was.
    out["applied"] = bool(out["submitted"])
    return out


def plan_strand(ctx: dict) -> list[dict]:
    """Free a slot for a man Sleeper will not let stay on reserve.

    THE ANNOYANCE THIS EXISTS FOR. A player parked on IR with an `Out`
    designation loses that designation once the week's games finish. He is then
    not IR-eligible, and Sleeper BLOCKS EVERY ROSTER MOVE -- adds, drops,
    claims, all of it -- until he is activated or cut. It is the one roster
    state that freezes the whole account.

    ir.py detects it correctly and refuses to resolve it, on the grounds that
    activating a man into a full roster means cutting somebody and a cut is a
    valuation. That was right, and its handoff was wrong: it says to run
    `moves --mode patch`, and patch fires only on holes(), which is about
    UNFILLABLE STARTING SLOTS. A stranded man on reserve leaves the lineup
    perfectly legal, so patch returned nothing and the deadlock sat there --
    ir pointing at moves, moves seeing no hole, Sleeper refusing everything.

    THE CUT IS NOW ANSWERABLE, which is what changed. The question is simply who
    is cheapest to lose: the returning man himself, or the cheapest body on the
    active roster. Both are priced by the same simulator, so this compares two
    drop prices and takes the smaller. Dropping the stranded man is often
    correct and was never even considered before.
    """
    from robo import ir, marginal
    p = ir.plan(ctx["league_id"])
    stuck = [b for b in p["blocked"] if b.get("player_id") in set(p["current"])]
    if not stuck:
        return []
    out, spent = [], set()
    for b in stuck:
        pid = b["player_id"]
        his = marginal.drop_price(pid, ctx["league_id"])
        cands = [d for d in droppables(ctx) if d["row"]["player_id"] not in spent]
        cheapest = cands[0] if cands else None
        row = ctx["by_id"].get(pid) or {"player_id": pid, "name": b["name"], "pos": "--"}
        if cheapest is None or his <= cheapest["value"]:
            out.append({"add": {"player_id": None, "name": "(nobody -- a cut, not a swap)",
                                "pos": "--"},
                        "drop": row, "gain": 0.0, "add_value": 0.0,
                        "drop_value": round(his, 1), "real": True,
                        "why": f"{b['status']}, so he cannot stay on reserve, and at "
                               f"{his:.1f} he is cheaper to lose than anyone we hold; "
                               f"cutting him unblocks the roster"})
            spent.add(pid)
            continue
        spent.add(cheapest["row"]["player_id"])
        out.append({"add": row, "drop": cheapest["row"],
                    "gain": round(his - cheapest["value"], 1),
                    "add_value": round(his, 1),
                    "drop_value": round(cheapest["value"], 1), "real": True,
                    "why": f"activating him off reserve ({b['status']}) needs an "
                           f"active slot; he is worth {his:.1f} against "
                           f"{cheapest['value']:.1f} for the cheapest body we hold"})
    return out


def plan_free(ctx: dict) -> list[dict]:
    """Instant adds. One proposal per roster slot we are willing to turn over."""
    mode = ctx["mode"]
    if mode == "patch":
        # A STRANDED RESERVE IS A LEGALITY PROBLEM TOO, and the more urgent one:
        # an unfillable lineup slot costs us one week's points, while a man
        # Sleeper will not let stay on reserve blocks every transaction on the
        # account until he moves. It is checked first for that reason.
        stranded = plan_strand(ctx)
        if stranded:
            return stranded
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


# How deep each channel's shortlist goes, per ordering. Two orderings, unioned:
# see priced().
#
# A COMPUTE BUDGET, NOT A MODELLING CLAIM -- which is why the near-term list is
# the deeper of the two. It exists to catch a man whose value sits in the next
# fortnight, and those rank below every steady producer by construction: a
# concussed quarterback's backup, the best free agent in the league for the week
# in question, came twelfth. A list that misses its own target by two places is
# too shallow for its purpose, and the cost is linear in the number of options
# priced.
SHORTLIST = 10
SHORTLIST_NEAR = 20


def priced(ctx: dict) -> dict:
    """Both channels priced, computed ONCE per context. See _priced.

    plan_free and plan_claims each need the whole priced board, and moves.run
    calls both -- so without this the same sixty hypotheticals are simulated
    twice, at six seconds each. Cached on ctx for the same reason droppables is.
    """
    if "_priced" not in ctx:
        ctx["_priced"] = _priced(ctx)
    return ctx["_priced"]


def _priced(ctx: dict) -> dict:
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
    # We only need membership for the shortlist. Building a whole simulated
    # Board here drew the same worlds a second time before the real candidate
    # Board below, and an audit of a stale odds file could also rebuild it.
    known = marginal.series(ctx["league_id"])["players"]

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
        pool = [c["row"]["player_id"] for c in candidates(ctx, waivers=waivers)
                if c["row"]["player_id"] in known]
        out = pool[:SHORTLIST]
        # THE SEASON TOTAL CANNOT SEE A FILL-IN. candidates() orders by
        # rest-of-season value, which is the right question for an upgrade we
        # will hold to January and the wrong one for the next three weeks: a man
        # whose value is concentrated in them has a small season total by
        # construction. With our quarterback out, his backup was the best free
        # agent in the league for the week in question and ranked 100th here.
        # A union rather than a replacement, so nothing the old ordering
        # surfaced is lost -- it can only ever add candidates to price.
        from robo import expected
        tbl = ctx.get("expected_table") or expected.load()
        near = sorted(pool,
                      key=lambda p: -value.near_value(p, ctx["week"], table=tbl))
        for pid in near[:SHORTLIST_NEAR]:
            if pid not in out:
                out.append(pid)
        ctx.setdefault("_shortlists", {})["waiver" if waivers else "free"] = [
            {"player_id": pid,
             "ros_rank": pool.index(pid) + 1,
             "near_rank": near.index(pid) + 1}
            for pid in out]
        return out

    if ctx["mode"] == "fill":
        # An open roster spot has no incumbent, so there is nothing to price the
        # candidate against and nothing to give up. None carries that through.
        drops = [None] * max(1, ctx["slots"]["open"])
    else:
        drops = [d["row"]["player_id"] for d in droppables(ctx)][:MAX_SLOTS_TO_TURN_OVER]
    free, wire = shortlist(False), shortlist(True)
    b = marginal.Board(ctx["league_id"], extra=free + wire,
                       p_playoffs=ctx.get("p_playoffs"))
    return {"board": b, "drops": drops,
            "free": marginal.price_options(b, drops, free),
            "wire": marginal.price_options(b, drops, wire)}


def clear_reason(o: dict, fill: bool = False) -> tuple[bool, str]:
    """The policy verdict and its exact gate, for decisions and their audit."""
    from robo import marginal
    noise = NOISE_MULTIPLE * o["se"]
    if o["gain"] <= noise:
        return False, (f"mean gain {o['gain']:+.2f} does not clear the "
                       f"{NOISE_MULTIPLE:g}x noise bar {noise:.2f}")
    if fill or not o["starter"]:
        if o["ceiling"] < marginal.HIT_POINTS:
            return False, (f"p90 ceiling {o['ceiling']:+.2f} is below the "
                           f"{marginal.HIT_POINTS:g}-point bench bar")
        return True, "clears the simulation-noise and bench-ceiling gates"
    if o["gain"] < MIN_GAIN_TO_ADD:
        return False, (f"mean gain {o['gain']:+.2f} is below the "
                       f"{MIN_GAIN_TO_ADD:g}-point starter bar")
    return True, "clears the simulation-noise and starter-mean gates"


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
    return clear_reason(o, fill=fill)[0]


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
        bids = faab.ladder(ctx["week"], [p["gain"] for p in picks], ctx["faab"],
                           [p["add"].get("pos") for p in picks])
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
    # Three states, and they read differently to a human: a number, a week whose
    # games are all played (infinity, and the blackout is clear), and a schedule
    # we could not read (None, treated as too close).
    if h is None:
        L.append("  next kickoff unknown")
    elif h == float("inf"):
        L.append("  this week's games are all played; no kickoff ahead")
    else:
        L.append(f"  next kickoff in {h:.1f}h")
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


def _distribution(values: list[float]) -> dict:
    """Compact shape plus the actual worlds used to produce it."""
    v = sorted(float(x) for x in values)
    if not v:
        return {"mean": 0.0, "p10": 0.0, "p50": 0.0, "p90": 0.0,
                "min": 0.0, "max": 0.0, "outcomes": []}

    def at(p: float) -> float:
        return v[min(len(v) - 1, int(p * len(v)))]

    return {"mean": round(sum(v) / len(v), 3),
            "p10": round(at(.10), 3), "p50": round(at(.50), 3),
            "p90": round(at(.90), 3), "min": round(v[0], 3),
            "max": round(v[-1], 3),
            "outcomes": [round(x, 3) for x in values]}


def _decision_snapshot(ctx: dict, out: dict) -> dict | None:
    """Serialize the exact marginal board already consumed by this run.

    This function performs no simulation. It only walks `ctx['_priced']`, whose
    board and option distributions were built before the policy selected a
    plan. Keeping the persistence on the production path makes the audit GUI a
    truthful reader rather than a second decision run with newer inputs.
    """
    P = ctx.get("_priced")
    if not P:
        return None
    from robo import marginal
    b = P["board"]

    def person(pid) -> dict:
        if pid is None:
            return {"player_id": None, "player": "(open roster spot)", "pos": "--"}
        p = b.S.get(pid) or {}
        row = ctx["by_id"].get(pid) or {}
        return {"player_id": pid,
                "player": p.get("name") or row.get("name") or
                          api.player_name(ctx["players"], pid),
                "pos": p.get("pos") or row.get("pos"),
                "team": p.get("team"), "role_rank": p.get("rank"),
                "standalone_ros": p.get("ros")}

    selected, claim_meta = set(), {}
    if out["channel"] == "free":
        selected = {(p["add"].get("player_id"), p["drop"].get("player_id"))
                    for p in out["plans"]}
    else:
        for slate in out["plans"]:
            for claim in slate["claims"]:
                key = (claim["add"].get("player_id"),
                       claim["drop"].get("player_id"))
                selected.add(key)
                claim_meta[key] = {"bid": claim.get("bid"),
                                   "sequence": claim.get("seq")}

    options = []
    for channel, rows in (("free", P["free"]), ("waiver", P["wire"])):
        for o in rows:
            ok, why = clear_reason(
                o, fill=ctx["mode"] == "fill" and channel == "free")
            over_free, free_name = None, None
            if channel == "waiver" and ok:
                alt = best_free(P["free"], o["drop"])
                floor = alt["gain"] if alt else 0.0
                over_free = o["gain"] - floor
                free_name = person(alt["add"])["player"] if alt else None
                if over_free <= NOISE_MULTIPLE * o["se"]:
                    ok = False
                    why = (f"gain over the best free option {over_free:+.2f} "
                           f"does not clear the {NOISE_MULTIPLE:g}x noise bar "
                           f"{NOISE_MULTIPLE * o['se']:.2f}")
                else:
                    why += f"; beats the best free option by {over_free:+.2f}"
            key = (o["add"], o["drop"])
            options.append({
                "channel": channel, "selected": key in selected,
                "add": person(o["add"]), "drop": person(o["drop"]),
                "mean_gain": round(o["gain"], 4), "se": round(o["se"], 4),
                "p90_ceiling": round(o["ceiling"], 4),
                "p_matters": round(o["p_hit"], 4),
                "starts": bool(o["starter"]), "first_start": o["first_start"],
                "start_weeks": o["start_weeks"], "start_points": o["start_pts"],
                "over_best_free": (round(over_free, 4)
                                     if over_free is not None else None),
                "best_free": free_name, "clears_policy": ok, "why": why,
                "bid": claim_meta.get(key, {}).get("bid"),
                "sequence": claim_meta.get(key, {}).get("sequence"),
                "outcomes": o.get("outcomes") or [],
            })

    # Summarise the exact candidate-excluded floor already held on this Board.
    # marginal.replaceability() answers a related general question by reading
    # the live league again; doing that here would let the audit drift from the
    # actual simulation between pricing and persistence.
    starts = {"QB": 2, "RB": 2, "WR": 2, "TE": 1}
    replace = []
    for pos, count in starts.items():
        ours, wire = [], []
        for week in b.weeks:
            held = sorted((marginal._base(b.S[pid], week) for pid in b.mine
                           if b.S.get(pid, {}).get("pos") == pos), reverse=True)
            if held[:count]:
                ours.append(sum(held[:count]) / len(held[:count]))
            if week in (b.repl or {}).get(pos, {}):
                wire.append(b.repl[pos][week])
        if ours and wire:
            own = sum(ours) / len(ours)
            first = sum(wire) / len(wire)
            replace.append({"pos": pos, "our_starters": own,
                            "wire_first": first, "wire_ratio": first / max(own, 1e-9)})
    floors = [{"week": w, "pos": pos, "points": round(points, 3)}
              for pos, by_week in sorted((b.repl or {}).items())
              for w, points in sorted(by_week.items())]
    roster = []
    drop_by_id = {r["player_id"]: r for r in ctx.get("_drop_audit", [])}
    for pid in ctx["roster"].get("players") or []:
        row = person(pid)
        row.update(drop_by_id.get(pid) or {
            "cost_to_drop": None, "eligible": False,
            "why": "not evaluated for this decision"})
        roster.append(row)

    return {
        "schema": AUDIT_SCHEMA, "computed": time.time(),
        "origin": out.get("origin") or "decision run",
        "channel": out["channel"], "mode": out["mode"], "week": out["week"],
        "inputs": {
            "expected_computed": b.expected_computed,
            "expected_schema": b.expected_schema,
            "playoff_computed": b.playoff_computed,
            "p_playoffs": b.p_playoffs,
            "roster_id": ctx["roster"].get("roster_id"),
            "roster_player_ids": list(ctx["roster"].get("players") or []),
            "faab": ctx["faab"], "slots": ctx["slots"],
            "hours_to_kickoff": ctx["hours_to_kickoff"],
        },
        "simulation": {
            "sims": b.sims, "seed": 0, "weeks": b.weeks,
            "weights": {str(w): b.weights[w] for w in b.weeks},
            "objective": "mean" if b.contender else f"p{marginal.UPSIDE_PCTL}",
            "contender_threshold": marginal.CONTENDER_ODDS,
            "baseline_score": round(b.score(b.base), 3),
            "baseline": _distribution(b.base),
            "hit_points": marginal.HIT_POINTS,
            "wire_min_availability": marginal.WIRE_MIN_AVAIL,
        },
        "policy": {
            "noise_multiple": NOISE_MULTIPLE,
            "minimum_starter_gain": MIN_GAIN_TO_ADD,
            "drop_floor": DROP_FLOOR,
            "max_slots": MAX_SLOTS_TO_TURN_OVER,
            "slate_depth": SLATE_DEPTH,
            "blackout_hours": ROS_MOVE_BLACKOUT_H,
        },
        "result": {
            "apply_requested": bool(out.get("apply_requested")),
            "gate_open": not out["gated"], "blackout": out["blackout"],
            "applied": out["applied"], "submitted": out["submitted"],
            "failed": out.get("failed") or [], "selected": out["plans"],
        },
        "roster": roster, "shortlists": ctx.get("_shortlists") or {},
        "replacement": replace, "replacement_by_week": floors,
        "options": options,
    }


def _save_decision_snapshot(ctx: dict, out: dict) -> None:
    """Best-effort audit persistence; a dashboard can never cost us a move."""
    try:
        snap = _decision_snapshot(ctx, out)
        if not snap:
            return
        tmp = AUDIT_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=1), encoding="utf-8")
        tmp.replace(AUDIT_CACHE)
    except Exception as e:
        print(f"  ** audit snapshot failed ({type(e).__name__}); the move stands")


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
           "gated": not value.may_submit(), "blackout": block,
           "apply_requested": bool(apply), "origin": "decision run"}
    _save_decision_snapshot(ctx, out)

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

    if channel == "free":
        out = submit_free(ctx, plans, out, league_id)
        _save_decision_snapshot(ctx, out)
        return out

    from robo import sleeper_write as sw
    rid = ctx["roster"]["roster_id"]
    if True:
        for s in plans:
            for c in sorted(s["claims"], key=lambda x: x["seq"]):
                add = c["add"]
                try:
                    sw.submit_waiver_claim({add["player_id"]: rid},
                                           {s["drop"]["player_id"]: rid},
                                           c["bid"], league_id)
                except Exception as e:
                    print(f"  ** CLAIM FAILED {add['name']}: {type(e).__name__}: {e}")
                    out.setdefault("failed", []).append(
                        {"add": add["name"], "bid": c["bid"],
                         "why": f"{type(e).__name__}: {str(e)[:120]}"})
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
                    out["applied"] = bool(out["submitted"])
                    _save_decision_snapshot(ctx, out)
                    return out
    # See submit_free: what reached Sleeper, not what we attempted.
    out["applied"] = bool(out["submitted"])
    _save_decision_snapshot(ctx, out)
    return out


def slate(kind: str, apply: bool = False,
          league_id: str = LEAGUE_ID_2026) -> dict:
    """One scheduled roster run: sweep IR, then make the pass.

    THE ORDERING BELONGS HERE, NOT IN A .VBS. Both wrappers chained
    `robo.ir --apply & robo.moves ...` with cmd's unconditional `&`, and that
    hand-written chain carried two defects at once: the `&` runs the second
    command even when the first died, and NEITHER wrapper passed `--apply` to
    robo.moves -- so the Tuesday waiver task whose own comment says "LIVE.
    Claims are submitted" planned a slate and threw it away, every week, and
    would have gone on doing so after the gate opened. Same class of bug the
    cascade was built to remove: an ordering that matters living in the
    schedule, where nothing tests it.

    ir first because reserve is three slots ON TOP of the seventeen-man roster,
    so an unswept IR is a roster cap that silently blocks every pickup and the
    slate would be built against the wrong number of open slots.

    A failed sweep does not stop the pass -- it is reported and the pass runs
    against whatever slots are genuinely open, which is strictly better than
    skipping the week.
    """
    from robo import ir
    out: dict = {"kind": kind, "applied": bool(apply)}
    try:
        out["ir"] = ir.run(apply=apply, league_id=league_id)
    except Exception as e:
        out["ir_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        print(f"  ** IR SWEEP FAILED: {out['ir_error']}")
        print("     continuing to the pass against the slots we can see")
    out["moves"] = run("claims" if kind == "claims" else "free",
                       apply=apply, league_id=league_id,
                       mode="ros" if kind == "ros" else "ros")
    return out


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--free", action="store_true", help="instant wire adds")
    g.add_argument("--claims", action="store_true", help="the FAAB slate")
    g.add_argument("--slate", choices=("ros", "claims"),
                   help="the scheduled run: sweep IR, then the pass. Owns the "
                        "ordering the .vbs wrappers used to chain by hand")
    ap.add_argument("--mode", default="ros", choices=MODES)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--league", default=LEAGUE_ID_2026)
    ap.add_argument("--payloads", action="store_true",
                    help="print the exact GraphQL variables that would be sent")
    args = ap.parse_args()
    if args.slate:
        return slate(args.slate, apply=args.apply, league_id=args.league)
    res = run("free" if args.free else "claims", apply=args.apply,
              league_id=args.league, mode=args.mode)
    if args.payloads:
        print("\nGraphQL variables that would be sent:")
        print(json.dumps(res["payloads"], indent=1))


if __name__ == "__main__":
    main()
