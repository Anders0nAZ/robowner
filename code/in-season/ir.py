"""Injured-reserve moves: the one roster decision that needs no valuation.

Whether a player may sit on reserve is written in the league settings, not in a
model. Ours accepts Out, Suspended, COV (plus anyone already tagged IR or PUP)
and refuses Doubtful, NA and DNR. That makes this the only part of in-season
roster management that can run live today, while what a player is WORTH is
still an open question (see robo/value.py).

IT NEVER FILLS THE SLOT IT FREES. Reserving an injured player and then signing
somebody with the space are two different decisions: the first is bookkeeping,
the second needs a valuation the bot does not have yet. So this opens the slot,
records why, and stops. An open slot showing on the status page is the point,
not an oversight.

Going the other way is asymmetric on purpose. Reserving is always safe -- the
player could not have played anyway. Activating is not: if the roster is full,
somebody has to be cut to make room, and choosing that person is a valuation.
plan() therefore activates only into space, and a man it cannot activate is
unblock()'s problem.

AN ILLEGAL RESERVE FREEZES THE WHOLE ACCOUNT, and unblock() runs before every
other write for that reason. A man left on IR after his designation clears
(an `Out` expires at the Wednesday rollover) locks the roster on Sleeper's side:
no adds, no claims, no lineup edits. Sleeper's documented way out goes through
a second, softer state -- a team OVER the roster limit may still drop players
and move players to IR, it just cannot add or edit its lineup -- so the path is
the same for one stuck man or three: activate him (a free swap with an
IR-eligible man if one is active), then cut back down to the limit one priced
drop at a time. Every step is read back from Sleeper before the next is chosen.

python -m robo.ir              # what it would do
python -m robo.ir --unblock    # just the unblock plan
python -m robo.ir --apply      # do it: unblock, then sweep
"""

import argparse

from robo import LEAGUE_ID_2026, season, settings, value
from robo import sleeper_read as api

# How long a designation typically keeps a man out, used only to break ties when
# more players are eligible than there are slots. A reserve slot is committed for
# the whole absence, so when two men are worth the same it is better spent on the
# longer one -- the short absence would only have to be activated back out.
_DURATION = {"IR": 3, "PUP": 3, "Sus": 2, "NA": 2, "DNR": 2,
             "Out": 1, "COV": 1, "Doubtful": 0}

# Master switch. Off means the module still reports what it would do and
# changes nothing, which is the state to leave it in if the league ever
# disputes an IR move.
IR_ENABLED = True

settings.apply(__name__, globals())


def _keep_rank(pid: str, players: dict) -> tuple:
    """Who most deserves a reserve slot when more men are eligible than fit.

    WHEN MORE MEN ARE ELIGIBLE THAN THERE ARE SLOTS, THIS IS A DECISION.
    Everyone eligible is unplayable, so any of them frees an active slot
    equally -- which means the question is not who to park but who to KEEP.
    Reserve the most valuable, and the ones left in active slots are then the
    natural drop candidates, which is exactly the right answer. Ordered by
    rest-of-season hold value, ties broken toward the longer absence. Before
    this it was roster order, which is arbitrary and would happily strand a
    starter in an active slot behind a fourth-string body.
    """
    # Through hold_of, not ros_value, so this asks the same question the drop
    # decision asks and gets the same answer. Reaching past the seam is how
    # the IR tiebreak would have kept ranking men by ros.hold long after
    # moves.py stopped -- and ros.hold had Carson Beck as the cheapest man on
    # the roster while the simulator has him above six of our starters.
    try:
        v, _ = value.hold_of({"player_id": pid}, season.current_week())
    except Exception:
        v = 0.0
    st = (players.get(pid) or {}).get("injury_status") or ""
    return (-v, -_DURATION.get(st, 0), pid)


def _status(players: dict, pid: str) -> str:
    return (players.get(pid) or {}).get("injury_status") or ""


def with_statuses(players: dict, statuses: dict | None) -> dict:
    """The player dump with fresher designations laid over it.

    The dump is cached for up to FRESH_STATUS_MAX_AGE_H, and Sleeper's weekly
    feed -- which the news pulse reads every twenty minutes -- can carry a newer
    one. On 23 Sep 2026 the pulse saw Nico go Out -> Questionable at 00:23 while
    unblock, reading the dump, called the roster legal; it stayed frozen for
    four hours. Only ids the caller supplies are replaced.
    """
    if not statuses:
        return players
    out = dict(players)
    for pid, st in statuses.items():
        out[str(pid)] = {**(players.get(str(pid)) or {}), "injury_status": st}
    return out


def legality(roster: dict, players: dict, ok: set[str]) -> dict:
    """Is this roster one Sleeper will accept writes against? Pure.

    Two states, and Sleeper treats them differently. A reserve man without an
    IR-eligible designation LOCKS the account. Being over the active limit is
    softer -- drops and IR moves still go through -- and it is the state
    unblock() deliberately passes through on the way out of the first one.
    Both block a lineup edit, so both make the roster illegal here.
    """
    reserve = [str(p) for p in (roster.get("reserve") or [])]
    active = [str(p) for p in (roster.get("players") or []) if str(p) not in reserve]
    stuck = [p for p in reserve if _status(players, p) not in ok]
    over_by = max(0, len(active) - season.ROSTER_MAX)
    return {"stuck": stuck, "over_by": over_by, "active": len(active),
            "legal": not stuck and not over_by}


def _describe(state: dict, players: dict) -> str:
    bits = []
    if state["stuck"]:
        bits.append(", ".join(
            f"{api.player_name(players, p)} ({_status(players, p) or 'healthy'})"
            for p in state["stuck"])
            + " on IR without an IR-eligible designation")
    if state["over_by"]:
        bits.append(f"active roster is {state['over_by']} over the "
                    f"{season.ROSTER_MAX}-man limit")
    return "; ".join(bits)


def frozen(league_id: str = LEAGUE_ID_2026, statuses: dict | None = None) -> str:
    """Why Sleeper will refuse a write right now, or "" if it will not.

    Read at the write boundary by lineup, moves and the sweep, so an entry point
    that forgot to call unblock() refuses cleanly instead of sending a write
    Sleeper is certain to reject.
    """
    season.invalidate_live()
    players = with_statuses(api.players(max_age_h=api.FRESH_STATUS_MAX_AGE_H),
                            statuses)
    state = legality(season.mine(league_id), players, season.ir_statuses(league_id))
    return "" if state["legal"] else "roster frozen: " + _describe(state, players)


def _slot_of(starters: list, pid: str) -> str | None:
    from robo.lineup import SLOTS
    try:
        i = starters.index(pid)
    except ValueError:
        return None
    return SLOTS[i] if i < len(SLOTS) else None


def _next_step(roster: dict, players: dict, ok: set[str], state: dict,
               league_id: str, tried: set, refused: set, undroppable: set) -> dict | None:
    """The first move that makes progress, in order of what it costs us.

    A swap costs nothing, an activation costs nothing by itself, and a drop
    costs a player -- so the drop is only reached once nobody can be parked in
    the stuck man's place.
    """
    from robo.lineup import SLOT_ELIGIBLE
    reserve = [str(p) for p in (roster.get("reserve") or [])]
    starters = [str(p) for p in (roster.get("starters") or [])]
    active = [str(p) for p in (roster.get("players") or []) if str(p) not in reserve]

    if state["stuck"]:
        cand = [p for p in active if _status(players, p) in ok]
        states = season.transaction_states(state["stuck"] + cand, league_id)
        movable = lambda p: ((states.get(p) or {}).get("roster_movement")
                             != "roster_locked")
        stuck = [p for p in state["stuck"] if movable(p)]
        if not stuck:
            return None
        eligible = sorted((p for p in cand if movable(p)),
                          key=lambda p: _keep_rank(p, players))

        def swap(s, e, starter):
            return {"action": "swap", "activate": s, "park": e, "starter": starter,
                    "reserve": [p for p in reserve if p != s] + [e]}

        # 1. A free swap with a bench man.
        for s in stuck:
            for e in eligible:
                if e not in starters and (s, e) not in tried:
                    return swap(s, e, False)
        # 2. A free swap with a starter, only where the returning man can
        #    legally take over the slot he leaves. The lineup cannot be edited
        #    while the roster is frozen, so he cannot be benched first.
        for s in stuck:
            pos = (players.get(s) or {}).get("position")
            for e in eligible:
                slot = _slot_of(starters, e)
                if (slot and pos in SLOT_ELIGIBLE.get(slot, set())
                        and (s, e) not in tried):
                    return swap(s, e, True)
        # 3. Activate into a full roster. Over the limit is a state Sleeper
        #    allows, and from it drops go through.
        if not refused:
            return {"action": "activate", "activate": stuck,
                    "reserve": [p for p in reserve if p not in stuck]}
        # Sleeper refused the over-limit activation. Its other documented way
        # out is to drop somebody FIRST and activate only into real space.
        room = season.ROSTER_MAX - state["active"]
        if room > 0:
            todo = stuck[:room]
            return {"action": "activate", "activate": todo, "into_space": True,
                    "reserve": [p for p in reserve if p not in todo]}
        return _drop_step(roster, league_id, exclude=set(state["stuck"]) | undroppable)

    if state["over_by"]:
        return _drop_step(roster, league_id, exclude=undroppable)
    return None


def _drop_step(roster: dict, league_id: str, exclude: set) -> dict | None:
    """The cheapest man to lose, priced on the roster as it now stands.

    Re-priced every time rather than taking the three cheapest at once: after
    the first cut the second-cheapest man alone is not necessarily the right
    second cut. Patch mode, because this is legality and not an upgrade -- the
    drop floor and the ticket protection both yield, while starters and locked
    men stay out of the pool.
    """
    from robo import marginal, moves
    marginal.board.cache_clear()
    ctx = moves._context(league_id, "patch")
    reserve = set(str(p) for p in (roster.get("reserve") or []))
    ctx["roster"] = {**ctx["roster"], "players": list(roster.get("players") or []),
                     "reserve": sorted(reserve),
                     "starters": list(roster.get("starters") or [])}
    ctx["reserve"] = reserve
    ctx["starters"] = set(str(p) for p in (roster.get("starters") or []))
    ctx.pop("_droppables", None)
    pool = [d for d in moves.droppables(ctx)
            if str(d["row"]["player_id"]) not in exclude]
    # THE CUT MUST NOT OPEN A STARTING SLOT. The simulator prices only
    # QB/RB/WR/TE, so a bench defence reads as a free cut even when it is the
    # only man who can fill this week's DEF slot -- on 23 Sep 2026 Nico's
    # activation released the defence we had won on waivers four hours
    # earlier. Judged by the lineup optimizer on the roster after the cut, so it
    # covers any position, not a K/DEF list; a slot that was already
    # unfillable does not veto every cut.
    active = [str(p) for p in (roster.get("players") or [])
              if str(p) not in reserve]
    base = None
    for d in pool:
        pid = str(d["row"]["player_id"])
        if base is None:
            base = _unfillable(active, ctx)
        if _unfillable([p for p in active if p != pid], ctx) > base:
            continue
        return {"action": "drop", "drop": pid,
                "value": round(float(d["value"]), 1)}
    return None


def _unfillable(ids: list[str], ctx: dict) -> int:
    """Starting slots this week that no startable man on `ids` can fill."""
    from robo import lineup
    cands, _ = lineup.project_roster(ids, season.SEASON, ctx["week"],
                                     ctx["players"], ctx["league_id"])
    filled, _ = lineup.optimize(cands)
    return sum(1 for p in filled if not p or not lineup.startable(p))


def _simulate(roster: dict, step: dict) -> dict:
    r = {**roster, "players": list(roster.get("players") or []),
         "reserve": list(roster.get("reserve") or [])}
    if step["action"] in ("swap", "activate"):
        r["reserve"] = list(step["reserve"])
    elif step["action"] == "drop":
        r["players"] = [p for p in r["players"] if str(p) != step["drop"]]
    return r


def _write(step: dict, roster_id: int, league_id: str) -> str:
    """Send one step. Returns an error string, "" on a clean send."""
    from robo import sleeper_write as sw
    reason = "unblock: " + (step.get("text") or step["action"])
    try:
        if step["action"] in ("swap", "activate"):
            sw.set_reserve(roster_id, step["reserve"], league_id, reason=reason)
        elif step["action"] == "drop":
            sw.free_agent_transaction(None, {step["drop"]: roster_id}, league_id,
                                      reason=reason)
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return ""


def _landed(step: dict, roster: dict) -> bool:
    reserve = set(str(p) for p in (roster.get("reserve") or []))
    held = set(str(p) for p in (roster.get("players") or []))
    if step["action"] == "swap":
        return step["park"] in reserve and step["activate"] not in reserve
    if step["action"] == "activate":
        return not (set(step["activate"]) & reserve)
    return step["drop"] not in held


def _narrate(step: dict, players: dict) -> str:
    n = lambda p: api.player_name(players, p)
    if step["action"] == "swap":
        where = "starting" if step["starter"] else "bench"
        return (f"activated {n(step['activate'])} and parked {n(step['park'])} "
                f"({_status(players, step['park'])}, {where}) in his reserve slot")
    if step["action"] == "activate":
        return "activated " + ", ".join(n(p) for p in step["activate"])
    return f"released {n(step['drop'])} (hold {step['value']:.1f}, the cheapest body we could cut)"


def unblock(apply: bool = False, league_id: str = LEAGUE_ID_2026,
            verbose: bool = True, statuses: dict | None = None) -> dict:
    """Make the roster one Sleeper will accept writes against. Runs FIRST.

    Loops until legal: a free swap per stuck man where an IR-eligible man can
    take his slot, otherwise activate every stuck man into a full roster and
    then drop, one re-priced man at a time, back down to the limit. Two or three
    stuck men therefore cost up to two or three drops. Every write is read back
    before the next step is chosen, and a step that did not land is never tried
    again -- a refused activation falls back to drop-first, a refused starter
    swap to activation. A dry run walks the same steps on a simulated copy.
    `statuses` overlays designations fresher than the cached dump.
    """
    from robo import construction
    with construction.session("ir unblock", apply=apply):
        return _unblock(apply, league_id, verbose, statuses)


def _unblock(apply: bool, league_id: str, verbose: bool,
             statuses: dict | None) -> dict:
    apply = apply and IR_ENABLED
    ok = season.ir_statuses(league_id)
    tried, refused, undroppable = set(), set(), set()
    out = {"legal": True, "was_legal": True, "steps": [], "applied": False,
           "reason": "", "failures": []}
    roster, players, cap, passes = None, None, None, 0
    while True:
        if apply or roster is None:
            season.invalidate_live()
            players = with_statuses(
                api.players(max_age_h=api.FRESH_STATUS_MAX_AGE_H), statuses)
            roster = season.mine(league_id)
        state = legality(roster, players, ok)
        if cap is None:
            out["was_legal"] = state["legal"]
            out["before"] = _describe(state, players)
            # Worst case is drop-first for every stuck man: a drop and an
            # activation each, plus whatever overage was already there.
            cap = 2 * len(state["stuck"]) + state["over_by"] + 3
        if state["legal"]:
            break
        if passes >= cap:
            out["reason"] = (f"still illegal after {passes} step(s): "
                             + _describe(state, players))
            break
        passes += 1
        step = _next_step(roster, players, ok, state, league_id,
                          tried, refused, undroppable)
        if step is None:
            out["reason"] = ("no legal move resolves it (the men involved are "
                             "locked, or nobody is left to cut): "
                             + _describe(state, players))
            break
        step["text"] = _narrate(step, players)
        out["steps"].append(step)
        if not apply:
            roster = _simulate(roster, step)
            continue
        err = _write(step, roster["roster_id"], league_id)
        season.invalidate_live()
        after = season.mine(league_id)
        step["landed"] = _landed(step, after)
        if step["landed"]:
            out["applied"] = True
            continue
        out["failures"].append({"step": step["text"],
                                "error": err or "not reflected on the roster"})
        if step["action"] == "swap":
            tried.add((step["activate"], step["park"]))
        elif step["action"] == "activate":
            if step.get("into_space"):
                # Refused even with room for him: nothing left to try.
                out["reason"] = "Sleeper refused activating into open space"
                state = legality(after, players, ok)
                break
            refused |= set(step["activate"])
        else:
            undroppable.add(step["drop"])
    out["legal"] = state["legal"]
    out["after"] = _describe(state, players)

    if verbose:
        print(render_unblock(out, apply))
    if apply and out["applied"]:
        _record_unblock(out, league_id)
    if apply and not out["legal"]:
        try:
            from robo import alerts
            alerts.blast("Roboner's roster is frozen on Sleeper and could not be "
                         "fixed automatically: " + out["reason"],
                         key="ir-unblock", channels=alerts.INSEASON_CHANNELS)
        except Exception:
            pass
    return out


def render_unblock(out: dict, apply: bool = False) -> str:
    if out["was_legal"]:
        return "IR unblock: roster is legal, nothing to do"
    L = [f"IR unblock: {out['before']}"]
    for s in out["steps"]:
        tag = ("done" if s.get("landed") else "FAILED") if apply else "plan"
        L.append(f"  -> {tag:<6} {s['text']}")
    for f in out["failures"]:
        L.append(f"     failed {f['step']}: {f['error']}")
    L.append("  legal now" if out["legal"] else f"  STILL FROZEN: {out['reason']}")
    return "\n".join(L)


def _record_unblock(out: dict, league_id: str) -> None:
    from robo.decisions import record
    done = [s for s in out["steps"] if s.get("landed")]
    drops = [s for s in done if s["action"] == "drop"]
    text = "; ".join(s["text"] for s in done)
    why = (f"{out['before']}. Sleeper refuses every roster and lineup change "
           f"while a man sits on reserve without an IR-eligible designation, so "
           f"this is resolved before anything else runs")
    if drops:
        why += (f". Activating put the roster over the {season.ROSTER_MAX}-man "
                f"limit, and each cut was the cheapest man to lose, re-priced "
                f"after the one before it")
    record("ir", "Roster unblocked", text[:1].upper() + text[1:] + ".", why + ".",
           data={"steps": [{k: v for k, v in s.items() if k != "text"}
                           for s in done],
                 "legal": out["legal"]})
    try:
        from robo import moves
        out["waiver_maintenance"] = moves.maintain_pending_claims(
            apply=True, league_id=league_id, reason="IR unblock")
    except Exception as e:
        out["waiver_maintenance"] = {"status": "failed",
                                     "error": f"{type(e).__name__}: {e}"}
    try:
        from robo import marginal
        marginal.board.cache_clear()
    except Exception:
        pass
    season.invalidate_live()


def plan(league_id: str = LEAGUE_ID_2026) -> dict:
    """What should move, in or out of reserve, and why. Reads only."""
    # SLEEPER'S OWN DESIGNATION DECIDES THIS, and it must be current. Sleeper
    # enforces the reserve rule against this field, so a day-old copy of it does
    # not merely delay a move -- it reports a clean sweep on a roster with an
    # eligible man sitting in an active slot, which is indistinguishable from
    # having nobody to park.
    players = api.players(max_age_h=api.FRESH_STATUS_MAX_AGE_H)
    r = season.mine(league_id)
    roster = list(r.get("players") or [])
    reserve = list(r.get("reserve") or [])
    starters = set(r.get("starters") or [])
    sl = season.slots(league_id)
    ok = season.ir_statuses(league_id)
    eligibility = season.transaction_states(roster, league_id)

    to_reserve, to_activate, blocked = [], [], []

    eligible = sorted(
        (p for p in roster
         if p not in reserve
         and ((players.get(p) or {}).get("injury_status") or "") in ok),
        key=lambda pid: _keep_rank(pid, players))

    room = sl["ir_open"]
    for pid in eligible:
        st = (players.get(pid) or {}).get("injury_status") or ""
        name = api.player_name(players, pid)
        if (eligibility.get(pid) or {}).get("roster_movement") == "roster_locked":
            blocked.append({"player_id": pid, "name": name, "status": st,
                            "why": "his NFL game has started, so Sleeper has locked "
                                   "all roster movement for him until the week advances"})
            continue
        if pid in starters:
            # Reserving somebody Sleeper still has in a starting slot would
            # leave the lineup pointing at a player who is not on the active
            # roster. The daily task runs lineup first for exactly this reason;
            # by the next pass he is on the bench and eligible to move.
            blocked.append({"player_id": pid, "name": name, "status": st,
                            "why": "still in the starting lineup; "
                                   "lineup runs first and will bench him"})
            continue
        if room <= 0:
            blocked.append({"player_id": pid, "name": name, "status": st,
                            "why": f"all {sl['ir_slots']} IR slots are full and "
                                   f"more valuable men hold them; he is the "
                                   f"cheapest body we could cut if a slot is "
                                   f"needed"})
            continue
        room -= 1
        to_reserve.append({"player_id": pid, "name": name, "status": st,
                           "why": f"designated {st}, which this league allows on "
                                  f"reserve; parking him frees an active slot"})

    space = sl["open"] + len(to_reserve)
    for pid in reserve:
        st = (players.get(pid) or {}).get("injury_status") or ""
        if st in ok:
            continue
        name = api.player_name(players, pid)
        if space <= 0:
            blocked.append({"player_id": pid, "name": name, "status": st or "healthy",
                            "why": "no longer IR-eligible but the active roster is "
                                   "full; the account is frozen until unblock() "
                                   "activates him and cuts back to the limit"})
            continue
        space -= 1
        to_activate.append({"player_id": pid, "name": name, "status": st or "healthy",
                            "why": f"no longer carries an IR-eligible designation "
                                   f"({st or 'healthy'}), so he cannot stay on "
                                   f"reserve"})

    target = [p for p in reserve if p not in {m["player_id"] for m in to_activate}]
    target += [m["player_id"] for m in to_reserve]
    return {"reserve": to_reserve, "activate": to_activate, "blocked": blocked,
            "current": reserve, "target": target, "slots": sl,
            "changed": sorted(target) != sorted(reserve)}


def render(p: dict) -> str:
    sl = p["slots"]
    L = [f"IR: {sl['ir_used']}/{sl['ir_slots']} used, "
         f"active roster {sl['active']}/{sl['roster_max']} ({sl['open']} open)"]
    if not IR_ENABLED:
        L.append("  IR_ENABLED is off - reporting only, nothing will move")
    for m in p["reserve"]:
        L.append(f"  -> RESERVE   {m['name']} [{m['status']}] - {m['why']}")
    for m in p["activate"]:
        L.append(f"  -> ACTIVATE  {m['name']} [{m['status']}] - {m['why']}")
    for m in p["blocked"]:
        L.append(f"     blocked   {m['name']} [{m['status']}] - {m['why']}")
    if not (p["reserve"] or p["activate"] or p["blocked"]):
        L.append("  nothing to move")
    return "\n".join(L)


def run(apply: bool = False, league_id: str = LEAGUE_ID_2026,
        verbose: bool = True) -> dict:
    from robo import construction
    with construction.session("ir sweep", apply=apply):
        return _run(apply, league_id, verbose)


def _run(apply: bool, league_id: str, verbose: bool) -> dict:
    p = plan(league_id)
    if verbose:
        print(render(p))
    p["applied"] = False
    if not apply or not IR_ENABLED or not p["changed"]:
        return p
    # The target still lists any stuck man, and Sleeper refuses a reserve update
    # that leaves one there. unblock() runs first everywhere; this is the net.
    why = frozen(league_id)
    if why:
        p["write_blocked"] = why
        if verbose:
            print(f"not applied: {why}")
        return p

    from robo.decisions import record
    from robo.sleeper_write import set_reserve
    r = season.mine(league_id)
    set_reserve(r["roster_id"], p["target"], league_id,
                reason="IR sweep: " + "; ".join(m["why"] for m in
                                                p["reserve"] + p["activate"]))
    p["applied"] = True

    moved = [f"{m['name']} to injured reserve" for m in p["reserve"]]
    moved += [f"{m['name']} back to the active roster" for m in p["activate"]]
    why = "; ".join(m["why"] for m in p["reserve"] + p["activate"])
    freed = len(p["reserve"]) - len(p["activate"])
    if freed > 0:
        why += (f". That leaves {freed} active roster spot(s) open, which the "
                f"next waiver or free-agent run may spend. Reserving and signing "
                f"stay separate decisions: this one is bookkeeping and cannot be "
                f"wrong, the other is a valuation and can be")
    record("ir", "Injured reserve updated", "Moved " + ", ".join(moved) + ".",
           why + ".", data={"reserve": p["target"], "previous": p["current"]})

    try:
        from robo import moves
        p["waiver_maintenance"] = moves.maintain_pending_claims(
            apply=True, league_id=league_id, reason="IR roster changed")
    except Exception as e:
        p["waiver_maintenance"] = {"status": "failed",
                                    "error": f"{type(e).__name__}: {e}"}

    if verbose:
        print("applied.")
    return p


def warnings(league_id: str = LEAGUE_ID_2026) -> list[str]:
    """Blocked moves worth a human's attention, for the status page.

    Deliberately NOT a chat alert. Neither of these is urgent enough to earn a
    permanent GroupMe post, and Nate reads the status page; blasting an
    operational nit into the league chat is the kind of noise that gets a bot
    muted. The genuinely time-critical case -- a lineup that cannot be made
    legal before kickoff -- is alerted from lineup.py, not here.
    """
    out = []
    for m in plan(league_id)["blocked"]:
        if "IR slots are full" in m["why"]:
            out.append(f"{m['name']} is {m['status']} and IR is full - an active "
                       f"roster spot is held by a player who cannot play")
        elif "active roster is full" in m["why"]:
            out.append(f"{m['name']} is no longer IR-eligible and the roster is "
                       f"full - activating him needs a cut")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--unblock", action="store_true",
                    help="only the unblock pass, not the sweep")
    ap.add_argument("--league", default=LEAGUE_ID_2026)
    args = ap.parse_args()
    ub = unblock(apply=args.apply, league_id=args.league)
    if args.unblock:
        return
    run(apply=args.apply, league_id=args.league)
    if args.apply and not ub["legal"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
