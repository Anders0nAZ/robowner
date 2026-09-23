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
import inspect
import json
import time
from datetime import datetime

from robo import LEAGUE_ID_2026, faab, lineup, season, settings, value, vegas
from robo import sleeper_read as api

def _quote_claim(gain: float, week: int, budget: int, pos: str | None,
                 field: dict | None, quality: dict | None = None) -> dict:
    target = getattr(faab.quote, "side_effect", None) or faab.quote
    try:
        sig = inspect.signature(target)
        accepts_q = any(p.name == "quality" or p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values())
    except Exception:
        accepts_q = True
    if accepts_q:
        return faab.quote(gain, week, budget, pos, field, quality=quality)
    return faab.quote(gain, week, budget, pos, field)

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

# HOW MUCH BETTER THAN THE BEST FREE AGENT A CLAIM MUST BE TO BE WORTH FAAB.
# In the same simulated lineup points as MIN_GAIN_TO_ADD, and measured against
# the best free option for the SAME drop on the SAME drawn worlds -- a paired
# difference, not two estimates subtracted.
#
# The comparison was already being computed and recorded as `over_free_gain`
# and reached no decision: on 18 Sep 2026 a run recorded that it preferred a
# 35-point waiver tight end to a 94-point free one and claimed the waiver man
# anyway. At 0 the bot refuses only a claim something free strictly beats;
# raise it and it insists a claim be meaningfully better before spending a
# dollar, which in a deep-wire week means no claims at all.
CLAIM_OVER_FREE_MIN = 0.0

# WHERE THE SEASON-TOTAL COMPARATOR STOPS BEING A FOOTNOTE. Both in
# rest-of-season points, both negative: the gap is add minus drop, so a claim
# that gives up far more than it gains is a large negative number.
#
# Sized against this league's own record rather than picked. Of 236 claims the
# bot has selected, 75 disagreed with the comparator; a veto at -25 refuses
# claims that surrender more than 25 season points (such as cutting an 80+ ROS
# starter for a 30-point backup). Raising the veto toward 0 starts refusing
# genuine lottery tickets, which look terrible on season totals by construction --
# that is what HIT_POINTS and the bench branch of clears() exist to permit.
DIRECT_ROS_FLAG = -20.0
DIRECT_ROS_VETO = -20.0

# Minimum player quality score Q to justify dropping an active rostered player
# on a waiver claim. Below this is T4_REPLACEMENT (Q < 0.25) -- bottom-tier
# replacement players who cannot displace an established roster asset.
CLAIM_DROP_MIN_QUALITY = 0.25

# Gain bin width within which moves are considered a dead heat. When two
# waiver wire options share the same FAAB bid and coverage priority, simulated
# gains inside this margin (< standard error) are arbitrated by the local LLM
# scout sentiment before falling back to raw simulation gain and player ID.
# Set to 0.0 to disable and let raw simulation gain decide.
TIEBREAKER_DEAD_HEAT_BIN = 0.5

# Qualitative LLM arbitration for near-tie / dead-heat move proposals.
# When a candidate move drops an active rostered player and the quantitative edge
# is negligible (< ARBITRATION_MAX_GAIN or < ARBITRATION_MAX_ROS_DIFF) in the same
# quality tier, the decision is referred to the local LLM scout. If the LLM rules
# KEEP_INCUMBENT, the move is refused to prevent lateral transaction churn.
ARBITRATION_ENABLED = True
ARBITRATION_MAX_GAIN = 1.5
ARBITRATION_MAX_ROS_DIFF = 5.0
ARBITRATION_MIN_CONFIDENCE = 0.60

# How many roster spots we are willing to turn over in one waiver run. This caps
# SLOTS, never claims -- capping claims would throw away the free optionality
# that makes a priority list worth submitting in the first place.
MAX_SLOTS_TO_TURN_OVER = 2

# Maximum number of skill-position claim slots (slates) to plan on weekly waivers.
# Weekly waivers can evaluate and submit claims across up to 2 distinct skill slots
# (e.g. RB+WR, RB+RB, WR+WR, QB+skill, TE+skill) under Option A positional rules.
WAIVER_MAX_SKILL_SLOTS = 2

# How deep each slot's priority list goes. Losing costs nothing, so this is
# bounded by how many candidates are plausibly worth the slot, not by risk.
SLATE_DEPTH = 5

# Hours before kickoff inside which `ros` stops running. 1.5 hours (90 min)
# strictly covers final inactives and Sleeper API game-lockout windows.
# Scoped to players involved in the transaction rather than national schedule.
ROS_MOVE_BLACKOUT_H = 1.5

# Hours before the next waiver settlement inside which `claims` may run. 48 hours
# covers Monday and Tuesday: the scheduled Tuesday waiver run fires at 20:00
# local (7h before Wednesday 03:00 settlement). Any run earlier in the week
# (Thursday-Sunday) sees only the locked Thursday players and is blacked out.
CLAIMS_SETTLEMENT_MAX_AHEAD_H = 48.0

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
        # Every proposal is priced against this roster, so the designations it
        # carries have to be the ones Sleeper would enforce on the write.
        "players": api.players(max_age_h=api.FRESH_STATUS_MAX_AGE_H),
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


def _claim_horizon(ctx: dict) -> dict:
    """When a claim built now would settle, and the first week it could play.

    The unlock comes from the pool we have ALREADY classified rather than the
    calendar, so an observed settlement beats the Wednesday fallback -- the same
    preference season.transaction_eligibility makes. The LATEST unlock is taken:
    too late under-values a claim, which is the safe direction, while too early
    restores the week nobody can receive.

    Only unlocks within CLAIMS_SETTLEMENT_MAX_AHEAD_H are considered actionable.
    If no actionable unlocks exist, settles_at falls back to the upcoming weekly run.
    """
    if "_claim_horizon" in ctx:
        return ctx["_claim_horizon"]
    now = time.time()
    actionable = [float(s["unlock_at"]) for s in (ctx.get("eligibility") or {}).values()
                  if s.get("acquisition") in {"weekly_waiver", "drop_waiver"}
                  and s.get("unlock_at")
                  and (float(s["unlock_at"]) - now) <= CLAIMS_SETTLEMENT_MAX_AHEAD_H * 3600.0]
    h = season.settlement_week(ctx.get("league_id") or LEAGUE_ID_2026,
                               week=ctx.get("week"),
                               settles_at=max(actionable) if actionable else None)
    at = datetime.fromtimestamp(h["settles_at"], season.PHOENIX)
    res = {**h, "settles_label": at.strftime("%a %H:%M"),
           "settles_iso": at.isoformat(),
           "has_actionable_unlocks": bool(actionable)}
    ctx["_claim_horizon"] = res
    return res


def hours_to_settlement(ctx: dict) -> float | None:
    """Hours until the next waiver run this context would submit into."""
    try:
        h = _claim_horizon(ctx)
        settles_at = float(h.get("settles_at") or 0)
        if settles_at > 0:
            return round((settles_at - time.time()) / 3600.0, 2)
    except Exception:
        pass
    return None


def player_hours_to_kickoff(pid: str | None, ctx: dict, now: float | None = None) -> float | None:
    """Hours until `pid`'s NFL game kicks off in `ctx['week']`.

    Returns None if:
    - The player has no NFL team (free agent / unrostered in NFL).
    - The player's team is on bye in this week.
    - Kickoff times cannot be read from the schedule.

    Returns negative float if the game has already kicked off.
    """
    if not pid:
        return None
    import time as _time
    now_ts = now if now is not None else _time.time()
    pinfo = (ctx.get("players") or {}).get(str(pid)) or {}
    team = pinfo.get("team")
    if not team:
        team = ((ctx.get("by_id") or {}).get(str(pid)) or {}).get("team")
    if not team:
        return None
    season_yr = getattr(season, "SEASON", 2026)
    week = ctx.get("week") or season.current_week()
    t_map = vegas.team_kickoffs(season_yr, week)
    if not t_map:
        return None
    code = vegas.team_code(team)
    kickoff_ts = t_map.get(code)
    if kickoff_ts is None:
        return None  # On bye this week
    return round((kickoff_ts - now_ts) / 3600.0, 2)


def blacked_out(ctx: dict, channel: str | None = None,
                players=None,
                add_id: str | None = None,
                drop_id: str | None = None) -> str:
    """Why a long-horizon move must not run right now, or "" if it may.

    `patch`, `news`, `ir_fill`, and `stream` are never blacked out.
    Claims are blacked out if the next waiver settlement is more than
    CLAIMS_SETTLEMENT_MAX_AHEAD_H hours away (Thursday through Sunday).

    For ordinary free-agent moves (`ros`), the blackout is scoped to the
    players involved in the transaction (add and drop). A player whose game
    kicks off within ROS_MOVE_BLACKOUT_H (1.5h = 90 min) or has already kicked off
    is blacked out from long-horizon swaps.
    """
    ch = channel or ctx.get("channel")
    if ch == "claims":
        hs = hours_to_settlement(ctx)
        if hs is not None and hs > CLAIMS_SETTLEMENT_MAX_AHEAD_H:
            return (f"{hs:.1f}h to waiver settlement, outside the "
                    f"{CLAIMS_SETTLEMENT_MAX_AHEAD_H:.0f}h window for claims")
        return ""
    if ctx.get("mode") in ("patch", "news", "ir_fill", "stream"):
        return ""

    h_next = ctx.get("hours_to_kickoff")

    involved = set()
    if players is not None:
        involved.update(str(p) for p in players if p is not None)
    if add_id is not None:
        involved.add(str(add_id))
    if drop_id is not None:
        involved.add(str(drop_id))

    if involved:
        for pid in sorted(involved):
            elig = (ctx.get("eligibility") or {}).get(pid) or {}
            if elig.get("roster_movement") == "roster_locked":
                pname = (ctx.get("players") or {}).get(pid, {}).get("full_name") or pid
                return f"{pname}'s game has started, so Sleeper has locked the player"

            h_p = player_hours_to_kickoff(pid, ctx)
            if h_p is None:
                if h_next is None:
                    pinfo = (ctx.get("players") or {}).get(str(pid)) or {}
                    if pinfo.get("team"):
                        return ("cannot read kickoff times, so the blackout cannot be cleared; "
                                "treating unknown as too close")
                continue
            if h_p <= 0:
                pname = (ctx.get("players") or {}).get(pid, {}).get("full_name") or pid
                return f"{pname}'s game has already kicked off ({abs(h_p):.1f}h ago)"
            if h_p < ROS_MOVE_BLACKOUT_H:
                pname = (ctx.get("players") or {}).get(pid, {}).get("full_name") or pid
                mins = int(round(ROS_MOVE_BLACKOUT_H * 60))
                return (f"{pname} kicks off in {h_p:.1f}h, inside the "
                        f"{ROS_MOVE_BLACKOUT_H:.1f}h ({mins}m) blackout for long-horizon moves")
        return ""

    # Blanket/fallback check when no specific players are specified
    if h_next is None:
        return ("cannot read kickoff times, so the blackout cannot be cleared; "
                "treating unknown as too close")
    if h_next < ROS_MOVE_BLACKOUT_H:
        mins = int(round(ROS_MOVE_BLACKOUT_H * 60))
        return (f"{h_next:.1f}h to the next kickoff, inside the "
                f"{ROS_MOVE_BLACKOUT_H:.1f}h ({mins}m) blackout for long-horizon moves")
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
        if ctx["mode"] == "ros" and blacked_out(ctx, channel="free", drop_id=pid):
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
    now = time.time()
    for row in ctx["available"]:
        state = (ctx.get("eligibility") or {}).get(row["player_id"]) or {}
        acquisition = state.get("acquisition")
        if waivers and acquisition not in {"weekly_waiver", "drop_waiver"}:
            continue
        if not waivers and acquisition not in {None, "free_now"}:
            continue
        if waivers:
            unlock_at = state.get("unlock_at")
            if unlock_at is not None:
                if (float(unlock_at) - now) / 3600.0 > CLAIMS_SETTLEMENT_MAX_AHEAD_H:
                    continue
        if ctx["mode"] == "news" and row["player_id"] not in ctx["affected"]:
            continue
        if ctx.get("mode") == "ros" and not waivers:
            if blacked_out(ctx, channel="free", add_id=row["player_id"]):
                continue
        if (row["player_id"] in onw) != waivers:
            continue
        if pos and (row.get("pos") or "") not in pos:
            continue
        if ctx.get("mode") == "patch":
            table = _expected_table(ctx)
            v = value.near_value(row["player_id"], ctx["week"], horizon=1, table=table)
            real = True
            if v <= 0:
                v, real = value.value_of(row, ctx["week"], table=table)
        else:
            v, real = value.value_of(row, ctx["week"], table=_expected_table(ctx))
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


def _coverage_after(ctx: dict, add_id: str | list | set | tuple,
                    drop_id: str | list | set | tuple | None,
                    table: dict | None = None,
                    record: dict | None = None) -> tuple[bool, str]:
    """Protect roster construction while allowing cross-position upgrades."""
    table = table or _expected_table(ctx)
    held = set(ctx["roster"].get("players") or [])
    reserve = set(ctx["roster"].get("reserve") or [])
    add_set = {str(x) for x in (add_id if isinstance(add_id, (list, set, tuple)) else [add_id]) if x}
    drop_set = {str(x) for x in (drop_id if isinstance(drop_id, (list, set, tuple)) else [drop_id]) if x} if drop_id else set()
    active = ((held - reserve) | add_set) - drop_set
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


def _ros_over(table: dict, pid: str | None, weeks) -> float:
    """His stored season total, re-summed over one horizon.

    A RE-SUMMATION OF STORED TERMS, not a second valuation. expected.py already
    writes every week's `final` and the weight applied to it, and `pts == final`
    holds on every live row, so restricting the sum reads the same arithmetic
    over fewer weeks. Recomputing it any other way would be the second
    implementation that drifts.
    """
    row = (table.get("players") or {}).get(str(pid)) if pid else None
    if not row:
        return 0.0
    by_week, weights = row.get("by_week") or {}, table.get("weights") or {}
    if weeks is None or not by_week:
        return float(row.get("ros") or 0.0)
    keep = {int(w) for w in weeks}
    return sum(float(cell.get("final") or 0.0) * float(weights.get(str(w), 1.0))
               for w, cell in by_week.items() if int(w) in keep)


def _direct_ros(table: dict, add_id: str, drop_id: str | None,
                weeks=None) -> dict:
    """The season-total comparator carried beside the simulated one, tiered.

    NOT A SECOND AUTHORITY, and for the reason marginal.py exists: subtracting
    two absolute season totals across positions is the question the simulator
    was built to replace, since a defence summing to 118 is not better than a
    fourth receiver summing to 45 if neither changes who starts. Inside the
    tiers the paired figure still decides.

    But it is not nothing either. Across every claim this bot has SELECTED, a
    third disagreed with it, the worst giving up 101.5 rest-of-season points for
    14.9. A gap that large is not a difference of opinion about lineup slots; it
    is one of the two numbers being wrong, and neither is trustworthy enough to
    spend on. So the far tail is a veto and the middle is a flag.

    COMPARED OVER THE SAME WEEKS as the simulator. Two engines measured over
    different horizons are not a cross-check, and the claims channel no longer
    prices the week its claim cannot reach.
    """
    add_ros = _ros_over(table, add_id, weeks)
    drop_ros = _ros_over(table, drop_id, weeks) if drop_id else 0.0
    gain = add_ros - drop_ros
    tier = ("refused" if gain <= DIRECT_ROS_VETO else
            "flagged" if gain <= DIRECT_ROS_FLAG else "agrees")
    return {"add_ros": round(add_ros, 3), "drop_ros": round(drop_ros, 3),
            "gain": round(gain, 3), "prefers_move": gain > 0,
            "verdict_tier": tier, "weeks": (list(weeks) if weeks else None),
            "veto_at": DIRECT_ROS_VETO, "flag_at": DIRECT_ROS_FLAG}


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
        # ATTACHED HERE, ENFORCED IN _rows. The tier is one of two screens that
        # can refuse a claim and they must not short-circuit each other, or the
        # record says which fired first instead of which were needed.
        direct = _direct_ros(table, add_id, drop_id, option.get("priced_weeks"))
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
        drop_ros = float(rows[pid]["ros"])
        if drop_ros > DROP_FLOOR:
            checks.append({**check, "eligible": False,
                           "reason": (f"drop price {drop_ros:.1f} exceeds the "
                                      f"{DROP_FLOOR:.1f} protection floor")})
            continue
        tail = _ticket_tail(pid, ctx)
        if tail is not None:
            checks.append({**check, "eligible": False,
                           "reason": (f"held as a lottery ticket: losing him "
                                      f"costs {tail:.1f} in the worst world, "
                                      f"over the {TICKET_PROTECT_POINTS:.1f} bar")})
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
        if waivers:
            state = (ctx.get("eligibility") or {}).get(pid) or {}
            unlock_at = state.get("unlock_at")
            if unlock_at is not None and (float(unlock_at) - time.time()) / 3600.0 > CLAIMS_SETTLEMENT_MAX_AHEAD_H:
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
        if drop["player_id"] is not None:
            try:
                from robo import quality
                q_info = quality.score(pid, week=ctx.get("week"), ctx=ctx, table=table)
            except Exception:
                q_info = None
            if q_info:
                q_val = float(q_info.get("q") or 0.0)
                q_tier = q_info.get("tier") or "T4_REPLACEMENT"
                if q_val < CLAIM_DROP_MIN_QUALITY or q_tier == "T4_REPLACEMENT":
                    reject("quality_tier",
                           f"replacement tier (cannot cut a rostered player for T4: Q={q_val:.2f})",
                           candidate_ros=cand_ros, drop_id=drop["player_id"],
                           drop_name=drop["row"].get("name"), drop_ros=drop["ros"])
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
    from robo import faab_field, marginal, quality
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
        q_info = quality.score(add_id, week=ctx.get("week"), ctx=ctx,
                               table=_expected_table(ctx))
    except Exception:
        q_info = None
    try:
        field = faab_field.predict(ctx, add_id, _expected_table(ctx), quality=q_info)
    except Exception as e:
        field = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    quote = _quote_claim(priced["gain"], ctx["week"], ctx["faab"],
                         p["add"].get("pos"), field if field.get("available") else None,
                         quality=q_info)
    claim = {**p, "bid": int(quote["bid"]), "priority": 0, "seq": 0,
             "bid_gain": round(priced["gain"], 3),
             "bid_se": round(priced["se"], 3), "bid_quote": quote,
             "opponent_field": field, "quality": q_info}
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
    if mode == "ros":
        t_block = blacked_out(ctx, channel="claims" if claim else "free",
                              add_id=add_id, drop_id=drop_id)
        if t_block:
            return t_block
    return ""


def evaluation_block(ctx: dict, channel: str | None = None) -> str:
    """Any hard reason this mode must not produce a transaction proposal."""
    if channel == "claims":
        timing = blacked_out(ctx, channel=channel)
        if timing:
            return timing
    elif ctx.get("mode") not in ("patch", "news", "ir_fill", "stream"):
        if ctx.get("hours_to_kickoff") is None:
            return ("cannot read kickoff times, so the blackout cannot be cleared; "
                    "treating unknown as too close")
        season_yr = getattr(season, "SEASON", 2026)
        week = ctx.get("week") or season.current_week()
        latest = vegas.latest_kickoff(season_yr, week)
        if latest is not None and latest < ROS_MOVE_BLACKOUT_H * 3600.0:
            mins = int(round(ROS_MOVE_BLACKOUT_H * 60))
            return (f"all remaining kickoffs inside the "
                    f"{ROS_MOVE_BLACKOUT_H:.1f}h ({mins}m) blackout for long-horizon moves")
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
    from robo import ir, sleeper_write as sw
    rid = ctx["roster"]["roster_id"]
    mode = ctx.get("mode", "ros")
    stop = ir.frozen(league_id) if plans else ""
    if stop:
        # Sleeper refuses every add while the account is frozen; ir.unblock()
        # owns that state and runs before any of this.
        print(f"  ** ADDS SKIPPED: {stop}")
        out["control_block"] = stop
        return out
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
                league_id,
                reason=f"moves {mode}: {p.get('why') or 'free-agent move'}"
                       f" (gain {p.get('gain', 0):+.1f})")
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
    need =_need_positions(ctx) if mode == "patch" else None
    if mode == "patch" and not need:
        return []
    drops = droppables(ctx)
    pool = candidates(ctx, waivers=False, pos=need)
    if mode == "patch":
        if need == {"DEF"}:
            # An empty DEF slot is a weekly matchup problem. The ordinary
            # board's season number picked DET over the better free defence
            # after our waiver winner was cut during an IR unblock. Use the
            # same fitted weekly ranking as the normal streaming decision.
            from robo import streaming
            weekly = {r["team"]: r["pts"] for r in streaming.rank_week(ctx["week"])}
            if weekly:
                for candidate in pool:
                    pid = candidate["row"]["player_id"]
                    if pid in weekly:
                        candidate["value"] = weekly[pid]
                pool.sort(key=lambda c: (-weekly.get(c["row"]["player_id"],
                                                float("-inf")),
                                         c["row"]["player_id"]))
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
    # An open spot is free and strictly dominant, so it is taken first and never
    # competes with a swap.
    for drop in opens:
        o = best_free([x for x in P["free"] if x["add"] not in used], drop,
                      fill=(mode == "fill"), ctx=ctx, board=P["board"])
        if not o:
            continue
        used.add(o["add"])
        out.append(_option_row(ctx, P["board"], o))
    if mode == "fill":
        return out

    # THE CHEAPEST MAN TO CUT IS NOT THE BEST SLOT TO SPEND, and `gain` already
    # knows it: price_options scores the WHOLE swapped roster against our own, so
    # what we give up by dropping him is inside the number. Ordering the pool on
    # drop price and then taking `swaps[:1]` therefore applied the drop cost
    # twice -- once inside every gain, and again as a filter that decided which
    # gains were allowed to be seen at all.
    #
    # Measured 18 Sep 2026, every pair already priced in the same run:
    #   Mendoza <- Dobbins  +18.20      Mendoza <- Boston  +17.73
    #   Barner  <- Strange   +0.70  <-- taken, because Strange was 1.17 cheaper
    # Mendoza could not pair with a tight end's slot without breaking the TE
    # coverage floor, so he was not beaten; he was never asked. The ledger then
    # recorded him at +20 as "cleared; lower-ranked option".
    #
    # The mutation budget is unchanged: still `slots` slots turned over. What
    # changes is that the best MOVE picks the slot, not the cheapest incumbent.
    # No extra simulation -- every (add, drop) pair in P["free"] is already priced.
    passed_over = ctx.setdefault("_slots_passed_over", [])
    for _ in range(max(1, slots)):
        best = None
        for drop in swaps:
            if drop in used:
                continue
            o = best_free([x for x in P["free"] if x["add"] not in used], drop,
                          ctx=ctx, board=P["board"])
            if o and (best is None or o["gain"] > best["gain"]):
                if best is not None:
                    passed_over.append({"drop": best["drop"], "add": best["add"],
                                        "gain": round(best["gain"], 3)})
                best = o
            elif o:
                passed_over.append({"drop": o["drop"], "add": o["add"],
                                    "gain": round(o["gain"], 3)})
        if not best:
            break
        used.add(best["add"])
        used.add(best["drop"])
        out.append(_option_row(ctx, P["board"], best))
    return out


def _identity(ctx: dict, board, pid) -> dict | None:
    """Who a player id refers to, from the board we already built.

    One definition, because a bare id in a record is a number the reader has to
    go and resolve: `over_free 11603` says nothing, "AJ Barner (SEA TE)" says
    the whole thing.
    """
    if pid is None:
        return None
    row = (ctx.get("by_id") or {}).get(pid)
    if row:
        return row
    p = (getattr(board, "S", None) or {}).get(pid) or {}
    return {"player_id": pid, "name": p.get("name") or pid, "pos": p.get("pos")}


def _option_row(ctx: dict, board, o: dict, why: str = "") -> dict:
    """One priced option in the shape render_free and run() already expect."""
    add_row = _identity(ctx, board, o["add"])
    if o["drop"] is None:
        drop_row = {"player_id": None, "name": "(open roster spot)", "pos": "--"}
        drop_val = 0.0
    else:
        drop_row = _identity(ctx, board, o["drop"])
        # THE SAME HORIZON THAT PRICED THE GAIN, or `gain` and `drop_value`
        # stop reconciling by hand -- which is the one check an audit page
        # exists to make.
        drop_val = round(board.drop_price(o["drop"], weeks=o.get("priced_weeks"))[0], 1)
    kind = "starting slot" if o["starter"] else "bench, judged on the ceiling"
    direct = o.get("direct_ros")
    comparison = ""
    if direct:
        # NOT "agrees"/"disagrees". That reads as two engines having fought and
        # this one having won, and narrate.py quotes this sentence verbatim.
        # Name what the two numbers are and which one decided.
        tail = {"refused": "refused", "flagged": "flagged"}.get(
            direct.get("verdict_tier"), "the lineup-marginal figure decides")
        comparison = (f"; season totals {direct['add_ros']:.1f} vs "
                      f"{direct['drop_ros']:.1f} ({direct['gain']:+.1f}); {tail}")
    from robo import quality, scout
    sentiment = float(o["scout_sentiment"]) if "scout_sentiment" in o else (
        scout.scout_sentiment(str(o["add"])) if o.get("add") is not None else 0.0)
    q_info = o.get("quality")
    if q_info is None and o.get("add"):
        try:
            q_info = quality.score(str(o["add"]), week=ctx.get("week"), ctx=ctx,
                                   table=_expected_table(ctx))
        except Exception:
            q_info = None
    horizon = o.get("priced_weeks")
    return {"add": add_row, "drop": drop_row,
            "gain": round(o["gain"], 1),
            "add_value": round(o["gain"], 1),
            "drop_value": drop_val,
            "real": True,
            "se": round(o["se"], 2), "ceiling": round(o["ceiling"], 1),
            "coverage": o.get("coverage"), "direct_ros": direct,
            "quality": q_info,
            "scout_sentiment": sentiment,
            "coverage_priority": o.get("coverage_priority", 0),
            "valuation_computed": o.get("valuation_computed"),
            "priced_weeks": horizon,
            "weeks_excluded": o.get("weeks_excluded"),
            "excluded_reason": o.get("excluded_reason"),
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




def _record_claim_pool(ctx: dict, b) -> None:
    """What the waiver pool actually WAS, and how high the wire floor sits.

    NOT A GATE, and deliberately. This league has no global waiver run over all
    free agents: `weekly_waiver` means a claim is genuinely required to acquire
    him, so a pool of "this week's completed games" is what a waiver wire IS
    here rather than an artifact to correct. Any rule of the form "refuse when
    the pool is unrepresentative" fires on exactly the set the free-agent
    comparison already refuses on a measured margin, so it would be the same
    control twice with no number behind the second one.

    What was missing was never a gate -- it was a reader's ability to see that
    the thirty-two men on offer were two teams who happened to play on Thursday.
    """
    players = ctx.get("players") or {}
    onw = ctx.get("on_waivers") or set()
    teams, by_pos = set(), {}
    for pid in onw:
        p = players.get(str(pid)) or {}
        if p.get("team"):
            teams.add(p["team"])
        pos = p.get("position") or "?"
        by_pos[pos] = by_pos.get(pos, 0) + 1
    free_n = sum(1 for pid, s in (ctx.get("eligibility") or {}).items()
                 if s.get("acquisition") == "free_now")
    horizon = ctx.get("_claim_horizon") or {}
    ctx["_claim_pool"] = {
        "waiver_pool_size": len(onw), "free_pool_size": free_n,
        "waiver_teams": sorted(teams), "waiver_by_pos": dict(sorted(by_pos.items())),
        "basis": ("unrostered players whose NFL game has locked and whose "
                  "waivers have not settled"),
        "settles_at": horizon.get("settles_at"),
        "settlement_basis": horizon.get("basis")}
    # HOW MUCH OF EACH POSITION THE WIRE ALREADY SUPPLIES, which is what decides
    # whether a bench spot spent there is worth holding. replaceability() has
    # measured it since it was written and nothing had ever read it.
    #
    # Measured against the floor the simulator PRICED against -- the men neither
    # rostered nor in this run's own shortlist -- so this percentage and the
    # gains beside it come from one pool. It does not on its own explain any
    # single drop price: a tight end can price near zero simply because he is
    # our second one.
    from robo import marginal
    try:
        ctx["_wire_floor"] = [
            {"pos": r["pos"], "ours": round(r["ours"], 2),
             "wire_first": round(r["wire_first"], 2),
             "wire_second": round(r["wire_second"], 2),
             "pct": round(r["pct"], 4), "excluded": r["excluded"],
             "supplier": r["supplier"], "supplier_weeks": r["supplier_weeks"]}
            for r in marginal.replaceability(b, ctx["league_id"])]
    except Exception as e:
        ctx["_wire_floor_error"] = f"{type(e).__name__}: {e}"


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
            # THE ERROR TRAVELS WITH THE PRICE. drop_price() has always returned
            # both and every caller took `[0]`, so the record could not show that
            # a slot was decided on a tie -- and it routinely is: on 18 Sep 2026
            # the three cheapest men on this roster priced at 0.069, -0.000 and
            # -0.000, and which of them landed first chose between a +20 move and
            # a +0.6 one.
            cost, cost_se = b.drop_price(pid)
            check = {**check, "drop_se": round(cost_se, 3)}
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
            priced_drops.append((cost, pid, cost_se))
        # AN EMPTY ROSTER SPOT IS A DROP THAT COSTS NOTHING, and until now the
        # ordinary channel could not see one: `fill` and `news` both put None in
        # here, `ros` never did, and `ros` is the only mode the Tuesday waiver
        # task runs. So a night with an open slot and a full FAAB budget built a
        # slate of swaps or nothing at all. Prepended AFTER the sort -- putting
        # None into priced_drops would compare None to a str on a cost tie.
        # STILL SORTED, because it bounds which drops get priced at all and the
        # cheapest are the likeliest to survive DROP_FLOOR. It no longer decides
        # WHICH slot is spent -- see plan_free.
        priced_drops.sort()
        drops = ([None] if ctx["slots"]["open"] > 0 else []) \
            + [pid for _, pid, _se in priced_drops]
    affected_weeks = None
    if ctx["mode"] == "news":
        affected_weeks = set()
        for pid in ctx.get("affected", set()):
            for w, cell in (b.S.get(pid, {}).get("weeks") or {}).items():
                if len(cell) > 2 and cell[2] < 0.999:
                    affected_weeks.add(int(w))
        if not affected_weeks:
            affected_weeks.add(ctx["week"])
    # A CLAIM IS PRICED ONLY OVER THE WEEKS A CLAIM CAN REACH. It does not
    # resolve until the waiver run, so a week that ends before that run is one
    # nobody can receive -- and the claims pool is made of exactly the men whose
    # current-week game is already over, because that is what puts them on
    # waivers in the first place.
    #
    # THE FREE CHANNEL NEEDS NO HORIZON, and the reason is enforced upstream
    # rather than asserted here: candidates(waivers=False) admits only
    # `free_now`, and a player's game locking is precisely what ends that. A
    # free agent's current week is by construction still ahead of him.
    horizon = _claim_horizon(ctx)
    ctx["_claim_horizon"] = horizon
    claim_weeks = [w for w in b.weeks if w >= horizon["week"]] or list(b.weeks)
    dropped = [w for w in b.weeks if w not in set(claim_weeks)]
    free_options = marginal.price_options(b, drops, free, affected_weeks)
    wire_options = marginal.price_options(b, drops, wire, affected_weeks,
                                          weeks=claim_weeks)
    for o in wire_options:
        o["weeks_excluded"] = dropped
        o["excluded_reason"] = (
            f"the claim settles {horizon['settles_label']}, after "
            f"{'week ' + ', '.join(str(w) for w in dropped)}" if dropped else None)
    _record_claim_pool(ctx, b)
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


def best_free(opts: list[dict], drop, fill: bool = False,
              ctx: dict | None = None, board=None) -> dict | None:
    """The best thing available for nothing, for this slot."""
    fits = []
    for o in opts:
        if o["drop"] != drop or not clears(o, fill=fill):
            continue
        if ctx is not None and board is not None:
            row = _option_row(ctx, board, o)
            screens = _claim_screens(row)
            if any(s.get("refused") for s in screens):
                continue
        fits.append(o)
    return max(fits, key=lambda o: o["ceiling"] if fill else o["gain"]) if fits else None


def _claim_screens(row: dict) -> list[dict]:
    """Every screen's verdict on one claim, none of them short-circuited.

    EVALUATE ALL, RECORD ALL, REFUSE IF ANY FIRED. Stopping at the first
    refusal records which screen happened to run first, not which were needed --
    so a reader cannot tell whether the free-agent comparison was doing work or
    whether the season-total veto was catching everything on its own. That is
    exactly the question anyone tuning these two bars has to answer, and the
    cost of answering it is one extra comparison on a row we are discarding.
    """
    out = []
    gap = row.get("over_free_gain")
    if gap is not None:
        beaten = gap <= CLAIM_OVER_FREE_MIN
        name = (row.get("over_free") or {}).get("name") or "a free agent"
        out.append({
            "name": "over_free", "refused": beaten,
            "verdict": ("refused: beaten by a free agent" if beaten
                        else "clears the free board"),
            "margin": gap, "se": row.get("over_free_se"),
            "threshold": CLAIM_OVER_FREE_MIN,
            "detail": (f"{name} is free and worth {abs(gap):.1f} more"
                       if beaten else f"{abs(gap):.1f} better than {name}, free")})
    direct = row.get("direct_ros") or {}
    if direct:
        tier = direct.get("verdict_tier")
        out.append({
            "name": "direct_ros", "refused": tier == "refused",
            "verdict": {"refused": "refused: season totals disagree",
                        "flagged": "cleared; season totals disagree",
                        }.get(tier, "season totals agree"),
            "margin": direct.get("gain"), "verdict_tier": tier,
            "threshold": direct.get("veto_at"),
            "detail": (f"season totals {direct.get('add_ros'):.1f} vs "
                       f"{direct.get('drop_ros'):.1f}")})
    # Proposal 2A: Minimum quality tier when dropping an active rostered player.
    # An open roster spot allows replacement tier, but dropping an asset for T4 is refused.
    # Furthermore, dropping an asset for a strictly lower quality tier is refused (no downgrades).
    drop_val = row.get("drop")
    if isinstance(drop_val, dict):
        drop_id = drop_val.get("player_id")
        drop_name = drop_val.get("name") or (str(drop_id) if drop_id else "incumbent")
        drop_q = drop_val.get("quality")
    else:
        drop_id = drop_val
        drop_name = str(drop_val) if drop_val else "incumbent"
        drop_q = None

    add_val = row.get("add")
    if isinstance(add_val, dict):
        add_id = add_val.get("player_id")
        add_name = add_val.get("name") or "candidate"
    else:
        add_id = add_val
        add_name = str(add_val) if add_val else "candidate"

    tier = "T4_REPLACEMENT"
    q_info = row.get("quality")
    if q_info:
        q_val = float(q_info.get("q") or 0.0)
        tier = q_info.get("tier") or "T4_REPLACEMENT"
        if drop_id is not None and str(drop_id).strip() not in ("", "(open roster spot)"):
            is_t4 = (q_val < CLAIM_DROP_MIN_QUALITY) or (tier == "T4_REPLACEMENT")
            if drop_q is None and drop_id:
                try:
                    from robo import quality
                    drop_q = quality.score(str(drop_id), week=row.get("priced_weeks", [None])[0] if isinstance(row.get("priced_weeks"), list) else None)
                except Exception:
                    drop_q = None
            tier_rank = {"T1_BREAKOUT": 1, "T2_CONTRIBUTOR": 2, "T3_SPECULATIVE": 3, "T4_REPLACEMENT": 4}
            add_tr = tier_rank.get(tier, 4)
            drop_tier = (drop_q or {}).get("tier") if drop_q else None
            drop_tr = tier_rank.get(drop_tier, 4) if drop_tier else None
            is_downgrade = (drop_tr is not None and add_tr > drop_tr)

            refused = is_t4 or is_downgrade
            if is_t4:
                verdict = "refused: replacement tier (cannot cut a rostered player for T4)"
                detail = f"{add_name} is tier {tier} (Q={q_val:.2f}) < {CLAIM_DROP_MIN_QUALITY:.2f}"
            elif is_downgrade:
                verdict = f"refused: quality tier downgrade (cannot drop {drop_tier} for {tier})"
                detail = f"{add_name} ({tier}, Q={q_val:.2f}) downgrades from {drop_name} ({drop_tier}, Q={drop_q.get('q', 0.0):.2f})"
            else:
                verdict = f"clears quality floor ({tier})" + (f" >= {drop_tier}" if drop_tier else "")
                detail = f"{add_name} ({tier}, Q={q_val:.2f})" + (f" vs {drop_name} ({drop_tier}, Q={drop_q.get('q', 0.0):.2f})" if drop_q else "")
            out.append({
                "name": "quality_tier",
                "refused": refused,
                "verdict": verdict,
                "margin": round(q_val - CLAIM_DROP_MIN_QUALITY, 3),
                "threshold": CLAIM_DROP_MIN_QUALITY,
                "detail": detail})
        else:
            out.append({
                "name": "quality_tier",
                "refused": False,
                "verdict": f"cleared: open slot allows {tier}",
                "margin": round(q_val - CLAIM_DROP_MIN_QUALITY, 3),
                "threshold": CLAIM_DROP_MIN_QUALITY,
                "detail": f"open roster spot: {tier} (Q={q_val:.2f}) permitted"})

    # Proposal 2B: LLM Arbitration on Near-Tie / Dead-Heat Moves.
    # When dropping an active rostered player and the quantitative margin is a dead
    # heat (< ARBITRATION_MAX_GAIN or < ARBITRATION_MAX_ROS_DIFF) in the same quality
    # tier, consult the local LLM scout. If the LLM rules KEEP_INCUMBENT, refuse
    # the move to stop lateral transaction churn.
    if (ARBITRATION_ENABLED
            and drop_id is not None
            and str(drop_id).strip() not in ("", "(open roster spot)")
            and not any(s.get("refused") for s in out)):
        tier_rank = {"T1_BREAKOUT": 1, "T2_CONTRIBUTOR": 2, "T3_SPECULATIVE": 3, "T4_REPLACEMENT": 4}
        add_tr = tier_rank.get(tier, 4) if q_info else 4
        drop_tier = (drop_q or {}).get("tier") if drop_q else None
        drop_tr = tier_rank.get(drop_tier, 4) if drop_tier else None

        sim_gain = float(row["gain"]) if ("gain" in row and row["gain"] is not None) else None
        direct_info = row.get("direct_ros") or {}
        direct_diff = float(direct_info["gain"]) if (direct_info.get("gain") is not None) else None

        has_metrics = (sim_gain is not None) or (direct_diff is not None)
        is_dead_heat = (has_metrics and drop_tr is not None and add_tr == drop_tr and (
            (sim_gain is not None and sim_gain <= ARBITRATION_MAX_GAIN)
            or (direct_diff is not None and direct_diff <= ARBITRATION_MAX_ROS_DIFF)
        ))

        if is_dead_heat and add_id and drop_id:
            try:
                from robo import scout
                arb_metrics = {
                    "add_name": add_name, "drop_name": drop_name,
                    "pos": (row.get("add") or {}).get("pos") if isinstance(row.get("add"), dict) else None,
                    "week": row.get("priced_weeks", [None])[0] if isinstance(row.get("priced_weeks"), list) else 2,
                    "gain": sim_gain if sim_gain is not None else 0.0,
                    "se": float(row.get("se") or 0.1),
                    "ros_diff": direct_diff if direct_diff is not None else 0.0,
                    "add_ros": float(direct_info.get("add_ros") or 0.0),
                    "drop_ros": float(direct_info.get("drop_ros") or 0.0),
                    "add_proj": float((row.get("add") or {}).get("proj") or 0.0) if isinstance(row.get("add"), dict) else 0.0,
                    "drop_proj": float((row.get("drop") or {}).get("proj") or 0.0) if isinstance(row.get("drop"), dict) else 0.0,
                    "add_q": q_info,
                    "drop_q": drop_q,
                }
                decision = scout.arbitrate_dead_heat(str(add_id), str(drop_id), arb_metrics)
                keep = (decision.get("verdict") == "KEEP_INCUMBENT" or
                        float(decision.get("confidence") or 0.0) < ARBITRATION_MIN_CONFIDENCE)
                if keep:
                    verdict = "refused: LLM arbitration kept incumbent"
                    detail = f"LLM kept {drop_name} (conf={decision.get('confidence', 0.5):.2f}): {decision.get('reason', '')}"
                else:
                    verdict = "cleared: LLM arbitration approved swap"
                    detail = f"LLM approved {add_name} (conf={decision.get('confidence', 0.5):.2f}): {decision.get('reason', '')}"
                out.append({
                    "name": "llm_arbitration",
                    "refused": keep,
                    "verdict": verdict,
                    "margin": round(sim_gain if sim_gain is not None else (direct_diff or 0.0), 2),
                    "threshold": ARBITRATION_MAX_GAIN,
                    "detail": detail
                })
            except Exception as e:
                out.append({
                    "name": "llm_arbitration",
                    "refused": True,
                    "verdict": "refused: LLM arbitration error (incumbent kept)",
                    "margin": round(sim_gain if sim_gain is not None else (direct_diff or 0.0), 2),
                    "threshold": ARBITRATION_MAX_GAIN,
                    "detail": f"Arbitration error: {e}"
                })
    return out


def _claim_sort_key(r: dict, kind_order: int | None = None, slate_idx: int = 0) -> tuple:
    """Sort key for waiver claim priority.

    Primary: descending bid.
    Secondary: group kind order (if multi-group portfolio), coverage priority, and slate index.
    Tertiary: dead-heat gain bin (if enabled), broken by local LLM scout sentiment,
              then exact simulated gain, and finally deterministic player ID.
    """
    bid = -int(r.get("bid") or 0)
    cov = int(r.get("coverage_priority") or 0)
    gain = float(r.get("gain") or 0.0)
    sent = float(r.get("scout_sentiment") or 0.0)
    pid = str((r.get("add") or {}).get("player_id") or "")

    if TIEBREAKER_DEAD_HEAT_BIN > 0:
        gain_bin = round(gain / TIEBREAKER_DEAD_HEAT_BIN)
        tiebreak = (-gain_bin, -sent, -gain, pid)
    else:
        tiebreak = (-gain, -sent, pid)

    if kind_order is not None:
        return (bid, kind_order, cov, slate_idx) + tiebreak
    return (bid, cov, slate_idx) + tiebreak


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
        # THE YARDSTICK MUST BE THE FREE MOVE WE WOULD ACTUALLY HAVE MADE. Once
        # this comparison gates rather than decorates, `fill` matters: against
        # an open slot the free channel ranks on the ceiling and skips the
        # upgrade bar, so asking it the other question would compare a claim
        # against a free agent nobody would have taken.
        fill = drop is None and open_slots > 0
        alt = best_free(P["free"], drop, fill=fill, ctx=ctx, board=board)
        out, refused = [], ctx.setdefault("_claim_refusals", [])
        for o in (x for x in P["wire"] if x["drop"] == drop):
            if not clears(o):
                continue
            row = _option_row(ctx, board, o)
            row["over_free"] = _identity(ctx, board, alt["add"]) if alt else None
            row["over_free_gain"] = (round(o["gain"] - alt["gain"], 3)
                                     if alt else None)
            # A conservative bound: the two share a baseline and are positively
            # correlated, so the true error of the difference is smaller. Recorded,
            # never gated on -- the point estimate answers the question asked.
            alt_se = alt.get("se") if alt else None
            row["over_free_se"] = (
                round((float(o.get("se") or 0.0) ** 2 + float(alt_se) ** 2) ** 0.5, 3)
                if alt_se is not None else None)
            screens = _claim_screens(row)
            row["screens"] = screens
            fired = [s for s in screens if s["refused"]]
            if fired:
                row["verdict"] = fired[0]["verdict"]
                refused.append(row)
                continue
            out.append(row)
        _price_claims(ctx, out)
        out.sort(key=_claim_sort_key)
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
    chosen_skill_slates: list[dict] = []
    for drop in P["drops"]:
        if drop is None or len(chosen_skill_slates) >= WAIVER_MAX_SKILL_SLOTS:
            continue
        pos = (((ctx.get("players") or {}).get(str(drop)) or {}).get("position")
               or ((ctx.get("by_id") or {}).get(str(drop)) or {}).get("pos"))
        # K stays on the free patch path and DEF owns its dedicated group.
        if pos in {"K", "DEF"}:
            continue
        raw_picks = [r for r in _rows(drop)
                     if r["add"]["player_id"] not in unblockable]
        if not raw_picks:
            continue

        if not chosen_skill_slates:
            # Slot 1: best cleared options against our cheapest droppable incumbent.
            picks = raw_picks[:SLATE_DEPTH]
            if not picks:
                continue
            slate = {"group_id": f"skill:{drop}", "kind": "skill", "capacity": 1,
                     "drop": picks[0]["drop"],
                     "drop_value": picks[0]["drop_value"], "claims": picks}
            chosen_skill_slates.append(slate)
            slates.append(slate)
        else:
            # Slot 2 (and beyond): enforce Option A positional limits, compound coverage, and cross-listing.
            # 1. Primary winner of prior skill slot(s) is excluded so Slot 2 does not compete for the same #1 target.
            prior_primaries = {s["claims"][0]["add"]["player_id"]
                               for s in chosen_skill_slates if s.get("claims")}
            # 2. Positional caps (Option A): max 1 QB slot, max 1 TE slot across the slate.
            # Up to 2 RBs, up to 2 WRs. Mixed combinations permitted.
            disallowed_positions = set()
            for s in chosen_skill_slates:
                s_positions = {
                    (r["add"].get("pos") or
                     (ctx.get("players") or {}).get(r["add"]["player_id"], {}).get("position"))
                    for r in s.get("claims", [])
                }
                if "QB" in s_positions:
                    disallowed_positions.add("QB")
                if "TE" in s_positions:
                    disallowed_positions.add("TE")

            # 3. Compound coverage & candidate filtering:
            s1_drop = chosen_skill_slates[0]["drop"]["player_id"]
            s1_primary_add = chosen_skill_slates[0]["claims"][0]["add"]["player_id"]

            eligible = []
            for r in raw_picks:
                c_pid = r["add"]["player_id"]
                c_pos = (r["add"].get("pos") or
                         (ctx.get("players") or {}).get(c_pid, {}).get("position"))
                if c_pid in prior_primaries:
                    continue
                if c_pos in disallowed_positions:
                    continue
                # Verify compound roster coverage: dropping both incumbents while adding both primary targets
                ok, _ = _coverage_after(ctx, [s1_primary_add, c_pid], [s1_drop, drop], table=_expected_table(ctx))
                if not ok:
                    continue
                eligible.append(r)

            picks = eligible[:SLATE_DEPTH]
            if not picks:
                continue
            slate = {"group_id": f"skill:{drop}", "kind": "skill", "capacity": 1,
                     "drop": picks[0]["drop"],
                     "drop_value": picks[0]["drop_value"], "claims": picks}
            chosen_skill_slates.append(slate)
            slates.append(slate)

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
    flat = []
    for s_idx, s in enumerate(slates):
        for c in s["claims"]:
            flat.append((s_idx, s, c))
    flat.sort(key=lambda sc: _claim_sort_key(
        sc[2],
        kind_order=group_order.get(sc[1].get("kind"), 9),
        slate_idx=sc[0]
    ))
    for i, (_, _, c) in enumerate(flat):
        c["seq"] = i
        c["priority"] = i
        c["submit_order"] = i
    from robo import waiver_manager
    ctx["_claim_exposure"] = waiver_manager.worst_case_spend(
        [c for s in slates for c in s.get("claims", [])]
    )
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
        unlock_at = state.get("unlock_at")
        if (pid in rostered
                or state.get("acquisition") not in {"weekly_waiver", "drop_waiver"}
                or (unlock_at is not None and (float(unlock_at) - time.time()) / 3600.0 > CLAIMS_SETTLEMENT_MAX_AHEAD_H)
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
    from robo import faab_field, quality
    cache = ctx.setdefault("_claim_prices", {})
    budget = max(0, int(ctx.get("faab") or 0))
    for p in rows:
        pid = p["add"]["player_id"]
        if pid not in cache:
            q_info = p.get("quality")
            if q_info is None:
                try:
                    q_info = quality.score(pid, week=ctx.get("week"), ctx=ctx,
                                           table=_expected_table(ctx))
                except Exception:
                    q_info = None
            field = p.get("opponent_field")
            if field is None:
                try:
                    field = faab_field.predict(ctx, pid, _expected_table(ctx), quality=q_info)
                except Exception as e:
                    field = {"available": False,
                             "reason": f"{type(e).__name__}: {e}"}
            q = _quote_claim(float(p.get("bid_gain", p["gain"])), ctx["week"],
                             budget, p["add"].get("pos"),
                             field if field.get("available") else None,
                             quality=q_info)
            bid = int(q["bid"])
            # Zero today and settable; a registry constant that quietly stops
            # being read is worse than no constant.
            if faab.MIN_LIVE_BID:
                bid = max(int(faab.MIN_LIVE_BID), bid)
            cache[pid] = (bid, q, field, q_info)
        bid, q, field, q_info = cache[pid]
        p.update({"bid": bid, "bid_gain": p.get("bid_gain", p["gain"]),
                  "bid_se": p.get("bid_se", p.get("se")),
                  "bid_quote": q, "opponent_field": field,
                  "quality": q_info})
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
    block = evaluation_block(ctx, channel="claims")
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


def stream_defence(week: int, apply: bool = False,
                   league_id: str = LEAGUE_ID_2026) -> dict:
    """Swap the defence when this week's lines say somebody free is better.

    One implementation for every caller -- the cascade's stream step and the
    news pulse's line-move repricing -- so the free-agent half of a defence
    decision can never run twice with two answers. The waiver half is
    _plan_defence_claims, inside the claims pass.

    DEFENCES ONLY, AND THAT IS A MEASURED DECISION. streaming.py fits 2,174
    defence weeks against the opponent's implied total and gets 11.39 points
    down to 4.72, monotone across all eight buckets. The same fit on 1,478
    kicker weeks runs flat and non-monotone, so a kicker is never streamed.

    SELF-GUARDING. It refuses under the Monday guard itself rather than
    trusting every caller to check, and it rechecks both teams' locks right
    before the write: kickoff is a hard boundary. Exempt from the ROS kickoff
    blackout: a stream is undone next Tuesday for nothing, and the lines it
    reads are firmest late.

    THE BOARD IS INTERSECTED WITH WHAT IS ACTUALLY FREE (streaming.swap ->
    best_available); a waiver defence can only be claimed.

    A construction session, like every roster writer: the new defence has to
    reach the lineup even when the caller -- a line move during live games --
    returns before its own construction check.
    """
    from robo import construction
    with construction.session("stream defence", apply=apply, league_id=league_id):
        return _stream_defence(week, apply, league_id)


def _stream_defence(week: int, apply: bool, league_id: str) -> dict:
    from robo import streaming
    if season.monday_guard_active():
        return {"status": "suppressed", "text": "suppressed: Monday guard"}
    ctx = _context(league_id, mode="stream")
    players = ctx["players"]
    held = [p for p in (ctx["roster"].get("players") or [])
            if (players.get(p) or {}).get("position") == "DEF"]
    if not held:
        return {"status": "none", "text": "we hold no defence; patch owns an empty slot"}
    ours = (players.get(held[0]) or {}).get("team") or held[0]
    d = streaming.swap(week, ours, league_id)
    if not d.get("best"):
        return {"status": "hold", "text": d["why"], "swap": d}
    if d["gain"] < streaming.MIN_STREAM_GAIN:
        return {"status": "hold", "swap": d,
                "text": (f"hold {ours}: {d['why']}, {d['gain']:+.2f} under the "
                         f"{streaming.MIN_STREAM_GAIN:g} bar")}
    best = d["best"]["team"]
    plan = [{"add": {"player_id": best, "name": api.player_name(players, best),
                     "pos": "DEF"},
             "drop": {"player_id": held[0],
                      "name": api.player_name(players, held[0]), "pos": "DEF"},
             "gain": d["gain"], "add_value": round(d["best"]["pts"], 2),
             "drop_value": round(d["mine"]["pts"], 2), "real": True,
             "why": d["why"]}]
    if not value.may_submit():
        return {"status": "gated", "swap": d,
                "text": f"WOULD stream {ours} -> {best} ({d['gain']:+.2f}) -- gate shut"}
    if not apply:
        return {"status": "would_stream", "swap": d,
                "text": f"would stream {ours} -> {best} ({d['gain']:+.2f})"}
    season.invalidate_live()
    live = season.week_points(week, season.SEASON, league_id)
    if (live.get(held[0]) or {}).get("locked"):
        return {"status": "hold", "swap": d,
                "text": f"hold {ours}: its game locked before submission"}
    if (live.get(best) or {}).get("locked"):
        return {"status": "hold", "swap": d,
                "text": f"hold {ours}: {best}'s game locked before submission"}
    out = {"submitted": [], "applied": False}
    submit_free(ctx, plan, out, league_id)
    if not out.get("submitted"):
        return {"status": "failed", "swap": d,
                "text": f"FAILED to stream {ours} -> {best}: transaction was not accepted"}
    return {"status": "streamed", "swap": d,
            "text": f"streamed {ours} -> {best} ({d['gain']:+.2f})"}


def run_ros_sequence(apply: bool = False, league_id: str = LEAGUE_ID_2026,
                     verbose: bool = True, source: str | None = None) -> dict:
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
                 verbose=verbose, _ctx=claims_ctx, source=source)
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

    def _key(row: dict) -> tuple:
        return (str((row.get("add") or {}).get("player_id")),
                (row.get("drop") or {}).get("player_id"))

    refusals = {_key(r): r for r in (ctx.get("_claim_refusals") or [])}
    # A SELECTED CLAIM WENT THROUGH THE SAME SCREENS and must show the same
    # evidence. Reading these only off the refusals left the one row a reader
    # most wants to check -- the move actually taken -- with the comparison
    # blank, as though nothing had been asked of it.
    planned = {_key(c): c for s in plans for c in (s.get("claims") or [])}

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
        # A row a screen REFUSED carries that screen's own words, and every
        # other screen's verdict beside it. Without this it would fall through
        # to "cleared; lower-ranked option", which is the one thing it is not.
        refusal = refusals.get((add_id, drop_id)) or {}
        evidence = refusal or planned.get((add_id, drop_id)) or {}
        if refusal:
            verdict = refusal.get("verdict") or "refused"
        elif picked:
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
                     "direct_ros": option.get("direct_ros"),
                     "priced_weeks": option.get("priced_weeks"),
                     "weeks_excluded": option.get("weeks_excluded"),
                     "excluded_reason": option.get("excluded_reason"),
                     "over_free": evidence.get("over_free"),
                     "over_free_gain": evidence.get("over_free_gain"),
                     "over_free_se": evidence.get("over_free_se"),
                     "screens": evidence.get("screens")})
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
                       "claim_over_free_min": CLAIM_OVER_FREE_MIN,
                       "direct_ros_flag": DIRECT_ROS_FLAG,
                       "direct_ros_veto": DIRECT_ROS_VETO,
                       "claim_drop_min_quality": CLAIM_DROP_MIN_QUALITY,
                       "tiebreaker_dead_heat_bin": TIEBREAKER_DEAD_HEAT_BIN,
                       "coverage_floor": dict(MIN_ROSTER_COVERAGE)},
        "claim_horizon": ctx.get("_claim_horizon"),
        "claim_pool": ctx.get("_claim_pool"),
        "wire_floor": ctx.get("_wire_floor"),
        "drop_checks": ctx.get("_ros_drop_checks") or [],
        # THE OTHER SLOTS, AND WHAT THEY WERE WORTH. The ledger used to record a
        # +18 option as "cleared; lower-ranked option" when it had never been
        # compared to anything -- the planner had already committed to a
        # different slot. These are the pairs that genuinely lost on value.
        "slots_passed_over": [
            {**r, "add": identity(r.get("add")), "drop": identity(r.get("drop"))}
            for r in (ctx.get("_slots_passed_over") or [])],
        "control_checks": controls,
        "options": rows,
    }


def run(channel: str, apply: bool = False, league_id: str = LEAGUE_ID_2026,
        mode: str = "ros", verbose: bool = True,
        affected: set[str] | None = None, _ctx: dict | None = None,
        ir_open_slots: int = 0, ir_moves: list[dict] | None = None,
        excluded: set[str] | None = None, source: str | None = None) -> dict:
    # Any add or drop reshapes the roster, so the outermost caller ends with a
    # construction check -- the new man must reach the lineup, and a cut must
    # not leave a slot nobody can fill.
    from robo import construction
    with construction.session(f"moves {channel} {mode}", apply=apply,
                              league_id=league_id):
        return _run(channel, apply, league_id, mode, verbose, affected, _ctx,
                    ir_open_slots, ir_moves, excluded, source)


def _run(channel, apply, league_id, mode, verbose, affected, _ctx,
         ir_open_slots, ir_moves, excluded, source) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    ctx = _ctx or _context(league_id, mode, affected=affected,
                           ir_open_slots=ir_open_slots, ir_moves=ir_moves,
                           excluded=excluded)
    ctx["channel"] = channel
    block = evaluation_block(ctx, channel=channel)
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
    timing_block = blacked_out(ctx, channel=channel)
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

    from robo import ir
    stop = ir.frozen(league_id)
    if stop:
        print(f"  ** CLAIMS SKIPPED: {stop}")
        out["control_block"] = stop
        return out
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
    from robo import construction
    with construction.session("moves ir_fill", apply=apply, league_id=league_id):
        return _run_ir_fills(opened, ir_moves, apply, league_id, verbose)


def _run_ir_fills(opened, ir_moves, apply, league_id, verbose) -> dict:
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
    if args.apply:
        from robo import ir
        ir.unblock(apply=True, league_id=args.league)
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
