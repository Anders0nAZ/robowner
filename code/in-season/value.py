"""Rest-of-season player value -- the transaction valuation seam.

WHAT THIS IS. What a player is worth from week n forward, and therefore what we
gain by swapping him for somebody we hold. Skill-player value comes from
robo/expected.py and is converted into roster-level marginal value by
robo/marginal.py. This module is the seam every consumer imports, so there is
exactly one place that decides whether the bot is allowed to act on it.

THE GATE IS STILL HERE AND STILL MEANS SOMETHING. It is closed pending human
review, and it remains
a constant in code rather than a setting: it is deliberately absent from the
settings registry, so no data/settings.json edit and no admin GUI field can
close or reopen it. Turning the bot loose on the roster took a commit, and
taking it back will take one too. That matters because roster decisions are not
reversible the way a lineup is -- a dropped player is claimed by somebody else
within the hour.

ADDS AND DROPS REMAIN ASYMMETRIC. expected.py puts fitted role inheritance into
each weekly mean without double-counting a provider projection that already
moved. marginal.py then prices our drop candidates in the actual roster and
lineup context, preserving upside without relying on a separate season-total
calibration or generic news multiplier.
"""

import time

from robo import ros

# Is the NUMBER real? Yes -- expected.py is built and every figure printed is
# the one the bot would act on.
VALUATION_READY = True

# May the bot ACT on it? YES, since 15 Sep 2026, on Nate's explicit approval
# after a full review of the waiver portfolio, the claim tiering and the bid
# model. `python -m robo.value --preflight` was clean at the time it was
# opened: fresh valuation and model artifacts, every source validated,
# transaction states self-consistent, a buildable slate, every bid inside the
# budget and no locked player in any payload.
#
# THESE ARE TWO DIFFERENT QUESTIONS AND WERE ONE FLAG FOR A DAY, WHICH WAS A
# MISTAKE. Collapsing them means the only way to stop the bot submitting is to
# also make it print the provisional preseason board -- so the review would be
# reading stand-in numbers to decide whether to trust the real ones, which is
# exactly backwards. Split, a dry run shows precisely what would have been
# submitted, priced on the real valuation, and submits none of it.
#
# TO CLOSE IT AGAIN: set this False. That is the whole kill switch for roster
# writes, and it beats disabling tasks because it leaves the bot reading,
# planning and publishing while it stops it acting.
SUBMIT_ENABLED = False

GATE_MESSAGE = (
    "the roster valuation is live but the transaction gate is closed pending "
    "human review; --apply will not submit to Sleeper. See robo/value.py.")


def ready() -> bool:
    """Is there a real rest-of-season number to reason with?"""
    return bool(VALUATION_READY)


def may_submit() -> bool:
    """May a roster move actually be sent to Sleeper?

    Owned by code, never by config: deliberately absent from the settings
    registry, so no data/settings.json edit and no admin GUI field can flip it.
    Turning the bot loose on the roster takes a commit, because roster decisions
    are not reversible the way a lineup is -- a dropped player is claimed by
    somebody else within the hour.
    """
    return bool(VALUATION_READY and SUBMIT_ENABLED)


def ros_value(player_id: str, week: int, field: str = "mean") -> float:
    """What this player is worth from `week` to the end of the season.

    EXPECTED IS THE VALUATION; ros.json PRICES WHAT IT DOES NOT MODEL. Pointing
    this at ros.json was the last place the superseded number gated a live
    decision, and it gated the one that matters most: moves.candidates() ranks
    the whole wire through here and moves.priced() simulates only the top ten.
    So a man ros.py rates at zero could never reach the simulator at all. In the
    week-2 concussion scenario, Mac Jones -- newly San Francisco's starting
    quarterback, unowned, worth 233 -- sat at position 406 of 424 on a ros.mean
    of 0.0 and was never evaluated. The new model could re-price candidates the
    old one liked; it could not surface one the old one missed.

    BOTH FIELDS MAP TO THE SAME NUMBER, and that is the design rather than a
    shortcut. expected.py folds inheritance INTO the mean instead of adding it
    on, so there is no separate `hold` to look up -- see hold_of(), which prices
    OUR men by simulation and only lands here for somebody else's bench.

    Kicker and defence fall through to ros.json, which is not a fallback but the
    right source: expected.py models neither, and says so.
    """
    try:
        from robo import expected
        row = (expected.load().get("players") or {}).get(str(player_id))
        if row and row.get("ros") is not None:
            return float(row["ros"])
    except Exception:
        pass
    return ros.value(player_id, week, field)


# How many weeks ahead counts as "now" for a roster decision. Three covers the
# span a fill-in is actually for: a concussion protocol, a one-week absence that
# becomes two, a bye. Further out and the wire has turned over anyway, which is
# the same reasoning behind moves.BYE_LOOKAHEAD_WEEKS.
NEAR_WEEKS = 3


def near_value(player_id: str, week: int, horizon: int = NEAR_WEEKS,
               table: dict | None = None) -> float:
    """What he is worth over the next few weeks, not the whole season.

    A REST-OF-SEASON TOTAL CANNOT RANK A FILL-IN, and that is not a matter of
    degree. The two numbers answer different questions, and a man whose value is
    concentrated in the next fortnight has a small season total by construction:
    with our quarterback concussed, his backup was the best free agent in the
    league for the week the decision was about and the HUNDREDTH by season
    total. moves.priced() simulates the top ten, so he could not be reached.

    Weighted the same way the season total is, so the two orderings are the same
    quantity over different horizons rather than two different measures.
    """
    try:
        from robo import expected
        # `table` is expected.load() hoisted out of the caller's loop. Without
        # it this re-reads and re-parses a 3MB file once per candidate, which a
        # 424-man wire turns into ~850 parses of the same bytes.
        d = table if table is not None else expected.load()
        row = (d.get("players") or {}).get(str(player_id))
        if not row:
            return 0.0
        by = row.get("by_week") or {}
        return round(sum(ros.weight_of(d, w) * (by.get(str(w)) or {}).get("final", 0.0)
                         for w in range(week, week + horizon)), 3)
    except Exception:
        return 0.0


def provisional(row: dict) -> float:
    """A STAND-IN, kept only for the shut-gate path.

    This is the preseason board number: full-season projections blended with
    expert consensus, frozen before week 1. It is the wrong quantity in two
    obvious ways -- it values a whole season when only part of one remains, and
    it has not heard about anything that happened since the draft. It survives
    so that closing the gate again produces readable output instead of an
    exception, and it must never be promoted to the real thing by deleting its
    label; the real thing is a different calculation, not this one with more
    confidence.
    """
    return float(row.get("blend_pts") or row.get("proj_pts") or 0.0)


def value_of(row: dict, week: int, field: str = "mean") -> tuple[float, bool]:
    """(value, is_real). Callers must surface `is_real` to the reader."""
    if ready():
        return ros_value(row["player_id"], week, field), True
    return provisional(row), False


def hold_of(row: dict, week: int) -> tuple[float, bool]:
    """What we give up by cutting this man. (value, is_real).

    PRICED BY SIMULATION -- robo/marginal.py, which asks what our starting
    lineup loses across the seasons it draws. ros.hold could not answer that: it
    made Carson Beck the cheapest man on our roster to cut at 0.4 while the
    simulator puts him at 37.5, above six of our starters, because only the
    simulator can see the worlds where our other two quarterbacks are missing.
    The ordering was not merely imprecise, it was inverted.

    ros.hold remains the fallback for a man the simulator does not carry, and
    choosing between them here rather than in moves.py keeps this module what it
    claims to be -- the one place that decides which number a decision is
    allowed to use.

    THIS RETURNS THE MEAN, AND THE TAIL IS A SEPARATE QUESTION. See hold_shape:
    the loss tail protects a lottery ticket from being cut, but it is a GATE and
    not a price, exactly as the ceiling is on the add side. Ranking one man's
    tail against another man's mean in a single sorted pool put Kaelon Black
    above Nico Collins as the more expensive cut, which is not a protection --
    it is a different incoherence in the other direction.
    """
    if ready():
        try:
            from robo import marginal
            return marginal.drop_price(row["player_id"]), True
        except Exception:
            pass
    return value_of(row, week, "hold")


def hold_shape(row: dict, week: int) -> dict | None:
    """The full distribution behind hold_of, for the protection gate.

    WHY A GATE AND NOT A PRICE. moves.clears() judges a bench ACQUISITION on
    `ceiling >= HIT_POINTS` -- a bar -- while best_free still RANKS on the mean.
    The ceiling decides whether a man is worth a transaction at all; the mean
    decides which of the survivors is best. Mirroring that on the drop side
    keeps one statistic doing one job: the mean orders the pool, and the loss
    tail says whether a man may be in it.

    Pricing the drop on the mean alone was the real asymmetry -- shape() calls
    the mean "selecting on noise" down there and names Kaelon Black as the
    example, and the add side agrees, so a lottery ticket was bought on his tail
    and sold on his middle.

    None for a man the simulator does not carry, so the caller keeps whatever
    behaviour it had rather than reading an absent shape as "unprotected".
    """
    if not ready():
        return None
    try:
        from robo import marginal
        return marginal.drop_shape(row["player_id"])
    except Exception:
        return None


# ----------------------------------------------------------- opening the gate

def _check(label: str, ok: bool, why: str = "", detail: str = "") -> dict:
    """One assertion. `why` is the failure; `detail` is true either way.

    Kept apart because a reason phrased as a failure reads as one: printing
    "only 802 players priced" beside a green OK is the kind of line that sends
    somebody looking for a problem that is not there.
    """
    # `check` is the STABLE identity. `label` is prose and one row rewrites its
    # own to say which way the gate is set, so a caller keying on it to find a
    # specific assertion finds nothing the moment that happens.
    return {"check": label, "label": label, "ok": bool(ok),
            "why": "" if ok else why, "detail": detail}


def preflight(league_id: str | None = None) -> list[dict]:
    """What has to be true before SUBMIT_ENABLED may be turned on.

    ASSERTIONS, NOT A DESCRIPTION. Each row either holds or says what is wrong,
    in the same shape as status.preflight()'s draft-readiness list, and each one
    exists because of a way this could go wrong silently rather than loudly:

      1. A stale valuation submits yesterday's opinion with today's confidence.
      2. A failed source keeps the OLD file, on purpose -- so the pipeline
         reports a clean run over a number nobody refreshed.
      3. A transaction classified wrong is the difference between a free add
         and a claim Sleeper will reject.
      4. The dry-run slate is the thing a human is being asked to approve. If
         it cannot be built, there is nothing to approve.
      5. A locked player in a payload is a write that fails AFTER the plan has
         been published as a decision.

    This changes nothing. It cannot open the gate and it is not consulted when
    a move is submitted -- may_submit() remains the single authority, and it is
    a constant in this file. This is what the operator reads first.
    """
    from robo import LEAGUE_ID_2026, expected, ir, moves, season
    from robo.rankings import build_board
    league_id = league_id or LEAGUE_ID_2026
    out = [_check("transaction gate", not SUBMIT_ENABLED,
                  "already open" if SUBMIT_ENABLED else "")]
    out[0]["label"] = ("transaction gate is %s"
                       % ("OPEN" if SUBMIT_ENABLED else "closed"))

    # 1. Fresh valuation artifacts.
    try:
        table = expected.load()
        age = time.time() - float(table.get("computed") or 0)
        out.append(_check(
            "valuation is current", age < moves.ROS_VALUE_MAX_AGE,
            "expected.json is %.1fh old; the limit is %.1fh"
            % (age / 3600, moves.ROS_VALUE_MAX_AGE / 3600),
            detail="%.1fh old" % (age / 3600)))
        out.append(_check("valuation covers the wire",
                          len(table.get("players") or {}) > 500,
                          "only %d players priced" % len(table.get("players") or {}),
                          detail="%d players priced" % len(table.get("players") or {})))
    except Exception as e:
        out.append(_check("valuation is current", False,
                          f"expected.json unreadable: {type(e).__name__}: {e}"))
    try:
        from robo import model_proj
        hours = model_proj.age_hours()
        out.append(_check("weekly model projection is current",
                          hours is not None and hours < 24,
                          "model_week.json is %s"
                          % ("missing" if hours is None else "%.1fh old" % hours),
                          detail=("missing" if hours is None
                                  else "%.1fh old" % hours)))
    except Exception as e:
        out.append(_check("weekly model projection is current", False,
                          f"{type(e).__name__}: {e}"))

    # 2. Zero source-validation failures. Every fetch in the pipeline keeps the
    # old file when validation fails, so a bad pull is invisible in the data.
    try:
        from robo import status as st
        bad = [r for r in st.ingests()
               if r["status"] == st.BAD or r.get("failed_since")]
        out.append(_check("every data source validated",
                          not bad,
                          "; ".join("%s: %s" % (r["label"], r.get("why") or "failed")
                                    for r in bad)))
    except Exception as e:
        out.append(_check("every data source validated", False,
                          f"freshness could not be read: {type(e).__name__}: {e}"))

    # 2b. The usage panel holds the latest COMPLETED week. Age cannot answer
    # this: a file rewritten every morning with last week's contents passes a
    # timestamp check forever, which is how the frozen season-projection spine
    # went unnoticed for two weeks. Asserted rather than reported because the
    # takeover prior is fitted on this panel and a stale one silently prices
    # every backup off a week that has already been played.
    try:
        from robo import roles, season as _season
        fr = roles.freshness(_season.SEASON)
        out.append(_check("usage panel holds the last completed week",
                          bool(fr.get("ok")), fr.get("why") or "",
                          f"through week {fr.get('have_week')}"
                          if fr.get("have_week") is not None else ""))
    except Exception as e:
        out.append(_check("usage panel holds the last completed week", False,
                          f"panel could not be read: {type(e).__name__}: {e}"))

    # 3. Transaction classification. Checked for internal contradiction rather
    # than against a second opinion: there isn't one, and a state that
    # contradicts itself is the failure that would actually reach Sleeper.
    try:
        season.invalidate_live()
        week = season.current_week()
        held = season.rostered_ids(league_id)
        board = [r["player_id"] for r in build_board()]
        states = season.transaction_states(board, league_id, week=week)
        wrong = []
        for pid, s in states.items():
            if pid in held and s["acquisition"] != "unavailable":
                wrong.append(f"{pid} is rostered but reads {s['acquisition']}")
            if s["acquisition"] == "weekly_waiver" and not s.get("unlock_at"):
                wrong.append(f"{pid} is on weekly waivers with no unlock time")
            if (s["roster_movement"] == "roster_locked"
                    and s["acquisition"] == "free_now"):
                wrong.append(f"{pid} is locked and free to add at the same time")
        out.append(_check("transaction states are consistent", not wrong,
                          "; ".join(wrong[:5])
                          + (f" (+{len(wrong) - 5} more)" if len(wrong) > 5 else "")))
    except Exception as e:
        out.append(_check("transaction states are consistent", False,
                          f"{type(e).__name__}: {e}"))
        states = {}

    # 4 and 5. The dry-run slate, and nothing locked in what it would send.
    locked = {pid for pid, s in (states or {}).items()
              if s["roster_movement"] == "roster_locked"}
    payload_ids = []
    try:
        slate = moves.run("claims", apply=False, league_id=league_id,
                          mode="ros", verbose=False)
        ok = not slate.get("control_block")
        out.append(_check("a waiver slate can be built", ok,
                          slate.get("control_block") or ""))
        budget = season.faab_left(league_id)
        claims = [c for s in (slate.get("plans") or []) for c in s["claims"]]
        over = [c for c in claims if int(c["bid"]) > budget]
        out.append(_check("every bid is inside the budget", not over,
                          "%d claim(s) bid more than the $%d left"
                          % (len(over), budget),
                          detail="%d claim(s) against $%d" % (len(claims), budget)))
        payload_ids += [v for p in (slate.get("payloads") or [])
                        for v in _payload_players(p)]
    except Exception as e:
        out.append(_check("a waiver slate can be built", False,
                          f"{type(e).__name__}: {e}"))
    try:
        free = moves.run("free", apply=False, league_id=league_id,
                         mode="ros", verbose=False)
        payload_ids += [v for p in (free.get("payloads") or [])
                        for v in _payload_players(p)]
    except Exception as e:
        out.append(_check("a free-agent pass can be built", False,
                          f"{type(e).__name__}: {e}"))
    try:
        # The MOVES, not the target list. `target` restates the whole desired
        # reserve, so a man who has sat on IR all week and is going nowhere
        # appears in it -- and he is locked every Sunday, which would fail this
        # check every week over a write that never happens.
        sweep = ir.plan(league_id)
        payload_ids += [str(m["player_id"]) for m in
                        (sweep.get("reserve") or []) + (sweep.get("activate") or [])]
    except Exception as e:
        out.append(_check("an IR sweep can be built", False,
                          f"{type(e).__name__}: {e}"))
    caught = sorted(set(payload_ids) & locked)
    out.append(_check("no locked player in any payload", not caught,
                      "locked: " + ", ".join(caught)))
    return out


def _payload_players(payload: dict) -> list:
    """Every player id inside one transaction payload.

    Reads the PARALLEL-ARRAY form the writers actually send (`k_adds`/`v_adds`),
    not a convenient reshaping of it -- the point of this check is that what
    goes on the wire carries nobody Sleeper has locked.
    """
    return [str(pid) for key in ("k_adds", "k_drops")
            for pid in (payload.get(key) or []) if pid is not None]


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="What has to be true before the transaction gate opens.")
    ap.add_argument("--preflight", action="store_true")
    args = ap.parse_args()
    if not args.preflight:
        print(GATE_MESSAGE if not may_submit() else
              "the transaction gate is OPEN; --apply submits to Sleeper")
        return
    rows = preflight()
    for r in rows:
        note = r["why"] or r.get("detail") or ""
        if r["check"] == "transaction gate":
            # Not OK/FAIL. It is the switch, and an open gate printing FAIL
            # reads as a fault rather than as the thing we chose on purpose.
            print(f"  GATE  {r['label']}")
            continue
        print(f"  {'OK  ' if r['ok'] else 'FAIL'}  {r['label']}"
              + (f" -- {note}" if note else ""))
    # THE GATE ROW IS A STATE, NOT A FAULT. Every other row is an assertion
    # about readiness; this one reports which side of the switch we are on, and
    # counting it as a failure made the whole report read "do not open the
    # gate" from the moment the gate was opened -- which is the exact moment
    # the operator starts running this WEEKLY to check everything else.
    bad = [r for r in rows if not r["ok"] and r["check"] != "transaction gate"]
    if bad:
        print(f"\n{len(bad)} check(s) failed; "
              + ("close the gate until they are fixed" if may_submit()
                 else "do not open the gate"))
    elif may_submit():
        print("\npreflight clean; the gate is OPEN and --apply submits to "
              "Sleeper. Close it by setting SUBMIT_ENABLED False in robo/value.py.")
    else:
        print("\npreflight clean; opening the gate is a commit to "
              "SUBMIT_ENABLED in robo/value.py and a process restart")


if __name__ == "__main__":
    main()
