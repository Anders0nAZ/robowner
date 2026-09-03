"""This week's projections from the NFL Model, or nothing at all.

The NFL Model repo simulates 4,000 stat lines per player and scores
them under this league's own settings, which makes its number strictly more
complete than Sleeper's weekly projection: Sleeper's feed carries 23 of our 57
scoring keys, so its number has never included a quarterback's sack penalty,
any bonus tier, a 40+ yard touchdown, or return yardage.

THE ARTIFACT IS THE INTERFACE, not an import. robo.lineup runs unattended twice
a day and writes to Sleeper. Importing the model would put a nflverse download,
a decade of play-by-play, and four thousand simulations inside that write --
so a stall in the other repo becomes a lineup that never gets set. Reading a
file it validated this morning means a broken model goes stale instead, and
stale falls back to Sleeper, which is what the optimizer ran on before.

NOTHING HERE RAISES. Every failure returns an empty map and a reason. The
reason is carried into the decision log and the status page, because a silent
fallback is how you find out in December that you have been benching people on
last month's numbers.
"""

import json
from datetime import datetime, timezone

from robo import DATA, season, settings

MODEL_FILE = DATA / "model_week.json"

SCHEMA = 1

# Which engine prices the weekly lineup. Off returns it to Sleeper's
# 23-of-57-key projection -- what it ran on before the model existed, and the
# one-line rollback if the model starts producing something strange mid-season.
USE_MODEL = True

# How stale the artifact may be before the lineup ignores it. Sized to the
# DAILY export, so a day the pre-kickoff runs are missed still leaves a usable
# number; the pre-kickoff refresh tightens it in practice without this having
# to know the kickoff schedule. Too high and a Sunday afternoon lineup runs on
# a Tuesday simulation that never heard about a Saturday scratch. Too low and
# one failed export benches the model for the rest of the week.
MAX_AGE_H = 30.0

settings.apply(__name__, globals())


def _age_hours(stamp: str) -> float:
    t = datetime.fromisoformat(stamp)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0


def load() -> tuple[dict, str]:
    """(artifact, reason). ({}, why) when it must not be used.

    Checked in the order a reader would want them reported: switched off,
    absent, unreadable, the wrong week, then too old.
    """
    if not USE_MODEL:
        return {}, "model projections switched off (model_proj.USE_MODEL)"
    if not MODEL_FILE.exists():
        return {}, f"no model artifact at {MODEL_FILE.name}"
    try:
        d = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {}, f"model artifact unreadable: {str(e)[:80]}"
    if d.get("schema") != SCHEMA:
        return {}, f"model artifact schema {d.get('schema')}, expected {SCHEMA}"
    if not isinstance(d.get("players"), dict) or not d["players"]:
        return {}, "model artifact carries no players"
    try:
        age = _age_hours(d.get("generated_utc") or "")
    except Exception:
        return {}, "model artifact has no readable generated_utc"
    # A future timestamp is a clock problem, not freshness. Treated as unusable
    # rather than as infinitely fresh, which is what a bare age check would do.
    if age < -1.0:
        return {}, f"model artifact is stamped {-age:.1f}h in the future"
    if age > MAX_AGE_H:
        return {}, f"model artifact is {age:.1f}h old (limit {MAX_AGE_H:.0f}h)"
    return d, ""


def age_hours() -> float | None:
    """How old the artifact the lineup would actually use is, or None.

    None for every reason load() refuses it, so a caller cannot accidentally
    report an age for a file nothing is reading.
    """
    d, _ = load()
    if not d:
        return None
    try:
        return _age_hours(d["generated_utc"])
    except (KeyError, TypeError, ValueError):
        return None


def week_projections(week: int, season_yr: str = season.SEASON,
                     league_id: str | None = None) -> tuple[dict, str]:
    """player_id -> {mean, p10, p25, p50, p75, p90}, plus a provenance line.

    ({}, reason) whenever the artifact is missing, stale, or about a different
    week -- the caller then keeps whatever it had, which is Sleeper's number.
    """
    d, why = load()
    if not d:
        return {}, why
    if str(d.get("season")) != str(season_yr) or int(d.get("week", -1)) != int(week):
        return {}, (f"model artifact is {d.get('season')} week {d.get('week')}, "
                    f"not {season_yr} week {week}")
    if league_id and d.get("league_id") != league_id:
        return {}, f"model artifact is for league {d.get('league_id')}"
    age = _age_hours(d["generated_utc"])
    # The anchor's first field is the projection snapshot the whole simulation
    # was built on, which is the part that makes a published number auditable.
    # Everything after the first separator is the model talking to its operator
    # -- which players it flagged as questionable, which ids it could not
    # resolve -- and the decision log is read by the league, not by us.
    snap = str(d.get("anchor") or "unknown").split("  |  ")[0].strip()
    return d["players"], (f"NFL Model, {len(d['players'])} players, {age:.1f}h old, "
                          f"anchored on {snap}")


def main():
    """python -m robo.model_proj -- what the lineup would see right now."""
    wk = season.current_week()
    proj, why = week_projections(wk)
    if not proj:
        print(f"week {wk}: NOT USED — {why}")
        return
    print(f"week {wk}: {why}\n")
    top = sorted(proj.items(), key=lambda kv: -kv[1]["mean"])[:20]
    print(f"  {'player':<24}{'pos':<5}{'p10':>7}{'mean':>7}{'p90':>7}")
    for pid, p in top:
        print(f"  {p['name'][:24]:<24}{p['pos']:<5}"
              f"{p['p10']:>7.1f}{p['mean']:>7.1f}{p['p90']:>7.1f}")


if __name__ == "__main__":
    main()
