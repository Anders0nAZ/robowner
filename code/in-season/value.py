"""Rest-of-season player value -- the transaction valuation seam.

WHAT THIS IS. What a player is worth from week n forward, and therefore what we
gain by swapping him for somebody we hold. Skill-player value comes from
robo/expected.py and is converted into roster-level marginal value by
robo/marginal.py. This module is the seam every consumer imports, so there is
exactly one place that decides whether the bot is allowed to act on it.

THE GATE IS STILL HERE AND STILL MEANS SOMETHING. It is now open, but it remains
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

from robo import ros

# Is the NUMBER real? Yes -- expected.py is built and every figure printed is
# the one the bot would act on.
VALUATION_READY = True

# May the bot ACT on it? No. The injury-response implementation remains in dry
# review until Nate explicitly approves opening the transaction gate.
#
# THESE ARE TWO DIFFERENT QUESTIONS AND WERE ONE FLAG FOR A DAY, WHICH WAS A
# MISTAKE. Collapsing them means the only way to stop the bot submitting is to
# also make it print the provisional preseason board -- so the review would be
# reading stand-in numbers to decide whether to trust the real ones, which is
# exactly backwards. Split, a dry run shows precisely what would have been
# submitted, priced on the real valuation, and submits none of it.
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
    """
    if ready():
        try:
            from robo import marginal
            return marginal.drop_price(row["player_id"]), True
        except Exception:
            pass
    return value_of(row, week, "hold")
