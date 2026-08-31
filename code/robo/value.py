"""Rest-of-season player value -- the seam, deliberately not implemented.

WHAT GOES HERE EVENTUALLY: what a player is worth from week n forward, and
therefore what we gain by swapping him for somebody we hold. That is a model.
It needs deciding how remaining-week projections compose, how a scout verdict
and a buzz signal fold in, how much of a full season is even left to value by
week 11, and what "gain" means when it becomes a FAAB bid. None of that has been
designed yet, so none of it is guessed at here.

WHY THE GATE EXISTS. The machinery around this -- discovering free agents,
partitioning waivers from the wire, picking a drop, building a claim slate,
submitting -- is ordinary mechanics and it is finished and tested. The
temptation is then to wire up "something reasonable" so it can start running.
That is exactly how an underbaked number becomes the thing making real roster
decisions, and roster decisions are not reversible the way a lineup is: a
dropped player is claimed by somebody else within the hour.

So moves.py asks this module for permission and does not get it.

VALUATION_READY IS OWNED BY CODE, NOT CONFIG. It is deliberately absent from the
settings registry, so no data/settings.json edit and no admin GUI field can flip
it. Turning the bot loose on the roster should require a commit.
"""

VALUATION_READY = False

GATE_MESSAGE = (
    "rest-of-season valuation is not built, so no roster move will be submitted. "
    "The machinery ran and the slate below is what it WOULD do using a "
    "provisional stand-in number; it is not a recommendation. See robo/value.py."
)


def ready() -> bool:
    return bool(VALUATION_READY)


def ros_value(player_id: str, week: int) -> float:
    """What this player is worth from `week` to the end of the season."""
    raise NotImplementedError(
        "rest-of-season valuation not built -- see the module docstring")


def provisional(row: dict) -> float:
    """A STAND-IN so the machinery produces readable output in dry runs.

    This is the preseason board number: full-season projections blended with
    expert consensus, frozen before week 1. It is the wrong quantity in two
    obvious ways -- it values a whole season when only part of one remains, and
    it has not heard about anything that happened since the draft -- and it is
    here only so a dry run prints something a human can sanity-check the
    PLUMBING against.

    Every line that uses it is labelled PROVISIONAL. It must never be promoted
    to the real thing by deleting the label; the real thing is a different
    calculation, not this one with more confidence.
    """
    return float(row.get("blend_pts") or row.get("proj_pts") or 0.0)


def value_of(row: dict, week: int) -> tuple[float, bool]:
    """(value, is_real). Callers must surface `is_real` to the reader."""
    if ready():
        return ros_value(row["player_id"], week), True
    return provisional(row), False
