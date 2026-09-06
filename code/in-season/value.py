"""Rest-of-season player value -- the seam, now wired to a real number.

WHAT THIS IS. What a player is worth from week n forward, and therefore what we
gain by swapping him for somebody we hold. The model lives in robo/ros.py; this
module is the seam every consumer imports, so there is exactly one place that
decides whether the bot is allowed to act on it.

THE GATE IS STILL HERE AND STILL MEANS SOMETHING. It is now open, but it remains
a constant in code rather than a setting: it is deliberately absent from the
settings registry, so no data/settings.json edit and no admin GUI field can
close or reopen it. Turning the bot loose on the roster took a commit, and
taking it back will take one too. That matters because roster decisions are not
reversible the way a lineup is -- a dropped player is claimed by somebody else
within the hour.

TWO NUMBERS, NOT ONE, AND THE ASYMMETRY IS DELIBERATE. `mean` is what a player
is worth to us and prices an ADD. `hold` is `mean + upside` and prices a DROP,
where upside is what he stands to inherit if the man ahead of him goes down.
Using one number for both is what makes a bot cut a rookie in October and watch
somebody else start him in December. See robo/ros.py.
"""

from robo import ros

# Is the NUMBER real? Yes -- ros.py is built and every figure printed is the one
# the bot would act on.
VALUATION_READY = True

# May the bot ACT on it? No, not until Nate has reviewed it module by module.
#
# THESE ARE TWO DIFFERENT QUESTIONS AND WERE ONE FLAG FOR A DAY, WHICH WAS A
# MISTAKE. Collapsing them means the only way to stop the bot submitting is to
# also make it print the provisional preseason board -- so the review would be
# reading stand-in numbers to decide whether to trust the real ones, which is
# exactly backwards. Split, a dry run shows precisely what would have been
# submitted, priced on the real valuation, and submits none of it.
SUBMIT_ENABLED = False

GATE_MESSAGE = (
    "the rest-of-season valuation is BUILT and every number below is the real "
    "one -- but submitting is switched off pending review, so nothing here has "
    "happened. This is exactly what the bot would have done. See robo/value.py.")


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
    """What this player is worth from `week` to the end of the season."""
    return ros.value(player_id, week, field)


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


def hold_of(row: dict, week: int, mine: bool = True) -> tuple[float, bool]:
    """What we give up by cutting this man. (value, is_real).

    OUR OWN MEN ARE PRICED BY SIMULATION -- robo/marginal.py, which asks what our
    starting lineup loses across the seasons it draws. ros.hold could not answer
    that: it made Carson Beck the cheapest man on our roster to cut at 0.4 while
    the simulator puts him at 37.5, above six of our starters, because only the
    simulator can see the worlds where our other two quarterbacks are missing.
    The ordering was not merely imprecise, it was inverted.

    `mine=False` keeps ros.hold, and that is a limit rather than an oversight:
    the blocking test prices somebody else's bench, and we do not know who he
    would start. Owning both branches here rather than in moves.py keeps this
    module what it claims to be -- the one place that decides which number a
    decision is allowed to use.
    """
    if mine and ready():
        try:
            from robo import marginal
            return marginal.drop_price(row["player_id"]), True
        except Exception:
            pass
    return value_of(row, week, "hold")
