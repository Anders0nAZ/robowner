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
    python -m robo.moves --free --mode news    # react to an injury event, now

THE PRIORITY CHAIN IS LEXICOGRAPHIC, NOT A WEIGHTED SUM. Fielding a legal
starting lineup this week beats improving the roster, which beats denying an
opponent, and no amount of the lower thing adds up to the higher one. That is
the same shape as lineup.illegal_starters() bypassing MIN_GAIN_TO_CHANGE:
legality is not a matter of degree. It is implemented as five modes:

    patch   a starting slot is empty or unstartable and the bench cannot cover
            it. May cut into a rising-role player, because a hole in the lineup
            is a certain loss and an inheritance is a maybe. Allowed at any hour.
    fill    the roster is UNDER 17 and an ordinary spot is sitting empty. Not a legality
            emergency, so it does not get patch's exemptions -- but an empty spot
            scores zero every week it stays empty, so it does not face the
            upgrade bar either. Nobody is displaced, so there is no incumbent to
            beat. Judged on the CEILING, because an empty spot is exactly where a
            lottery ticket belongs.
    ir_fill an IR move in this run created the spot. Rank immediately free
            QB/RB/WR/TE players by authoritative ROS and fill only that measured
            delta, with no drop. This is the narrow Monday-guard exception.
    stream  matchup-driven defence streaming.
    ros     ordinary simulator-priced upgrades. It wears the same hard roster
            controls as news mode: complete/current weekly series, unlocked
            nonstarters, positional floors, four-week lineup coverage, a
            just-in-time recheck, and at most one resulting mutation. The
            direct ROS-to-ROS answer is retained beside the simulation so their
            disagreements can be audited. The default.
    news    event-scoped injury reaction. Affected players enter independently
            of season rank, but room membership is only discovery. A positive
            measured event delta and causal successor edge are required, then
            candidate and drop are compared apples-to-apples on the same fresh
            weekly-model ROS table. Coverage must survive and only one proposal
            may leave an event batch. Allowed until the candidate's game locks.

ORDINARY ADDS AND DROPS ARE PRICED IN CONTEXT, ON PURPOSE. expected.py supplies each
weekly mean, including fitted inheritance where a role opens. marginal.py then
simulates the candidate and each legal drop against our optimal lineups, so a
temporary starter can surface without pretending his value lasts all season and
a bench lottery ticket retains the value of the role he could inherit. News
mode deliberately uses the simpler absolute ROS comparison as its authority;
lineup marginality is advisory and can never justify dropping a higher-ROS man.

NO LONG-HORIZON MOVE NEAR KICKOFF. A rest-of-season swap made forty minutes
before the early games is a decision taken on this week's panic with the season's
consequences, and there is no reason it could not have been made on Tuesday. So
Everything but `patch` and event-scoped `news` refuses inside
ROS_MOVE_BLACKOUT_H of the next kickoff,
and says so out loud -- a silent no-op would be indistinguishable from "nothing
cleared".
`patch` is exempt because legality is urgent; `news` is exempt because the
information itself just arrived, but only until the candidate locks.

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

from robo import LEAGUE_ID_2026, faab, lineup, season, settings, value, vegas
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

# A BENCH MAN WHOSE LOSS TAIL REACHES THIS IS NOT CASUALLY CUT. The exact mirror
# of the bar clears() applies when ACQUIRING one -- `ceiling >= HIT_POINTS`, the
# median realised contribution of an add in this league -- so the same man is no
# longer bought on his tail and sold on his mean. Defaulting to the same 7.0
# keeps the two halves one decision; raising it protects fewer tickets and 0.0
# disables the gate entirely, which is the A/B baseline.
#
# It gates membership and never the ORDER. The pool stays sorted on the mean,
# because ranking one man's tail against another's mean put a rookie back above
# a WR1 as the more expensive cut.
TICKET_PROTECT_POINTS = 7.0

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

# Event evaluation has a hard three-minute SLA. Common random numbers and the
# reported paired SE make 48 worlds enough to refuse a noisy result rather than
# pretending it is precise; the ordinary weekly ROS run keeps marginal.SIMS=200.
NEWS_SIMS = 48

# The event path rebuilds immediately before it evaluates, so fifteen minutes
# is already generous there.  Scheduled ROS has two hand-offs: Wednesday's free
# pass follows the 09:00 cascade by about half an hour, while Tuesday claims
# follow the 16:00 cascade by four hours.  The news pulse rebuilds on
# meaningful intervening news.  Six hours therefore covers both quiet hand-offs
# while still refusing a prior-day valuation.  These are freshness controls,
# not tuning knobs for how readily the bot makes a move.
ROS_VALUE_MAX_AGE = 6 * 60 * 60

# An ordinary pass may build a deep waiver ladder for ONE roster slot, but it
# may not turn over two independent slots in the same vintage.  That is the
# same stale-comparison control news mode wears: after one mutation, every
# remaining add/drop comparison is against a roster that no longer exists.
ROS_MAX_MUTATIONS = 1

settings.apply(__name__, globals())

MODES = ("patch", "fill", "ir_fill", "stream", "ros", "news")


# ------------------------------------------------------------------ evaluation

def _context(league_id: str = LEAGUE_ID_2026, mode: str = "ros",
             affected: set[str] | None = None,
             event_deltas: dict | None = None,
             event_fingerprint: str | None = None,
             ir_open_slots: int = 0,
             ir_moves: list[dict] | None = None,
             excluded: set[str] | None = None) -> dict:
    from robo.rankings import build_board
    board = build_board()
    by_id = {r["player_id"]: r for r in board}
    r = season.mine(league_id)
    week = season.current_week()
    secs = vegas.next_kickoff(season.SEASON, week)
    available = season.free_agents(board, league_id)
    eligibility = season.transaction_states(
        [row["player_id"] for row in board], league_id, week=week)
    return {
        "board": board, "by_id": by_id, "roster": r, "week": week, "mode": mode,
        "reserve": set(r.get("reserve") or []), "starters": set(r.get("starters") or []),
        "players": api.players(),
        "available": available, "eligibility": eligibility,
        "on_waivers": {pid for pid, state in eligibility.items()
                       if state["acquisition"] in {"weekly_waiver", "drop_waiver"}},
        "faab": season.faab_left(league_id),
        "slots": season.slots(league_id),
        "league_id": league_id,
        # None means the schedule could not be read, and unknown is treated as
        # too close rather than plenty of time -- see vegas.next_kickoff.
        "hours_to_kickoff": None if secs is None else round(secs / 3600.0, 2),
        "affected": {str(p) for p in (affected or set())},
        "event_deltas": event_deltas or {},
        "event_fingerprint": event_fingerprint,
        "ir_open_slots": max(0, int(ir_open_slots)),
        "ir_moves": list(ir_moves or []),
        "excluded": {str(pid) for pid in (excluded or set())},
    }


def blacked_out(ctx: dict) -> str:
    """Why a long-horizon move must not run right now, or "" if it may.

    `patch` is never blacked out. Everything else is, close to kickoff, and an
    unreadable schedule counts as close.
    """
    if ctx["mode"] in ("patch", "news", "ir_fill"):
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

    Reuses lineup.optimize, so a "hole" means the exact optimizer that sets our lineup
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
    locks = season.week_points(ctx["week"], season.SEASON, ctx["league_id"])

    out = []
    for pid in ids:
        if pid in protected:
            continue
        # A locked bench player is not a legal drop in ANY mode. Sleeper
        # refuses the write once his game has started, so leaving him in the
        # pool for patch or fill does not buy an emergency option -- it buys a
        # rejected transaction and a plan that reads as submitted.
        if (locks.get(pid) or {}).get("locked"):
            continue
        row = ctx["by_id"].get(pid)
        if not row:
            continue
        v = hold_value(row, ctx)
        # In patch mode a hole in the lineup is a certain loss this week and an
        # inheritance is a maybe, so the floor and the rising-role premium both
        # yield -- but only far enough to reach the cheapest bodies we hold.
        if ctx["mode"] != "patch" and v > DROP_FLOOR:
            continue
        if _protected_ticket(row, ctx):
            continue
        out.append({"row": row, "value": v})
    return sorted(out, key=lambda d: d["value"])


def _ticket_tail(pid: str, ctx: dict) -> float | None:
    """The loss tail if it protects him, else None. One predicate, two pools.

    Returns the NUMBER rather than a boolean because both callers want to say
    why: a pool that silently drops a man reads identically to a pool that
    never considered him, which is the failure mode ros/block modes already
    refuse ("a silent no-op would look identical to nothing cleared").
    """
    if TICKET_PROTECT_POINTS <= 0 or ctx["mode"] == "patch":
        return None
    sh = value.hold_shape({"player_id": pid}, ctx["week"])
    if not sh or sh.get("starter"):
        return None
    tail = float(sh.get("tail") or 0.0)
    return tail if tail >= TICKET_PROTECT_POINTS else None


def _protected_ticket(row: dict, ctx: dict) -> bool:
    """Is this a bench man whose loss tail says he is a real lottery ticket?

    THE MIRROR OF clears(), AND A GATE FOR THE SAME REASON. Acquiring a bench
    player is judged on `ceiling >= HIT_POINTS` because down there the mean "is
    ranking noise and would always prefer a safe body to a man who might become
    something" -- and then best_free still RANKS the survivors on the mean.
    Cutting one was judged on the mean alone, so the same man was bought on his
    tail and sold on his middle.

    This restores the bar without disturbing the ordering. Ranking a bench man's
    tail against a starter's mean in one sorted pool is not a fix: it put Kaelon
    Black above Nico Collins as the more expensive cut, which would refuse real
    moves to protect a maybe.

    A STARTER IS NEVER PROTECTED HERE. He plays in the median world, so his mean
    is the right question and DROP_FLOOR already answers it. `patch` keeps its
    precedence: an unfillable starting slot is a certain loss this week and
    outranks protecting a contingency.
    """
    return _ticket_tail(str(row.get("player_id")), ctx) is not None


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
        state = (ctx.get("eligibility") or {}).get(row["player_id"]) or {}
        acquisition = state.get("acquisition")
        if waivers and acquisition not in {"weekly_waiver", "drop_waiver"}:
            continue
        if not waivers and acquisition not in {None, "free_now"}:
            continue
        if ctx["mode"] == "news" and row["player_id"] not in ctx["affected"]:
            continue
        if (row["player_id"] in onw) != waivers:
            continue
        if pos and (row.get("pos") or "") not in pos:
            continue
        v, real = value.value_of(row, ctx["week"])
        out.append({"row": row, "value": v, "real": real})
    return sorted(out, key=lambda d: (-d["value"], d["row"]["player_id"]))


# ---------------------------------------------------------- guarded news mode

MIN_ROSTER_COVERAGE = {"QB": 3, "RB": 3, "WR": 3, "TE": 2}
# Backward-compatible name for existing audit/tests; the control now protects
# both event and ordinary ROS moves.
NEWS_MIN_COVERAGE = MIN_ROSTER_COVERAGE
NEWS_VALUE_MAX_AGE = 15 * 60


def _table_ready(table: dict, max_age: float) -> tuple[bool, str]:
    """Whether a weekly-model table is current enough to authorize a move."""
    if table.get("schema") is None:
        return False, "weekly ROS table has no schema"
    try:
        computed = float(table.get("computed") or 0)
    except (TypeError, ValueError):
        return False, "weekly ROS table has an invalid timestamp"
    if computed <= 0 or time.time() - computed > max_age:
        return False, "weekly ROS table is stale"
    try:
        weighted = any(float(weight or 0) > 0
                       for weight in (table.get("weights") or {}).values())
    except (TypeError, ValueError):
        return False, "weekly ROS horizon is invalid"
    if not weighted:
        return False, "weekly ROS horizon is missing"
    return True, ""


def _value_complete(table: dict, pid: str, max_age: float) -> tuple[bool, str]:
    """Whether one weekly ROS row is complete enough to authorize a move."""
    ready, why = _table_ready(table, max_age)
    if not ready:
        return ready, why
    row = (table.get("players") or {}).get(str(pid))
    if not row:
        return False, "missing from weekly ROS table"
    required = len([w for w, weight in (table.get("weights") or {}).items()
                    if float(weight or 0) > 0])
    # One absent row is the player's bye. Anything thinner is an incomplete
    # horizon and may not be compared with a complete incumbent.
    if len(row.get("by_week") or {}) < max(1, required - 1):
        return False, "weekly ROS series is incomplete"
    return True, ""


def _news_complete(table: dict, pid: str) -> tuple[bool, str]:
    """Event-SLA form retained as the public seam used by the pulse audit."""
    return _value_complete(table, pid, NEWS_VALUE_MAX_AGE)


def _ros_complete(table: dict, pid: str) -> tuple[bool, str]:
    """Scheduled-pass form of the same completeness control."""
    return _value_complete(table, pid, ROS_VALUE_MAX_AGE)


def _expected_table(ctx: dict) -> dict:
    """One valuation vintage per move context."""
    if "_expected_table" not in ctx:
        from robo import expected
        ctx["_expected_table"] = expected.load()
    return ctx["_expected_table"]


def _skill_lineup_fillable(table: dict, active_ids: set[str], week: int) -> bool:
    """Can the eight skill slots be filled from available players this week?"""
    slots = lineup.SLOTS[:8]
    rows = table.get("players") or {}
    pool = []
    for pid in active_ids:
        row = rows.get(pid)
        cell = (row or {}).get("by_week", {}).get(str(week))
        if not row or not cell or float(cell.get("a", 1.0)) <= 0:
            continue
        pool.append((pid, row.get("pos")))
    masks = {0}
    for _, pos in pool:
        nxt = set(masks)
        for mask in masks:
            for i, slot in enumerate(slots):
                if not mask & (1 << i) and pos in lineup.SLOT_ELIGIBLE[slot]:
                    nxt.add(mask | (1 << i))
        masks = nxt
    return (1 << len(slots)) - 1 in masks


def _coverage_after(ctx: dict, add_id: str, drop_id: str | None,
                    table: dict | None = None,
                    record: dict | None = None) -> tuple[bool, str]:
    """Protect roster construction while allowing cross-position upgrades."""
    table = table or _expected_table(ctx)
    held = set(ctx["roster"].get("players") or [])
    reserve = set(ctx["roster"].get("reserve") or [])
    active = (held - reserve) | {str(add_id)}
    if drop_id:
        active.discard(str(drop_id))
    players = ctx.get("players") or {}
    erows = table.get("players") or {}

    def available_soon(pid: str) -> bool:
        row = erows.get(pid) or {}
        for w in range(ctx["week"], ctx["week"] + 4):
            cell = (row.get("by_week") or {}).get(str(w))
            if cell and float(cell.get("a", 1.0)) > 0:
                return True
        return False

    def tally(ids: set[str]) -> tuple[dict, dict]:
        c = {p: 0 for p in MIN_ROSTER_COVERAGE}
        s = {"K": 0, "DEF": 0}
        for pid in ids:
            pos = ((erows.get(pid) or {}).get("pos")
                   or (players.get(pid) or {}).get("position"))
            if pos in c and available_soon(pid):
                c[pos] += 1
            if pos in s:
                s[pos] += 1
        return c, s

    counts, specialists = tally(active)
    # WHERE WE ALREADY STAND, so the floor can be a direction rather than a wall.
    before, _ = tally(held - reserve)
    audit = {
        "active_after": len(active),
        "minimums": dict(MIN_ROSTER_COVERAGE),
        "counts": counts,
        "counts_before": before,
        "specialists": specialists,
        "weeks": [],
    }

    def finish(ok: bool, why: str) -> tuple[bool, str]:
        audit.update({"passed": ok, "reason": why or "coverage preserved"})
        if record is not None:
            record.update(audit)
        return ok, why

    # NON-WORSENING, NOT A WALL. The floor used to reject any roster that did
    # not clear it, which is the wrong test when we are ALREADY under it: with
    # our third quarterback on IR the active QB count is 2 against a floor of 3,
    # so every candidate failed "QB2/3" and the ordinary channel returned zero
    # options for reasons that had nothing to do with the candidate. Worse, the
    # rejection was position-selective without meaning to be -- adding a QB
    # passed and adding anyone else did not -- so the control was quietly
    # steering every proposal toward any warm quarterback, which is most of how
    # a third-string QB who cannot play came to be the only thing on the board.
    #
    # A shortfall we are already in is a REASON TO PRIORITISE, handled by
    # `meets_floor` below and the ordering in plan_claims. What stays forbidden
    # is making it worse: a move may not take a position below the floor, nor
    # below where it already sits if that is lower still.
    short = [f"{p}{counts[p]}/{minimum}" for p, minimum in MIN_ROSTER_COVERAGE.items()
             if counts[p] < minimum]
    worse = [f"{p}{counts[p]}/{minimum}" for p, minimum in MIN_ROSTER_COVERAGE.items()
             if counts[p] < min(minimum, before[p])]
    audit["short_after"] = short
    audit["meets_floor"] = not short
    audit["relieves"] = sorted(p for p, minimum in MIN_ROSTER_COVERAGE.items()
                               if before[p] < minimum and counts[p] > before[p])
    if worse:
        return finish(False, "coverage floor would fail: " + ", ".join(worse))
    missing = [p for p, n in specialists.items() if n < 1]
    if missing:
        return finish(False, "roster cannot fill " + ", ".join(missing))

    horizon = max((int(w) for w in (table.get("weights") or {}) if str(w).isdigit()),
                  default=ctx["week"])
    for w in range(ctx["week"], min(horizon, ctx["week"] + 3) + 1):
        fillable = _skill_lineup_fillable(table, active, w)
        audit["weeks"].append({"week": w, "skill_lineup_fillable": fillable})
        if not fillable:
            return finish(False, f"cannot fill all skill lineup slots in week {w}")
    return finish(True, "")


def _direct_ros(table: dict, add_id: str, drop_id: str | None) -> dict:
    """The simple apples-to-apples answer carried beside a simulated one."""
    rows = table.get("players") or {}
    add_ros = float((rows.get(str(add_id)) or {}).get("ros") or 0.0)
    drop_ros = (float((rows.get(str(drop_id)) or {}).get("ros") or 0.0)
                if drop_id else 0.0)
    gain = add_ros - drop_ros
    return {"add_ros": round(add_ros, 3), "drop_ros": round(drop_ros, 3),
            "gain": round(gain, 3), "prefers_move": gain > 0}


def _controlled_ros_options(ctx: dict, table: dict,
                            options: list[dict]) -> list[dict]:
    """Put simulator proposals through the news path's hard roster controls.

    The simulator remains the authority.  The direct ROS comparison is attached
    for audit and calibration, while freshness, complete horizons, roster
    construction and near-term lineup coverage are non-negotiable.
    """
    accepted, checks = [], ctx.setdefault("_ros_control_checks", [])
    for option in options:
        add_id, drop_id = str(option["add"]), option.get("drop")
        check = {"add_id": add_id, "drop_id": drop_id}
        complete, why = _ros_complete(table, add_id)
        if not complete:
            checks.append({**check, "eligible": False, "reason": why})
            continue
        if drop_id is not None:
            complete, why = _ros_complete(table, str(drop_id))
            if not complete:
                checks.append({**check, "eligible": False, "reason": why})
                continue
        coverage = {}
        safe, why = _coverage_after(ctx, add_id, drop_id, table, record=coverage)
        if not safe:
            checks.append({**check, "eligible": False, "reason": why,
                           "coverage": coverage})
            continue
        direct = _direct_ros(table, add_id, drop_id)
        # PRIORITY, NOT PERMISSION. 0 is a move that leaves every floor met --
        # including one that lifts us back over a floor we are under -- and 1 is
        # a move that is merely not worse. plan_claims orders on this before it
        # orders on value, so a roster under its quarterback floor asks for a
        # quarterback FIRST and still prices everyone else behind him instead of
        # refusing to look. See _coverage_after for why this stopped being a veto.
        priority = 0 if coverage.get("meets_floor") else 1
        reason = ("coverage preserved" if coverage.get("meets_floor")
                  else "below floor and not worsened: "
                       + ", ".join(coverage.get("short_after") or []))
        checks.append({**check, "eligible": True, "reason": reason,
                       "coverage": coverage, "direct_ros": direct,
                       "coverage_priority": priority})
        accepted.append({**option, "coverage": coverage,
                         "coverage_priority": priority,
                         "direct_ros": direct,
                         "valuation_computed": table.get("computed")})
    return accepted


def _news_drop_pool(ctx: dict, add_id: str, table: dict) -> tuple[list[dict], list[dict]]:
    """Coverage-safe incumbents, cheapest weekly-model ROS first."""
    live = season.week_points(ctx["week"], season.SEASON, ctx["league_id"])
    protected = ctx["starters"] | ctx["reserve"]
    rows = table.get("players") or {}
    out, checks = [], []
    for pid in (ctx["roster"].get("players") or []):
        pos = (rows.get(pid) or {}).get("pos") or (ctx["players"].get(pid) or {}).get("position")
        identity = ctx["by_id"].get(pid) or rows.get(pid) or {}
        check = {"candidate_id": str(add_id), "drop_id": pid,
                 "drop_name": identity.get("name") or pid,
                 "drop_pos": identity.get("pos") or pos,
                 "drop_team": identity.get("team"),
                 "drop_ros": (float(rows[pid]["ros"]) if pid in rows else None)}
        if pid in protected:
            checks.append({**check, "eligible": False,
                           "reason": ("protected starter" if pid in ctx["starters"]
                                      else "reserve/IR player")})
            continue
        if pos in {"K", "DEF"}:
            checks.append({**check, "eligible": False,
                           "reason": f"{pos} is not a skill-player drop in news mode"})
            continue
        if (live.get(pid) or {}).get("locked"):
            checks.append({**check, "eligible": False,
                           "reason": "player's game is locked"})
            continue
        complete, why = _news_complete(table, pid)
        if not complete:
            checks.append({**check, "eligible": False, "reason": why})
            continue
        coverage = {}
        safe, why = _coverage_after(ctx, add_id, pid, table, record=coverage)
        if not safe:
            checks.append({**check, "eligible": False, "reason": why,
                           "coverage": coverage})
            continue
        checks.append({**check, "eligible": True, "reason": "coverage preserved",
                       "coverage": coverage})
        out.append({"player_id": pid, "row": identity or rows[pid],
                    "ros": float(rows[pid]["ros"]), "coverage": coverage})
    return sorted(out, key=lambda x: (x["ros"], x["player_id"])), checks


def plan_news(ctx: dict, waivers: bool | None = None) -> list[dict]:
    """One event-authorized swap, priced strictly ROS-to-ROS.

    Room membership only admits a player for consideration. Authority requires
    a positive measured pre/post event delta and an explicit causal edge, then
    the candidate must beat the cheapest coverage-safe incumbent on the exact
    same freshly built expected.py table. Weekly lineup gain is deliberately
    absent from this decision boundary; it belongs in the review narrative.
    """
    table = _expected_table(ctx)
    erows = table.get("players") or {}
    onw = ctx["on_waivers"]
    options, rejected, candidates, drop_checks = [], [], [], []
    for c in ctx["available"]:
        pid = str(c["player_id"])
        if pid not in ctx["affected"] or (waivers is not None and ((pid in onw) != waivers)):
            continue
        delta = ctx.get("event_deltas", {}).get(pid) or {}
        identity = ctx["by_id"].get(pid) or erows.get(pid) or c
        check = {
            "player_id": pid,
            "name": identity.get("name") or pid,
            "pos": identity.get("pos"),
            "team": identity.get("team"),
            "on_waivers": pid in onw,
            "pre_ros": delta.get("pre_ros"),
            "post_ros": delta.get("post_ros"),
            "delta_ros": delta.get("delta_ros"),
            "causal_edge": delta.get("causal_edge"),
            "series_complete": bool(delta.get("complete")),
        }

        def reject(stage: str, reason: str, **extra) -> None:
            row = {**check, **extra, "stage": stage, "outcome": "rejected",
                   "reason": reason}
            rejected.append(row)
            candidates.append(row)

        complete, why = _news_complete(table, pid)
        if not complete:
            reject("fresh_table", why)
            continue
        if not delta.get("complete"):
            reject("pre_post", "pre/post event series is incomplete")
            continue
        if not delta.get("causal_edge") or float(delta.get("delta_ros") or 0) <= 0:
            reject("causal_delta", "no positive causal event delta")
            continue
        cand_ros = float(erows[pid]["ros"])
        if ctx["slots"]["open"] > 0:
            coverage = {}
            safe, why = _coverage_after(ctx, pid, None, table, record=coverage)
            drops = ([{"player_id": None,
                       "row": {"player_id": None, "name": "(open roster spot)", "pos": "--"},
                       "ros": 0.0, "coverage": coverage}] if safe else [])
            if not safe:
                reject("coverage", why, candidate_ros=cand_ros,
                       coverage=coverage)
        else:
            drops, checks = _news_drop_pool(ctx, pid, table)
            drop_checks.extend(checks)
        if not drops:
            if not any(x.get("player_id") == pid for x in candidates):
                reject("drop_pool", "no coverage-safe unlocked nonstarter",
                       candidate_ros=cand_ros)
            continue
        drop = drops[0]
        gain = cand_ros - drop["ros"]
        if gain <= 0:
            reject("ros_comparison",
                   f"ROS {cand_ros:.2f} does not beat {drop['ros']:.2f}",
                   candidate_ros=cand_ros, drop_id=drop["player_id"],
                   drop_name=drop["row"].get("name"), drop_ros=drop["ros"],
                   gain=round(gain, 2), coverage=drop.get("coverage"))
            continue
        approved = {**check, "stage": "ros_comparison", "outcome": "proposed",
                    "reason": "positive event delta and candidate beats the cheapest coverage-safe incumbent",
                    "candidate_ros": cand_ros, "drop_id": drop["player_id"],
                    "drop_name": drop["row"].get("name"), "drop_ros": drop["ros"],
                    "gain": round(gain, 2), "coverage": drop.get("coverage")}
        candidates.append(approved)
        options.append({"add": identity,
                        "drop": drop["row"], "gain": round(gain, 2),
                        "add_value": round(cand_ros, 2),
                        "drop_value": round(drop["ros"], 2), "real": True,
                        "on_waivers": pid in onw,
                        "event_delta": round(float(delta["delta_ros"]), 2),
                        "causal_edge": delta.get("causal_edge"),
                        "coverage": drop.get("coverage"),
                        "valuation_computed": table.get("computed"),
                        "why": (f"weekly-model ROS apples-to-apples; event delta "
                                f"{float(delta['delta_ros']):+.2f}; coverage preserved")})
    ctx["_news_rejections"] = rejected
    ctx["_news_candidates"] = candidates
    ctx["_news_drop_checks"] = drop_checks
    return sorted(options, key=lambda p: (-p["gain"], -p["event_delta"],
                                          p["add"]["player_id"]))[:1]


def _news_channel_plans(ctx: dict, channel: str):
    if "_news_choice" not in ctx:
        ctx["_news_choice"] = plan_news(ctx, waivers=None)
    choice = ctx["_news_choice"]
    if not choice:
        return []
    p = choice[0]
    if channel == "free":
        return [p] if not p["on_waivers"] else []
    if not p["on_waivers"]:
        return []
    # News authorizes the candidate with the deliberately simple ROS-to-ROS
    # safety comparison above.  Money is priced in one unit everywhere,
    # however: paired marginal lineup value against the roster we keep if the
    # claim loses.  Feeding direct player ROS into faab.py made a news claim and
    # an identical Tuesday claim receive different bids.
    from robo import faab_field, marginal
    add_id = str(p["add"]["player_id"])
    drop_id = p["drop"].get("player_id")
    b = marginal.Board(ctx["league_id"], sims=NEWS_SIMS, extra=[add_id],
                       roster_ids=list(ctx["roster"].get("players") or []))
    options = marginal.price_options(b, [drop_id], [add_id])
    if not options:
        return []
    priced = options[0]
    if priced["gain"] <= NOISE_MULTIPLE * priced["se"]:
        ctx.setdefault("_news_rejections", []).append({
            "player_id": add_id, "name": p["add"].get("name"),
            "stage": "paired_bid_value", "outcome": "rejected",
            "reason": (f"paired roster gain {priced['gain']:+.2f} did not beat "
                       f"{NOISE_MULTIPLE:g}x error {priced['se']:.2f}")})
        return []
    try:
        field = faab_field.predict(ctx, add_id, _expected_table(ctx))
    except Exception as e:
        field = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    quote = faab.quote(priced["gain"], ctx["week"], ctx["faab"],
                       p["add"].get("pos"), field if field.get("available") else None)
    claim = {**p, "bid": int(quote["bid"]), "priority": 0, "seq": 0,
             "bid_gain": round(priced["gain"], 3),
             "bid_se": round(priced["se"], 3), "bid_quote": quote,
             "opponent_field": field}
    return [{"drop": p["drop"], "drop_value": p["drop_value"], "claims": [claim]}]


# -------------------------------------------------------------------- channels

def _transaction_recheck(ctx: dict, add_id: str, drop_id=None,
                         plan: dict | None = None,
                         claim: bool = False) -> str:
    """Return why a just-in-time news/ROS transaction is no longer safe."""
    season.invalidate_live()
    live = season.week_points(ctx["week"], season.SEASON, ctx["league_id"])
    if add_id in season.rostered_ids(ctx["league_id"]):
        return "he is no longer available"
    eligibility = season.transaction_eligibility(
        add_id, ctx["league_id"], week=ctx["week"])
    # ROSTER MOVEMENT FIRST, AND SEPARATELY. The two states answer different
    # questions and a player can fail either one alone: his own game locking
    # stops any movement involving him this week, whatever the wire thinks his
    # acquisition state is. Checking only the acquisition half let a locked
    # candidate through whenever the feed carried the lock without a game id.
    if eligibility["roster_movement"] == "roster_locked":
        return ("he is locked: his NFL game has started, so no roster movement "
                "involving him lands until the week advances")
    allowed = ({"weekly_waiver", "drop_waiver"} if claim else {"free_now"})
    if eligibility["acquisition"] not in allowed:
        return (f"acquisition state is {eligibility['acquisition']}: "
                f"{eligibility['reason']}")
    mine_now = season.mine(ctx["league_id"]) or {}
    held = set(mine_now.get("players") or [])
    starters = set(mine_now.get("starters") or [])
    reserve = set(mine_now.get("reserve") or [])
    mode = ctx.get("mode", "news")
    max_age = NEWS_VALUE_MAX_AGE if mode == "news" else ROS_VALUE_MAX_AGE
    if drop_id is None:
        if season.slots(ctx["league_id"])["open"] <= 0:
            return "the open roster spot has been filled"
        if plan is not None:
            from robo import expected
            table = expected.load()
            complete, why = _value_complete(table, str(add_id), max_age)
            if not complete:
                return why
            if (plan.get("valuation_computed") is not None
                    and table.get("computed") != plan.get("valuation_computed")):
                return "weekly ROS table changed after this proposal was priced"
            live_ctx = {**ctx, "roster": mine_now,
                        "starters": starters,
                        "reserve": reserve}
            safe, why = _coverage_after(live_ctx, str(add_id), None, table)
            if not safe:
                return why
        return ""
    if drop_id not in held:
        return "the planned drop is no longer held"
    defence_swap = bool(plan and plan.get("claim_kind") == "def")
    if defence_swap:
        # A streamed defence is necessarily the current starter and is replaced
        # in the same transaction. Starter protection is for skill players; the
        # real hard boundaries here are reserve state and kickoff lock.
        if drop_id in reserve or (live.get(drop_id) or {}).get("locked"):
            return "the planned defence drop is now protected"
        apos = ((ctx.get("players") or {}).get(str(add_id)) or {}).get("position")
        dpos = ((ctx.get("players") or {}).get(str(drop_id)) or {}).get("position")
        if apos != "DEF" or dpos != "DEF":
            return "defence stream is not a DEF-for-DEF replacement"
        return ""
    if drop_id in starters or drop_id in reserve or (live.get(drop_id) or {}).get("locked"):
        return "the planned drop is now protected"
    if plan is not None:
        from robo import expected
        table = expected.load()
        add_row = (table.get("players") or {}).get(str(add_id))
        drop_row = (table.get("players") or {}).get(str(drop_id))
        ac, aw = _value_complete(table, str(add_id), max_age)
        dc, dw = _value_complete(table, str(drop_id), max_age)
        if not ac or not dc:
            return aw or dw
        if (plan.get("valuation_computed") is not None
                and table.get("computed") != plan.get("valuation_computed")):
            return "weekly ROS table changed after this proposal was priced"
        # Direct ROS is news mode's authority.  In ordinary mode it is the
        # recorded comparator; the simulator remains the authority.
        if mode == "news" and float(add_row["ros"]) <= float(drop_row["ros"]):
            return (f"live ROS comparison no longer clears: {float(add_row['ros']):.2f} "
                    f"<= {float(drop_row['ros']):.2f}")
        live_ctx = {**ctx, "roster": mine_now,
                    "starters": starters,
                    "reserve": reserve}
        safe, why = _coverage_after(live_ctx, str(add_id), str(drop_id), table)
        if not safe:
            return why
    return ""


def evaluation_block(ctx: dict) -> str:
    """Any hard reason this mode must not produce a transaction proposal."""
    timing = blacked_out(ctx)
    if timing:
        return timing
    if ctx.get("mode") in {"ros", "ir_fill"}:
        ready, why = _table_ready(_expected_table(ctx), ROS_VALUE_MAX_AGE)
        if not ready:
            return why
    return ""


def _news_recheck(ctx: dict, add_id: str, drop_id=None,
                  plan: dict | None = None) -> str:
    """Backward-compatible event-path seam used by the focused safety tests."""
    return _transaction_recheck({**ctx, "mode": "news"}, add_id, drop_id, plan)

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
        if mode in ("news", "ros", "ir_fill"):
            why = _transaction_recheck(ctx, add.get("player_id"),
                                       drop.get("player_id"), p, claim=False)
            if why:
                print(f"  ** ADD SKIPPED {add['name']}: {why}")
                continue
        try:
            sw.free_agent_transaction(
                {add["player_id"]: rid} if add.get("player_id") else None,
                {drop["player_id"]: rid} if drop.get("player_id") else None,
                league_id)
        except Exception as e:
            print(f"  ** ADD FAILED {add['name']}: {type(e).__name__}: {e}")
            continue
        out["submitted"].append({"add": add["name"], "drop": drop["name"]})
        if mode == "ir_fill":
            _record("free-agent", f"Filled IR-created spot with {add['name']}",
                    f"Added {add['name']} ({add['pos']}) without a drop.",
                    f"The current IR sweep opened the spot; {add['name']} was the "
                    f"highest-ROS immediately acquirable skill player at "
                    f"{p['add_value']:.1f}.",
                    {"add": add.get("player_id"), "drop": None,
                     "mode": mode, "reason": "ir_open_slot_fill",
                     "ir_source_players": p.get("ir_source_players") or [],
                     "ros": p["add_value"], "week": ctx["week"]})
        else:
            _record("free-agent", f"Signed {add['name']}, released {drop['name']}",
                    f"{add['name']} ({add['pos']}) in, {drop['name']} out.",
                    f"Rest-of-season value {p['add_value']:.1f} against "
                    f"{p['drop_value']:.1f} held, a gain of {p['gain']:+.1f} "
                    f"in {mode} mode."
                    + (f" {p['why']}." if p.get("why") else ""),
                    {"add": add.get("player_id"), "drop": drop.get("player_id"),
                     "mode": mode, "week": ctx["week"], "gain": p["gain"]})
        if mode in ("news", "ros"):
            # ONE ROSTER WRITE PER RUN. A successful write invalidates every
            # comparison made against the old roster -- that was always true
            # and was only enforced for `news`, because `ros` could not produce
            # a second plan: ROS_MAX_MUTATIONS capped it at one drop and the
            # open spot was excluded from the channel entirely. Now that a fill
            # costs no mutation, ros can propose a fill AND a swap, and the
            # swap was priced against a roster that does not yet contain the
            # man the fill just signed. _transaction_recheck re-reads the live
            # roster but re-validates; it does not re-price. The fill is first
            # and free, so it goes; the swap is re-derived next run against the
            # roster it would actually be swapping into.
            break
    out["applied"] = bool(out["submitted"])
    if out["applied"] and mode not in ("news", "ros"):
        try:
            out["waiver_maintenance"] = maintain_pending_claims(
                apply=True, league_id=league_id,
                reason=f"{mode} free-agent roster change")
        except Exception as e:
            out["waiver_maintenance"] = {"status": "failed",
                                         "error": f"{type(e).__name__}: {e}"}
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
    if mode == "ir_fill":
        count = min(int(ctx.get("ir_open_slots") or 0),
                    int((ctx.get("slots") or {}).get("open") or 0))
        if count <= 0:
            return []
        table = _expected_table(ctx)
        options = []
        for candidate in candidates(ctx, waivers=False,
                                    pos={"QB", "RB", "WR", "TE"}):
            row = candidate["row"]
            pid = str(row["player_id"])
            if pid in ctx.get("excluded", set()):
                continue
            complete, _ = _value_complete(table, pid, ROS_VALUE_MAX_AGE)
            value_row = (table.get("players") or {}).get(pid) or {}
            ros_value = float(value_row.get("ros") or 0)
            if not complete or ros_value <= 0:
                continue
            options.append((ros_value, pid, row))
        options.sort(key=lambda item: (-item[0], item[1]))
        source_ids = [str(m.get("player_id")) for m in ctx.get("ir_moves", [])
                      if m.get("player_id")]
        return [{"add": row,
                 "drop": {"player_id": None,
                          "name": "(IR-created open roster spot)", "pos": "--"},
                 "gain": round(ros_value, 1), "add_value": round(ros_value, 1),
                 "drop_value": 0.0, "real": True,
                 "valuation_computed": table.get("computed"),
                 "reason_code": "ir_open_slot_fill",
                 "ir_source_players": source_ids,
                 "why": "highest positive authoritative ROS among immediately "
                        "acquirable QB/RB/WR/TE players; no player is dropped"}
                for ros_value, _, row in options[:count]]
    if mode == "news":
        return _news_channel_plans(ctx, "free")
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
    elif mode == "ros":
        slots = ROS_MAX_MUTATIONS
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
        #
        # AN EMPTY BENCH SPOT IS THE FIRST PLACE TO PUT HIM. droppables() only
        # ever returns men, so patch cut somebody to fill a starting slot while
        # a roster spot sat empty -- and in the cascade it runs at step 4,
        # ahead of the `fill` step at step 6 that was supposed to have used
        # that spot. Ordering cannot fix what the step cannot express.
        if int((ctx.get("slots") or {}).get("open") or 0) > 0:
            drops = [{"row": {"player_id": None, "name": "(open roster spot)"},
                      "value": 0.0}] + drops
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
                            "why": ("fills an unfillable starting slot from the "
                                    "open roster spot, dropping nobody"
                                    if d["row"]["player_id"] is None
                                    else "fills an unfillable starting slot")})
                break
        return out

    # ORDINARY UPGRADES ARE PRICED BY SIMULATION, not by subtracting two absolute
    # season totals across different positions. That arithmetic put a defence and
    # a fourth receiver on the same axis and scored a +1.6 swap at +87.
    P = priced(ctx)
    # AN EMPTY SPOT IS FILLED BEFORE ANYBODY IS CUT, AND THE FILL IS NOT A
    # MUTATION. An empty roster spot scores zero every week it stays empty and
    # a free agent costs nothing to take, so the two are not alternatives to
    # weigh -- the fill is free and strictly dominant, and only the swap spends
    # the budget ROS_MAX_MUTATIONS exists to ration.
    #
    # `ros` used to drop the open spot from this list entirely, on the
    # reasoning that the cascade's `fill` step owns the free side and one None
    # at the front of a slice of length ROS_MAX_MUTATIONS would be the only
    # drop the loop ever saw. The second half of that is true and is fixed by
    # not counting it; the first half is not, because RobonerWaivers and
    # RobonerMoves do not run the cascade at all. So on waiver night the one
    # channel that could take a free agent into an empty spot was the one
    # forbidden to, and it proposed cutting a man to sign somebody it could
    # have had for nothing with a spot still open.
    opens = [d for d in P["drops"] if d is None]
    swaps = [d for d in P["drops"] if d is not None]
    slot_drops = opens + (swaps[:max(1, slots)] if mode != "fill" else [])
    for drop in slot_drops:
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
    direct = o.get("direct_ros")
    comparison = ""
    if direct:
        verdict = "agrees" if direct["prefers_move"] else "disagrees"
        comparison = (f"; direct ROS {direct['add_ros']:.1f} vs "
                      f"{direct['drop_ros']:.1f} ({direct['gain']:+.1f}, {verdict})")
    return {"add": add_row, "drop": drop_row,
            "gain": round(o["gain"], 1),
            "add_value": round(o["gain"], 1),
            "drop_value": drop_val,
            "real": True,
            "se": round(o["se"], 2), "ceiling": round(o["ceiling"], 1),
            "coverage": o.get("coverage"), "direct_ros": direct,
            "coverage_priority": o.get("coverage_priority", 0),
            "valuation_computed": o.get("valuation_computed"),
            "why": ((why or f"{kind}; +/- {o['se']:.1f}, ceiling {o['ceiling']:.1f}")
                    + comparison)}


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
    # Membership only needs the cached weekly series. Constructing a Board
    # simulates our baseline roster, so doing that before we know this channel
    # contains an affected free agent wastes minutes on a quiet/no-longer-
    # actionable event.
    series_players = marginal.series(ctx["league_id"])["players"]
    table = _expected_table(ctx)

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
                if c["row"]["player_id"] in series_players]
        if ctx["mode"] == "ros":
            pool = [pid for pid in pool if _ros_complete(table, pid)[0]]
        if ctx["mode"] == "news":
            pool = [pid for pid in pool if pid in ctx.get("affected", set())]
        out = pool[:SHORTLIST]
        # THE SEASON TOTAL CANNOT SEE A FILL-IN. candidates() orders by
        # rest-of-season value, which is the right question for an upgrade we
        # will hold to January and the wrong one for the next three weeks: a man
        # whose value is concentrated in them has a small season total by
        # construction. With our quarterback out, his backup was the best free
        # agent in the league for the week in question and ranked 100th here.
        # A union rather than a replacement, so nothing the old ordering
        # surfaced is lost -- it can only ever add candidates to price.
        near = sorted(pool,
                      key=lambda p: -value.near_value(p, ctx["week"], table=table))
        for pid in near[:SHORTLIST_NEAR]:
            if pid not in out:
                out.append(pid)
        return out

    free, wire = shortlist(False), shortlist(True)
    if not free and not wire:
        return {"board": None, "drops": [], "free": [], "wire": []}

    roster_ids = list(ctx["roster"].get("players") or [])
    if ctx["mode"] == "fill":
        # An open roster spot has no incumbent, so there is nothing to price the
        # candidate against and nothing to give up. None carries that through.
        drops = [None] * max(1, ctx["slots"]["open"])
        b = marginal.Board(ctx["league_id"], extra=free + wire,
                           roster_ids=roster_ids)
    elif ctx["mode"] == "news":
        drops = []
        if ctx["slots"]["open"]:
            drops += [None] * min(ctx["slots"]["open"], MAX_SLOTS_TO_TURN_OVER)
        room = MAX_SLOTS_TO_TURN_OVER - len(drops)
        b = marginal.Board(ctx["league_id"], sims=NEWS_SIMS, extra=free + wire,
                           roster_ids=roster_ids)
        if room:
            locks = season.week_points(ctx["week"], season.SEASON, ctx["league_id"])
            protected = ctx["starters"] | ctx["reserve"]
            eligible = [pid for pid in (ctx["roster"].get("players") or [])
                        if pid not in protected and pid in b.S
                        and not (locks.get(pid) or {}).get("locked")]
            priced_drops = sorted((b.drop_price(pid)[0], pid) for pid in eligible)
            drops += [pid for cost, pid in priced_drops
                      if cost <= DROP_FLOOR][:room]
    else:
        b = marginal.Board(ctx["league_id"], extra=free + wire,
                           roster_ids=roster_ids)
        locks = season.week_points(ctx["week"], season.SEASON, ctx["league_id"])
        protected = ctx["starters"] | ctx["reserve"]
        priced_drops = []
        drop_checks = ctx.setdefault("_ros_drop_checks", [])
        for pid in roster_ids:
            identity = ctx["by_id"].get(pid) or b.S.get(pid) or {}
            check = {"player_id": pid,
                     "name": identity.get("name") or pid,
                     "pos": identity.get("pos") or identity.get("position")}
            if pid in protected:
                reason = ("protected starter" if pid in ctx["starters"]
                          else "reserve/IR player")
                drop_checks.append({**check, "eligible": False, "reason": reason})
                continue
            if pid not in b.S:
                drop_checks.append({**check, "eligible": False,
                                    "reason": "missing from the simulation board"})
                continue
            if (locks.get(pid) or {}).get("locked"):
                drop_checks.append({**check, "eligible": False,
                                    "reason": "player's game is locked"})
                continue
            complete, why = _ros_complete(table, pid)
            if not complete:
                drop_checks.append({**check, "eligible": False, "reason": why})
                continue
            cost = b.drop_price(pid)[0]
            if cost > DROP_FLOOR:
                drop_checks.append({**check, "eligible": False,
                                    "drop_price": round(cost, 3),
                                    "reason": (f"drop price {cost:.1f} exceeds the "
                                               f"{DROP_FLOOR:.1f} protection floor")})
                continue
            # THE SAME GATE _droppables APPLIES, THROUGH THE SAME PREDICATE.
            # This loop is a second drop pool -- it feeds the option board the
            # waiver planner reads, while _droppables feeds the free-agent
            # planner -- and a gate added to only one of them protects a
            # lottery ticket from being cut for a free agent and not from being
            # cut for a claim. It also made the audit record CONTRADICT the
            # decision: Kaelon Black was excluded from one pool while this loop
            # wrote him down as "available for an upgrade comparison".
            tail = _ticket_tail(pid, ctx)
            if tail is not None:
                drop_checks.append({**check, "eligible": False,
                                    "drop_price": round(cost, 3),
                                    "reason": (f"held as a lottery ticket: losing him "
                                               f"costs {tail:.1f} in the worst world, "
                                               f"over the {TICKET_PROTECT_POINTS:.1f} bar")})
                continue
            drop_checks.append({**check, "eligible": True,
                                "drop_price": round(cost, 3),
                                "reason": "available for an upgrade comparison"})
            priced_drops.append((cost, pid))
        # AN EMPTY ROSTER SPOT IS A DROP THAT COSTS NOTHING, and until now the
        # ordinary channel could not see one: `fill` and `news` both put None in
        # here, `ros` never did, and `ros` is the only mode the Tuesday waiver
        # task runs. So a night with an open slot and a full FAAB budget built a
        # slate of swaps or nothing at all. Prepended AFTER the sort -- putting
        # None into priced_drops would compare None to a str on a cost tie.
        drops = ([None] if ctx["slots"]["open"] > 0 else []) \
            + [pid for _, pid in sorted(priced_drops)]
    affected_weeks = None
    if ctx["mode"] == "news":
        affected_weeks = set()
        for pid in ctx.get("affected", set()):
            for w, cell in (b.S.get(pid, {}).get("weeks") or {}).items():
                if len(cell) > 2 and cell[2] < 0.999:
                    affected_weeks.add(int(w))
        if not affected_weeks:
            affected_weeks.add(ctx["week"])
    free_options = marginal.price_options(b, drops, free, affected_weeks)
    wire_options = marginal.price_options(b, drops, wire, affected_weeks)
    if ctx["mode"] == "ros":
        free_options = _controlled_ros_options(ctx, table, free_options)
        wire_options = _controlled_ros_options(ctx, table, wire_options)
        # Do not let an unsafe cheapest incumbent consume the mutation slot and
        # hide the next coverage-safe one.  Retain original drop-price order.
        usable = {o["drop"] for o in free_options + wire_options}
        drops = [pid for pid in drops if pid in usable]
    return {"board": b, "drops": drops,
            "free": free_options, "wire": wire_options}


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
    """The complete FAAB portfolio: capacity, skill-drop and DEF groups.

    Every claim in one slot's list names the SAME drop. Sleeper assigns settled
    processing priority by descending bid, with creation order breaking equal
    bids; the first winner takes the slot and the rest bounce off a player who
    is no longer on our roster, at no cost. That is the whole point -- we get
    our best AVAILABLE outcome instead of our best guess.

    The rungs are priced as a DESCENDING LADDER off this league's own bid
    history rather than as one bid repeated: the top rung pays a real price for
    the man we want, and the cheap rungs sit where the record says claims still
    convert. See robo/faab.py, including why P(win | bid) is not estimated.
    """
    if ctx["mode"] == "news":
        # Event discovery may decide that a claim deserves attention, but the
        # live writer always rebuilds the COMPLETE ROS portfolio. Keeping this
        # branch makes news-mode dry-run audits readable without authorising an
        # isolated claim to stack on top of an existing Tuesday slate.
        return _news_channel_plans(ctx, "claims")
    P = priced(ctx)
    board = P["board"]
    slates = []
    open_slots = int((ctx.get("slots") or {}).get("open") or 0) if "slots" in ctx \
        else (1 if None in P["drops"] else 0)

    def _rows(drop):
        """Every option against one drop, priced and ranked. Ranking is by BID,
        which is to say by value: Sleeper works the global queue in descending
        bid, so ordering on price IS ordering on preference and the old
        per-group descending ladder is redundant. Coverage breaks an equal
        price and nothing more -- if the floor is genuinely worth more than the
        best receiver on the wire, that belongs in the BID, not in a sort that
        overrides one."""
        alt = best_free(P["free"], drop)
        out = []
        for o in (x for x in P["wire"] if x["drop"] == drop):
            if not clears(o):
                continue
            row = _option_row(ctx, board, o)
            row["over_free"] = alt["add"] if alt else None
            row["over_free_gain"] = (round(o["gain"] - alt["gain"], 3)
                                     if alt else None)
            out.append(row)
        _price_claims(ctx, out)
        out.sort(key=lambda r: (-int(r["bid"]), int(r.get("coverage_priority") or 0),
                                -float(r["gain"]), r["add"]["player_id"]))
        return out

    # THE CAPACITY POOL IS TIER ONE, and its top `capacity` claims cannot be
    # blocked by anything else we submit -- win or lose, those men are resolved
    # before a later tier could matter.
    ranked = _rows(None) if (None in P["drops"] and open_slots > 0) else []
    if ranked:
        slates.append({"group_id": "open", "kind": "open",
                       "capacity": max(1, open_slots),
                       "drop": ranked[0]["drop"],
                       "drop_value": ranked[0]["drop_value"],
                       "claims": ranked[:SLATE_DEPTH]})

    # A LATER TIER ONLY COVERS MEN OUR OWN HIGHER CLAIMS COULD HAVE BLOCKED.
    # If a player tops the capacity pool, duplicating him into a drop tier buys
    # nothing: either we are high bid and he fills the open slot, or he goes to
    # another roster and the second claim bounces off a player who is already
    # gone. He only needs the drop route when a claim we rank ABOVE him might
    # take the slot out from under him first -- then his capacity claim fails
    # on "too many players" while he is still unowned, and the drop route is
    # the live one.
    unblockable = {r["add"]["player_id"] for r in ranked[:max(1, open_slots)]}
    skill_drops = 0
    for drop in P["drops"]:
        if drop is None or skill_drops >= ROS_MAX_MUTATIONS:
            continue
        pos = (((ctx.get("players") or {}).get(str(drop)) or {}).get("position")
               or ((ctx.get("by_id") or {}).get(str(drop)) or {}).get("pos"))
        # K stays on the free patch path and DEF owns its dedicated group.
        if pos in {"K", "DEF"}:
            continue
        picks = [r for r in _rows(drop)
                 if r["add"]["player_id"] not in unblockable][:SLATE_DEPTH]
        if not picks:
            continue
        skill_drops += 1
        slates.append({"group_id": f"skill:{drop}", "kind": "skill", "capacity": 1,
                       "drop": picks[0]["drop"],
                       "drop_value": picks[0]["drop_value"], "claims": picks})

    # THE DEFENCE GROUP MUST NOT BE ABLE TO DISCARD THE SLATE. It is the only
    # part of this planner that makes live Sleeper reads, and it makes them
    # AFTER the simulator has spent a quarter of an hour pricing everything
    # else -- so one dropped connection was throwing away the whole priced
    # board and leaving waiver night with nothing to submit. Measured
    # 15 Sep 2026: a reset inside season.week_points took out a run that had
    # already produced 122 wire options. It is also the LAST portfolio
    # priority, so losing it costs the least. Recorded rather than swallowed.
    try:
        defence = _plan_defence_claims(ctx)
    except Exception as e:
        defence = None
        ctx["_defence_group_error"] = f"{type(e).__name__}: {e}"
    if defence:
        _price_claims(ctx, defence["claims"])
        slates.append(defence)

    _apply_exposure_cap(ctx, slates)

    # This is our predicted processing priority, not a value sent to Sleeper.
    # Settled history shows Sleeper assigns its global seq by descending bid;
    # equal-bid claims retain submission order.  Keep the field for compatibility
    # but call it priority everywhere user-facing until the settled seq exists.
    # Coverage priority sits between the bid and the value, because equal bids
    # are the common case here -- the ladder quotes $1 across a whole slate in a
    # quiet week -- and on an equal bid the tiebreak decides who Sleeper reaches
    # first. Without it the floor-relieving rung loses the tie to a bigger
    # number and the ordering the slate was built to express is thrown away at
    # the last step.
    group_order = {"open": 0, "skill": 1, "def": 2}
    flat = [(s, c) for s in slates for c in s["claims"]]
    flat.sort(key=lambda sc: (-sc[1]["bid"],
                              group_order.get(sc[0].get("kind"), 9),
                              sc[1].get("coverage_priority", 0),
                              -sc[1]["gain"], sc[1]["add"]["player_id"]))
    for i, (_, c) in enumerate(flat):
        c["seq"] = i
        c["priority"] = i
        c["submit_order"] = i
    ctx["_claim_exposure"] = sum(int(s.get("exposure") or 0) for s in slates)
    return slates


def _plan_defence_claims(ctx: dict) -> dict | None:
    """One dedicated DEF stream group, independent of the skill simulator.

    It took the cross-group dedupe set when one existed. That could never fire:
    _priced's shortlist strips K and DEF, so no defence has ever been a
    candidate in any other group.
    """
    if ctx.get("mode") != "ros":
        return None
    from robo import streaming
    held = [str(pid) for pid in (ctx.get("roster", {}).get("players") or [])
            if (((ctx.get("players") or {}).get(str(pid)) or {}).get("position")
                or ((ctx.get("by_id") or {}).get(str(pid)) or {}).get("pos")) == "DEF"]
    if not held:
        return None
    board = streaming.rank_week(ctx["week"], pos="DEF")
    locks = season.week_points(ctx["week"], season.SEASON, ctx["league_id"])
    mine_rows = [row for row in board
                 if row["team"] in held
                 and not (locks.get(row["team"]) or {}).get("locked")]
    if not mine_rows:
        return None
    # If two defences are temporarily rostered, replace the weaker unlocked one.
    mine = min(mine_rows, key=lambda row: (row["pts"], row["team"]))
    incumbent = str(mine["team"])
    rostered = season.rostered_ids(ctx["league_id"])
    picks = []
    for row in board:
        pid = str(row["team"])
        state = (ctx.get("eligibility") or {}).get(pid) or {}
        if (pid in rostered
                or state.get("acquisition") not in {"weekly_waiver", "drop_waiver"}
                or state.get("roster_movement") == "roster_locked"
                or (locks.get(pid) or {}).get("locked")):
            continue
        gain = round(float(row["pts"]) - float(mine["pts"]), 2)
        if gain < streaming.MIN_STREAM_GAIN:
            continue
        add_name = ((ctx.get("by_id") or {}).get(pid) or {}).get("name") or pid
        drop_name = ((ctx.get("by_id") or {}).get(incumbent) or {}).get("name") or incumbent
        picks.append({
            "add": {"player_id": pid, "name": add_name, "pos": "DEF"},
            "drop": {"player_id": incumbent, "name": drop_name, "pos": "DEF"},
            "gain": gain, "add_value": float(row["pts"]),
            "drop_value": float(mine["pts"]), "real": True,
            "se": 0.0, "ceiling": gain, "coverage_priority": 0,
            "bid_gain": gain, "bid_se": 0.0,
            "opponent_field": {"available": False,
                               "reason": "DEF uses the validated pooled positional model"},
            "why": (f"defence stream: {pid} {row['pts']:.2f} against "
                    f"{incumbent} {mine['pts']:.2f} on this week's lines")})
        if len(picks) >= SLATE_DEPTH:
            break
    if not picks:
        return None
    return {"group_id": f"def:{incumbent}", "kind": "def", "capacity": 1,
            "drop": picks[0]["drop"], "drop_value": picks[0]["drop_value"],
            "claims": picks}


def _quote_at_bid(q: dict, bid: int, reason: str) -> dict:
    """Keep quote audit fields aligned when portfolio exposure limits a bid."""
    bid = max(0, int(bid))
    if bid == int(q.get("bid") or 0):
        return q
    row = next((r for r in (q.get("curve") or []) if int(r["bid"]) == bid), None)
    out = {**q, "bid": bid, "portfolio_limited": True,
           "reason": q.get("reason", "") + f"; {reason}"}
    if row:
        out.update({"p_win": row.get("p_win"),
                    "expected_utility": row.get("expected_utility")})
    return out


def _price_claims(ctx: dict, rows: list[dict]) -> list[dict]:
    """ONE PRICE PER PLAYER, and never a price per route.

    What a man is worth to us does not change with whether acquiring him
    happens to fill an empty slot or force a drop. Pricing him inside his group
    made it change: the same receiver came back at $7 as the lead rung of a
    drop tier and $1 three rungs down the capacity pool, which says we would
    pay more for the WORSE version of the same acquisition. Cached on the
    context so a man who appears in two tiers carries one number into both.
    """
    from robo import faab_field
    cache = ctx.setdefault("_claim_prices", {})
    budget = max(0, int(ctx.get("faab") or 0))
    for p in rows:
        pid = p["add"]["player_id"]
        if pid not in cache:
            field = p.get("opponent_field")
            if field is None:
                try:
                    field = faab_field.predict(ctx, pid, _expected_table(ctx))
                except Exception as e:
                    field = {"available": False,
                             "reason": f"{type(e).__name__}: {e}"}
            q = faab.quote(float(p.get("bid_gain", p["gain"])), ctx["week"],
                           budget, p["add"].get("pos"),
                           field if field.get("available") else None)
            bid = int(q["bid"])
            # Zero today and settable; a registry constant that quietly stops
            # being read is worse than no constant.
            if faab.MIN_LIVE_BID:
                bid = max(int(faab.MIN_LIVE_BID), bid)
            cache[pid] = (bid, q, field)
        bid, q, field = cache[pid]
        p.update({"bid": bid, "bid_gain": p.get("bid_gain", p["gain"]),
                  "bid_se": p.get("bid_se", p.get("se")),
                  "bid_quote": q, "opponent_field": field})
    return rows


def _apply_exposure_cap(ctx: dict, slates: list[dict]) -> None:
    """Worst case is one winner per group. Hold that inside the budget.

    Only the lower tiers yield, and only when the budget actually binds -- the
    top claim's price is what the engine thinks the man is worth and is not
    negotiable against a constraint that is not biting. A man listed in two
    tiers cannot win twice, and the capacity pool's unblockable men are already
    excluded from the drop tiers, so no group's exposure counts him twice.
    """
    def _limit(p, ceiling):
        if int(p["bid"]) <= ceiling:
            return
        q = p.get("bid_quote") or {}
        row = next((r for r in (q.get("curve") or [])
                    if int(r["bid"]) == ceiling), None)
        p["bid_quote"] = {**q, "bid": ceiling, "portfolio_limited": True,
                          "reason": (q.get("reason", "")
                                     + "; limited by portfolio FAAB exposure"),
                          **({"p_win": row.get("p_win"),
                              "expected_utility": row.get("expected_utility")}
                             if row else {})}
        p["bid"] = ceiling

    order = {"open": 0, "skill": 1, "def": 2}
    slates.sort(key=lambda s: order.get(s.get("kind"), 9))
    remaining = max(0, int(ctx.get("faab") or 0))
    for slate in slates:
        picks = slate.get("claims") or []
        capacity = max(1, int(slate.get("capacity") or 1))
        # A pool of capacity N can land N winners AT ONCE, so its own rungs
        # stack against each other before any later tier is reached. Walk them
        # in order and charge each against what the ones above it left: the top
        # claim keeps the price the engine set and the rungs beneath it yield.
        # A rung past the capacity cut cannot stack -- it only wins when one
        # above it lost -- so it is held to the group's budget, not the
        # remainder.
        left = remaining
        for i, p in enumerate(picks):
            p.update({"priority": i, "group_id": slate["group_id"],
                      "claim_kind": slate["kind"], "group_capacity": capacity})
            _limit(p, left if i < capacity else remaining)
            if i < capacity:
                left -= int(p["bid"])
        slate["exposure"] = sum(sorted((int(p.get("bid") or 0) for p in picks),
                                       reverse=True)[:capacity])
        remaining = max(0, remaining - slate["exposure"])


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


def claim_payload(add_id: str, drop_id: str | None, roster_id: int, bid: int) -> dict:
    """Exactly what sleeper_write.submit_waiver_claim would send.

    EXACTLY, including the empty drop arrays on a no-drop claim. This omitted
    the keys instead, and the capacity pool is entirely no-drop claims -- so on
    a slate of six, five printed one shape for review and would have gone out
    as another. A dry run whose whole job is to be approved by a human must not
    be the one place the payload is tidied up.

    The `waiver_bid` key is confirmed -- it was read off this league's own
    completed waiver transactions -- and so is the parallel-array form, which
    came back as `settings: {"waiver_bid": 1}` on a live submission. Every
    claim is still read back from the pending queue before the next one is
    sent: a mis-encoded bid reads as 0, the claim still looks submitted, and
    the player goes to anyone who bid a dollar. That read-back now lives in
    waiver_manager._submit, which compares the whole add/drop/bid identity
    rather than the bid alone.
    """
    return {"k_adds": [add_id], "v_adds": [roster_id],
            "k_drops": ([drop_id] if drop_id is not None else []),
            "v_drops": ([roster_id] if drop_id is not None else []),
            "k_settings": ["waiver_bid"], "v_settings": [bid]}


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
    # Patch mode owns lineup-legality diagnosis. Recomputing three optimized
    # lineups twice merely to decorate a news-mode report burns reaction time
    # and cannot alter the news verdict.
    hs = [] if ctx["mode"] == "news" else holes(ctx)
    for x in hs:
        gaps = ", ".join(x["empty"] + [f"{s} (unstartable)" for s in x["unstartable"]])
        L.append(f"  !! week {x['week']} cannot be filled: {gaps}")
    return L


def render_free(ctx: dict, plans: list[dict]) -> str:
    L = [f"FREE AGENTS - {len(ctx['available']) - len(ctx['on_waivers'])} available "
         f"now, {len(ctx['on_waivers'])} still on waivers"] + _header(ctx)
    block = ctx.get("_evaluation_block") or evaluation_block(ctx)
    if block:
        L.append(f"  BLACKED OUT: {block}")
        return "\n".join(L)
    if ctx["mode"] == "news":
        L.append("  policy: positive causal event delta, then weekly-model ROS "
                 "must beat the cheapest coverage-safe incumbent")
    if not plans:
        from robo import marginal
        L.append("  nothing clears the bar: a starting upgrade must add "
                 f"{MIN_GAIN_TO_ADD:g}+ points to the simulated lineup, a bench "
                 f"spot must reach a ceiling of {marginal.HIT_POINTS:g}, and both "
                 f"must beat {NOISE_MULTIPLE:g}x the simulator's own error"
                 if ctx["mode"] == "ros" else "  nothing to do in this mode")
        if ctx["mode"] == "news":
            for x in (ctx.get("_news_rejections") or [])[:12]:
                pid = x.get("player_id")
                name = (ctx["by_id"].get(pid) or {}).get("name") or pid
                L.append(f"    REJECT {name}: {x.get('reason')}")
    for p in plans:
        L.append(f"  ADD  {p['add']['name']:<24} {p['add']['pos']:<4} "
                 f"{p['add_value']:>7.1f}{_tag(p['real'])}")
        L.append(f"  DROP {p['drop']['name']:<24} {p['drop']['pos']:<4} "
                 f"{p['drop_value']:>7.1f}   gain {p['gain']:+.1f}"
                 + (f"   {p['why']}" if p.get("why") else ""))
    return "\n".join(L)


def render_claims(ctx: dict, slates: list[dict]) -> str:
    L = [f"WAIVER SLATE - {len(ctx['on_waivers'])} player(s) on waivers"] + _header(ctx)
    block = evaluation_block(ctx)
    ctx["_evaluation_block"] = block
    if block:
        L.append(f"  BLACKED OUT: {block}")
        return "\n".join(L)
    if ctx["mode"] == "news":
        L.append("  policy: same weekly-model ROS comparison as free agents; "
                 "at most one event proposal across both channels")
    if not slates:
        L.append("  no claim clears the bar")
    for s in slates:
        if s["drop"]["player_id"] is None:
            L.append(f"  open-slot capacity pool ({s.get('capacity', 1)} winner(s) max), "
                     "nobody dropped - fallback list:")
        elif s.get("kind") == "def":
            L.append(f"  defence stream replacing {s['drop']['name']} - "
                     "first winner takes the slot:")
        else:
            L.append(f"  slot freed by dropping {s['drop']['name']} "
                     f"({s['drop_value']:.1f}) - priority list, first winner takes it:")
        for c in s["claims"]:
            # LEAD WITH THE NUMBER THE DECISION WAS MADE ON. In ordinary ros
            # claims `gain` and `bid_gain` are the same simulated quantity and
            # this changes nothing. In news mode they are not: `gain` there is a
            # raw rest-of-season difference, and against an empty roster spot it
            # degenerates to the candidate's whole season total -- the
            # add_value-minus-drop_value shape marginal.py exists to replace. It
            # printed "gain +32.26" beside a paired value of +1.34 for a third
            # string quarterback. The ROS comparison is still the news channel's
            # admission test, so it stays on the line; it just stops sitting
            # where a reader takes the decision number to be.
            paired = c.get("bid_gain")
            headline = (f"gain {c['gain']:+.1f}" if paired is None
                        or abs(float(paired) - float(c["gain"])) < 0.05
                        else f"lineup {float(paired):+.2f} (ros {c['gain']:+.1f})")
            L.append(f"    priority {c.get('priority', c.get('seq', 0)):<3} ${c['bid']:<4} "
                     f"{c['add']['name']:<24} {c['add']['pos']:<4} "
                     f"{c['add_value']:>7.1f}  {headline}{_tag(c['real'])}"
                     + (f"   {c['why']}" if c.get("why") else ""))
            q = c.get("bid_quote") or {}
            if q:
                band = q.get("near_optimal") or [c["bid"], c["bid"]]
                L.append(f"      paired bid value {float(c.get('bid_gain') or 0):+.2f} "
                         f"+/- {float(c.get('bid_se') or 0):.2f}; "
                         f"P(win) {float(q.get('p_win') or 0):.0%}; "
                         f"expected utility {float(q.get('expected_utility') or 0):.2f}; "
                         f"near-optimal ${band[0]}-${band[1]}")
                if q.get("expected_highest") is not None:
                    hs = q.get("highest_quantiles") or {}
                    L.append(f"      opponent field: expected high "
                             f"${float(q['expected_highest']):.1f}, "
                             f"p50/p75/p90 ${hs.get('p50', 0)}/"
                             f"${hs.get('p75', 0)}/${hs.get('p90', 0)}")
            field = c.get("opponent_field") or {}
            if field.get("available"):
                likely = sorted(field.get("opponents") or [],
                                key=lambda o: -float(o.get("p_claim") or 0))[:3]
                for o in likely:
                    need = o.get("need") or {}
                    L.append(f"      {o.get('manager')}: "
                             f"P(claim) {float(o.get('p_claim') or 0):.0%}, "
                             f"need {need.get('need')} ({float(need.get('gain') or 0):+.1f}), "
                             f"bid p50/p75 ${o.get('bid_p50')}/${o.get('bid_p75')}, "
                             f"${o.get('faab_left')} left")
            elif field:
                L.append(f"      opponent model unavailable: {field.get('reason')}")
    total = sum(int(s.get("exposure") or 0) for s in slates)
    if slates:
        moves_at_risk = sum(int(s.get("capacity") or 1) for s in slates)
        L.append(f"  worst-case exposure: {moves_at_risk} move(s), ${total}; "
                 "open pools count their highest capacity bids")
    if ctx.get("_defence_group_error"):
        L.append(f"  no defence stream this pass: {ctx['_defence_group_error']}")
    return "\n".join(L)


# ------------------------------------------------------------------------ run

def _record(kind: str, title: str, decision: str, why: str, data: dict) -> None:
    from robo import decisions
    try:
        decisions.record(kind, title, decision, why, data=data)
    except Exception as e:  # a published record must never cost us the move
        print(f"  ** decision log failed ({type(e).__name__}); the move stands")


def _after_free_context(ctx: dict, plan: dict) -> dict:
    """Roster context after the first free move, without touching Sleeper.

    This is the dry-run equivalent of submitting the free acquisition and
    pulling the roster again.  It exists so a closed transaction gate still
    shows whether the waiver claim survives the roster change it depends on.
    """
    add_id = str(plan["add"]["player_id"])
    drop_id = plan["drop"].get("player_id")
    roster = dict(ctx["roster"])
    held = [str(pid) for pid in (roster.get("players") or [])
            if str(pid) != str(drop_id)]
    if add_id not in held:
        held.append(add_id)
    roster["players"] = held
    roster["starters"] = [str(pid) for pid in (roster.get("starters") or [])
                          if str(pid) != str(drop_id)]
    roster["reserve"] = [str(pid) for pid in (roster.get("reserve") or [])
                         if str(pid) != str(drop_id)]
    # RECOUNT THE SLOTS. Everything else here is rebuilt from the hypothetical
    # roster and `slots` was being copied through unchanged, which is only
    # harmless while the free channel cannot take the open spot -- it skips a
    # None drop in ros mode for exactly that reason. If that ever changes back,
    # a stale `open` here would have the claims channel bid for a slot the free
    # pass just filled. Cheap to keep honest, and it is derived, not fetched.
    active = [pid for pid in held if pid not in set(roster.get("reserve") or [])]
    slots = dict(ctx.get("slots") or {})
    slots["active"] = len(active)
    slots["open"] = max(0, int(slots.get("roster_max") or len(active)) - len(active))
    return {
        **{k: v for k, v in ctx.items() if not k.startswith("_")},
        "roster": roster,
        "slots": slots,
        "starters": set(roster.get("starters") or []),
        "reserve": set(roster.get("reserve") or []),
        "available": [row for row in ctx["available"]
                      if str(row["player_id"]) != add_id],
        "_expected_table": _expected_table(ctx),
    }


def run_ros_sequence(apply: bool = False, league_id: str = LEAGUE_ID_2026,
                     verbose: bool = True) -> dict:
    """Secure the best free improvement, then reprice the waiver board.

    Availability changes transaction mechanics, not player valuation.  The
    immediate move is evaluated first because it can be banked now.  A waiver
    claim is then evaluated from the roster that move creates, and only the bid
    calculation is waiver-specific.
    """
    ctx = _context(league_id, mode="ros")
    free = run("free", apply=apply, league_id=league_id, mode="ros",
               verbose=verbose, _ctx=ctx)
    proposed = (free.get("plans") or [None])[0]

    if proposed:
        if apply and value.may_submit():
            season.invalidate_live()
            claims_ctx = _context(league_id, mode="ros")
            basis = ("live roster after the completed free-agent move"
                     if free.get("submitted") else
                     "live roster after the proposed free-agent move did not complete")
        else:
            claims_ctx = _after_free_context(ctx, proposed)
            basis = "hypothetical roster after the proposed free-agent move"
    else:
        claims_ctx = ctx
        basis = "current roster; no free-agent move cleared"

    if verbose:
        print(f"\nWAIVER RE-EVALUATION - {basis}")
    # The claims pass is the immutable checkpoint for the whole ordered run.
    # Carry the already-finished free-agent decision into it so the audit can
    # explain both halves without rerunning either calculation later.
    claims_ctx["_sequence_basis"] = basis
    claims_ctx["_sequence_free_audit"] = free.get("decision_audit") or {}
    claims_ctx["_sequence_free_plans"] = free.get("plans") or []
    claims = run("claims", apply=apply, league_id=league_id, mode="ros",
                 verbose=verbose, _ctx=claims_ctx)
    return {"mode": "ros-sequence", "week": ctx["week"], "basis": basis,
            "free": free, "claims": claims,
            "applied": bool(free.get("applied") or claims.get("applied")),
            "submitted": list(free.get("submitted") or [])
                         + list(claims.get("submitted") or [])}


def maintain_pending_claims(apply: bool = False,
                            league_id: str = LEAGUE_ID_2026,
                            reason: str = "roster-state check") -> dict:
    """Cheap no-op while state is unchanged; full reslate when it is not.

    The news pulse and successful roster/lineup/IR writes call this.
    Monday preserves the existing guard: provably unsafe owned claims may be
    cancelled, but no new waiver transaction is submitted.
    """
    from robo import waiver_manager
    roster = season.mine(league_id)
    snap = waiver_manager.inspect(league_id, roster["roster_id"],
                                  season.current_week())
    if snap["foreign"]:
        if apply:
            waiver_manager._alert(
                "WAIVER AUTOMATION BLOCKED: an unowned pending claim is present.",
                "waiver-foreign-claim")
        return {"status": "blocked_foreign", "foreign": snap["foreign"]}
    if not snap["pending"]:
        return {"status": "idle", "pending": 0,
                "settled": snap.get("settled") or []}
    saved = list((snap["state"].get("active") or {}).values())
    relevant = ({x.get("spec", {}).get("add_id") for x in saved} |
                {x.get("spec", {}).get("drop_id") for x in saved})
    ctx = _context(league_id, mode="ros")
    fp = waiver_manager.context_fingerprint(ctx, relevant)
    if fp == snap["state"].get("last_fingerprint"):
        return {"status": "unchanged", "pending": len(snap["pending"])}
    if season.monday_guard_active():
        unsafe = []
        for txid, item in (snap["state"].get("active") or {}).items():
            spec = item.get("spec") or {}
            why = _transaction_recheck(
                ctx, spec.get("add_id"), spec.get("drop_id"),
                {"claim_kind": spec.get("kind")}, claim=True)
            if why:
                unsafe.append(str(txid))
        result = waiver_manager.cancel_owned(
            unsafe, league_id=league_id, roster_id=roster["roster_id"],
            week=ctx["week"], reason=f"Monday safety check: {reason}", apply=apply)
        return {"status": "monday_cancel_only", "unsafe": unsafe,
                "result": result}
    result = run("claims", apply=apply, league_id=league_id, mode="ros",
                 verbose=False, _ctx=ctx, source=reason)
    blocked = result.get("control_block") or result.get("blackout")
    if blocked:
        # A changed fingerprint means the old slate is no longer backed by the
        # current facts. If a fresh valuation cannot be produced, retain no
        # stale automated claims.
        ids = list((snap["state"].get("active") or {}).keys())
        cancelled = waiver_manager.cancel_owned(
            ids, league_id=league_id, roster_id=roster["roster_id"],
            week=ctx["week"], reason=f"replan blocked: {blocked}", apply=apply)
        if apply and cancelled.get("applied"):
            waiver_manager._alert(
                "WAIVER PORTFOLIO CANCELLED: live facts changed and a safe replan was blocked.",
                "waiver-replan-blocked")
        return {"status": "replan_blocked", "why": blocked,
                "cancelled": cancelled, "result": result}
    return {"status": "reslated", "result": result}


def _ordinary_audit_snapshot(ctx: dict, plans: list[dict], channel: str) -> dict:
    """Freeze the candidates and bars behind one ordinary ROS decision.

    The UI must not recreate a historical near-miss from today's settings.
    This is therefore assembled while the real planner's Board, thresholds and
    control verdicts are still in memory, then written beside the decision.
    """
    from robo import marginal

    priced_board = ctx.get("_priced") or {}
    selected = set()
    if channel == "free":
        selected = {(str((p.get("add") or {}).get("player_id")),
                     ((p.get("drop") or {}).get("player_id"))) for p in plans}
    else:
        selected = {(str((c.get("add") or {}).get("player_id")),
                     ((s.get("drop") or {}).get("player_id")))
                    for s in plans for c in (s.get("claims") or [])}

    def identity(pid) -> dict:
        if pid is None:
            return {"player_id": None, "name": "(open roster spot)", "pos": "--"}
        row = ctx.get("by_id", {}).get(str(pid)) or {}
        if not row:
            p = ctx.get("players", {}).get(str(pid)) or {}
            row = {"name": " ".join(x for x in (p.get("first_name"),
                                                  p.get("last_name")) if x),
                   "pos": p.get("position")}
        return {"player_id": str(pid), "name": row.get("name") or str(pid),
                "pos": row.get("pos") or row.get("position")}

    rows = []
    options = priced_board.get("free" if channel == "free" else "wire") or []
    for option in options:
        add_id, drop_id = str(option["add"]), option.get("drop")
        gain = float(option.get("gain") or 0)
        se = float(option.get("se") or 0)
        ceiling = float(option.get("ceiling") or 0)
        starter = bool(option.get("starter"))
        noise_margin = gain - NOISE_MULTIPLE * se
        policy_margin = (gain - MIN_GAIN_TO_ADD if starter
                         else ceiling - marginal.HIT_POINTS)
        picked = (add_id, drop_id) in selected
        cov_priority = int(option.get("coverage_priority") or 0)
        best_priority = min((int(o.get("coverage_priority") or 0)
                             for o in options), default=0)
        if picked:
            verdict = "selected"
        elif noise_margin <= 0:
            verdict = "below simulator noise"
        elif policy_margin < 0:
            verdict = ("below starting-gain bar" if starter
                       else "below bench-ceiling bar")
        elif cov_priority > best_priority:
            # NOT THE SAME THING AS LOSING ON VALUE, and the old text said it
            # was. Since the coverage floor became an ordering rather than a
            # veto, an option can clear every bar and still sit below a smaller
            # gain because that one refills a position we are short at. A
            # reader who cannot tell those apart cannot check the policy.
            verdict = "cleared; outranked by the coverage floor"
        else:
            verdict = "cleared; lower-ranked option"
        rows.append({"add": identity(add_id), "drop": identity(drop_id),
                     "gain": round(gain, 3), "se": round(se, 3),
                     "ceiling": round(ceiling, 3), "starter": starter,
                     "noise_margin": round(noise_margin, 3),
                     "policy_margin": round(policy_margin, 3),
                     "selected": picked, "verdict": verdict,
                     "coverage": option.get("coverage"),
                     "coverage_priority": cov_priority,
                     "direct_ros": option.get("direct_ros")})
    # Ordered the way the ladder is actually built, so the table a reader scans
    # top-down matches the priority list that was submitted.
    rows.sort(key=lambda r: (not r["selected"], r["coverage_priority"],
                             -min(r["noise_margin"], r["policy_margin"]),
                             r["add"]["player_id"]))
    controls = []
    for check in ctx.get("_ros_control_checks") or []:
        controls.append({**check, "add": identity(check.get("add_id")),
                         "drop": identity(check.get("drop_id"))})
    return {
        "channel": channel,
        "thresholds": {"noise_multiple": NOISE_MULTIPLE,
                       "starting_gain": MIN_GAIN_TO_ADD,
                       "bench_ceiling": marginal.HIT_POINTS,
                       "drop_floor": DROP_FLOOR,
                       "skill_drop_limit": ROS_MAX_MUTATIONS,
                       "worst_case_faab": ctx.get("_claim_exposure", 0),
                       "coverage_floor": dict(MIN_ROSTER_COVERAGE)},
        "drop_checks": ctx.get("_ros_drop_checks") or [],
        "control_checks": controls,
        "options": rows,
    }


def run(channel: str, apply: bool = False, league_id: str = LEAGUE_ID_2026,
        mode: str = "ros", verbose: bool = True,
        affected: set[str] | None = None, _ctx: dict | None = None,
        ir_open_slots: int = 0, ir_moves: list[dict] | None = None,
        excluded: set[str] | None = None, source: str | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    ctx = _ctx or _context(league_id, mode, affected=affected,
                           ir_open_slots=ir_open_slots, ir_moves=ir_moves,
                           excluded=excluded)
    block = evaluation_block(ctx)
    ctx["_evaluation_block"] = block

    if mode == "news" and not ctx["affected"]:
        raise ValueError("news mode requires affected player ids")

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
    timing_block = blacked_out(ctx)
    out = {"channel": channel, "mode": mode, "week": ctx["week"], "plans": plans,
           "payloads": payloads, "applied": False, "submitted": [],
           "gated": not value.may_submit(), "blackout": timing_block,
           "control_block": (block if block != timing_block else ""),
           "roster_control_checks": ctx.get("_ros_control_checks") or []}
    if mode == "ros":
        out["decision_audit"] = _ordinary_audit_snapshot(ctx, plans, channel)
    if channel == "claims" and mode == "ros":
        try:
            from robo import waiver_audit
            path = waiver_audit.record(ctx, plans, out)
            out["audit_path"] = str(path) if path else None
        except Exception as e:
            out["audit_error"] = f"{type(e).__name__}: {e}"

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
    if not apply or block or (not plans and channel == "free"):
        return out

    if channel == "free":
        return submit_free(ctx, plans, out, league_id)

    rid = ctx["roster"]["roster_id"]
    desired = []
    for s in plans:
        for c in sorted(s["claims"], key=lambda x: x["seq"]):
            add, drop_id = c["add"], s["drop"].get("player_id")
            why = _transaction_recheck(ctx, add.get("player_id"), drop_id, c,
                                       claim=True)
            if why:
                print(f"  ** CLAIM SKIPPED {add['name']}: {why}")
                continue
            desired.append({"add_id": add["player_id"], "drop_id": drop_id,
                            "bid": c["bid"], "group_id": s.get("group_id"),
                            "kind": s.get("kind"),
                            "submit_order": c.get("submit_order", c["seq"]),
                            "priority": c["seq"], "add_name": add["name"],
                            "drop_name": s["drop"].get("name"),
                            "gain": c.get("gain"),
                            "capacity": s.get("capacity", 1)})
    from robo import waiver_manager
    relevant = {s.get("add_id") for s in desired} | {s.get("drop_id") for s in desired}
    fp = waiver_manager.context_fingerprint(ctx, relevant)
    rec = waiver_manager.reconcile(
        desired, league_id=league_id, roster_id=rid, week=ctx["week"],
        source=source or ("weekly" if mode == "ros" else mode),
        apply=True, fingerprint=fp)
    out["reconciliation"] = rec
    out["submitted"] = rec.get("submitted") or []
    out["applied"] = bool(rec.get("applied"))
    if rec.get("blocked"):
        print(f"  ** WAIVER PORTFOLIO BLOCKED: {rec['blocked']}")
    if rec.get("error"):
        print(f"  ** WAIVER PORTFOLIO FAILED: {rec['error']}")
    # Pending claims are deliberately absent from the PUBLIC decision log.
    # Publishing this ladder here exposed both targets and exact bids before
    # waivers ran. waiver_manager retains the full local audit and publishes
    # the confirmed outcomes only after every claim in the submitted batch has
    # left Sleeper's pending queue. Replaced/cancelled claims are never public.
    return out


def run_ir_fills(opened: int, ir_moves: list[dict] | None = None,
                 apply: bool = False, league_id: str = LEAGUE_ID_2026,
                 verbose: bool = True) -> dict:
    """Fill only active-roster spots created by this run's IR sweep.

    Dry runs return the complete ordered proposal list. A live run deliberately
    prices one spot at a time and rebuilds the eligibility/roster snapshot after
    every attempt. If the top player loses an availability race, the next pass
    excludes him and tries the next-highest valid ROS player.
    """
    opened = max(0, int(opened))
    if opened <= 0:
        return {"channel": "free", "mode": "ir_fill", "plans": [],
                "payloads": [], "applied": False, "submitted": [],
                "gated": not value.may_submit(), "opened": 0}
    if not (apply and value.may_submit()):
        out = run("free", apply=apply, league_id=league_id, mode="ir_fill",
                  verbose=verbose, ir_open_slots=opened, ir_moves=ir_moves)
        out["opened"] = opened
        return out

    aggregate = {"channel": "free", "mode": "ir_fill", "plans": [],
                 "payloads": [], "applied": False, "submitted": [],
                 "gated": False, "opened": opened}
    excluded: set[str] = set()
    # The board is finite; the bound also prevents an unexpected API failure
    # from becoming an unbounded scheduled-task loop.
    attempts = 0
    while len(aggregate["submitted"]) < opened and attempts < 25:
        attempts += 1
        season.invalidate_live()
        out = run("free", apply=True, league_id=league_id, mode="ir_fill",
                  verbose=verbose, ir_open_slots=1, ir_moves=ir_moves,
                  excluded=excluded)
        aggregate["plans"] += out.get("plans") or []
        aggregate["payloads"] += out.get("payloads") or []
        got = out.get("submitted") or []
        aggregate["submitted"] += got
        aggregate["applied"] = aggregate["applied"] or bool(got)
        if got:
            continue
        plan = (out.get("plans") or [None])[0]
        if not plan:
            break
        excluded.add(str(plan["add"]["player_id"]))
    aggregate["attempts"] = attempts
    return aggregate


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--free", action="store_true", help="instant wire adds")
    g.add_argument("--claims", action="store_true", help="the FAAB slate")
    g.add_argument("--sequence", action="store_true",
                   help="ROS: free move first, then re-evaluate waiver claims")
    ap.add_argument("--mode", default="ros", choices=MODES)
    ap.add_argument("--affected", default="",
                    help="comma-separated player ids for news mode")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--league", default=LEAGUE_ID_2026)
    ap.add_argument("--payloads", action="store_true",
                    help="print the exact GraphQL variables that would be sent")
    args = ap.parse_args()
    affected = {p.strip() for p in args.affected.split(",") if p.strip()}
    channel = "free" if args.free else "claims"
    if args.mode == "ros":
        # Do not price a weekly move while a news/full cascade is replacing the
        # exact expected.py vintage the simulator reads.
        from robo.runlock import DecisionRun
        with DecisionRun("ROS transaction evaluation", wait_s=15 * 60):
            if args.sequence:
                res = run_ros_sequence(apply=args.apply, league_id=args.league)
            else:
                res = run(channel, apply=args.apply, league_id=args.league,
                          mode=args.mode, affected=affected)
    else:
        if args.sequence:
            ap.error("--sequence is only valid with --mode ros")
        res = run(channel, apply=args.apply, league_id=args.league,
                  mode=args.mode, affected=affected)
    if args.payloads:
        print("\nGraphQL variables that would be sent:")
        payloads = ({"free": res["free"].get("payloads") or [],
                     "claims": res["claims"].get("payloads") or []}
                    if args.sequence else res["payloads"])
        print(json.dumps(payloads, indent=1))
    if args.apply:
        claim_result = res.get("claims") if args.sequence else res
        rec = (claim_result or {}).get("reconciliation") or {}
        if rec.get("error") or rec.get("blocked"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
