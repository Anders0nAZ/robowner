"""Implied team totals from the betting market, for the weeks the books have posted.

WHAT THIS IS FOR. Sleeper's weekly projection feed carries no matchup at all --
Josh Allen reads 23.1 / 22.8 / 23.0 for weeks 1 / 5 / 14 -- and for defences it
carries nothing usable at all, because every pts_allow tier is one of the 22
scoring keys the feed omits. Streaming a defence is entirely a question of who
they play, so it needs a different input, and the market is the honest one: a
team's implied total IS the consensus forecast of how much they will score.

WHERE THE NUMBERS COME FROM. nflverse's schedule table, downloaded and kept
fresh by Roboner's private NFL model under data/nflmodel/parquet. Read directly
so the chat responder never imports the heavy simulation stack.

The arithmetic and the two team-code maps are transcribed from
nflmodel/dist/kdef.py:history and nflmodel/teams.py, which stay the source of
truth. They are eight lines and two dicts; a shared package for them would be a
third repo to keep in sync.

THE HORIZON IS DATA, NOT A CONSTANT. Books post a week or two out, not a season.
Measured 3 Sep 2026: 14-16 of 16 games have a line for weeks 1-6, then 7 games
at week 7 and one or two a week after that. So a lookahead is four to six weeks
today and will settle to two or three in-season, and a week with no line must
report that it has none. Imputing one would invent a matchup, which is the exact
thing this module exists to avoid.

    python -m robo.vegas --week 5
    python -m robo.vegas --coverage
"""

import argparse
from functools import lru_cache

from robo import MODEL_DATA

PARQUET = MODEL_DATA / "parquet" / "schedules.parquet"
# THE LINE SOURCE: ESPN's live board, nflverse only per game as a fallback --
# see nflmodel/ingest/lines.py, which writes it. Read as JSON so nothing here
# imports the model. The parquet above still supplies kickoff times, and lines
# for any week the artifact has not been built for.
LINES = MODEL_DATA / "lines" / "current.json"

# Sleeper's dialect -> nflverse's. Sleeper writes LAR where nflverse writes LA.
# Transcribed from nflmodel/teams.py ALIAS.
ALIAS = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS", "ARZ": "ARI", "OAK": "LV"}

# nflverse's own two tables disagree about relocated franchises: `schedules`
# uses the code worn AT THE TIME, `player_stats` stamps the current one on every
# season. Transcribed from nflmodel/teams.py RELOCATED.
RELOCATED = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

# A week is only worth planning around if most of its games are priced. Below
# this the "best available defence" is really "the best of whichever four teams
# the book happened to post first", which is not the same question.
MIN_COVERAGE = 0.6

from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())


def team_code(t: str) -> str:
    """A Sleeper team code in nflverse's vocabulary."""
    t = (t or "").upper()
    return RELOCATED.get(ALIAS.get(t, t), ALIAS.get(t, t))


@lru_cache(maxsize=4)
def _schedule(season_yr: int) -> tuple:
    """(week, home, away, spread_line, total_line) rows for one season.

    A tuple so lru_cache holds something immutable, and so a missing parquet is
    an empty result rather than an exception -- nothing here is worth taking
    down a scheduled task for.
    """
    try:
        import polars as pl
        df = pl.read_parquet(PARQUET)
    except Exception:
        return ()
    try:
        df = df.filter(pl.col("season") == int(season_yr))
        cols = ["week", "home_team", "away_team", "spread_line", "total_line"]
        return tuple(tuple(r) for r in df.select(cols).iter_rows())
    except Exception:
        return ()


_artifact_cache = {"mtime": None, "doc": None}


def _artifact() -> dict:
    """The lines artifact, re-read only when the file has changed.

    mtime-checked rather than lru_cached: implied_totals runs in tight loops
    (the streaming board, ROS, playoff odds), and a long-lived process -- the
    audit app, the responder -- must still see the pulse's refresh.
    """
    import json
    try:
        mtime = LINES.stat().st_mtime
    except OSError:
        return {}
    if _artifact_cache["mtime"] != mtime:
        try:
            doc = json.loads(LINES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _artifact_cache["doc"] or {}
        _artifact_cache.update(mtime=mtime, doc=doc)
    return _artifact_cache["doc"] or {}


def lines(season_yr: int) -> list[dict]:
    """Every game's line for a season: the artifact's rows, and the parquet's
    for any week the artifact has not been built for.

    Each row: week, home, away, spread (nflverse convention: positive = home
    favoured), total, plus source / status / provider / fetched_utc / details
    so a caller can say where its number came from.
    """
    doc = _artifact()
    rows, covered = [], set()
    if doc.get("season") == int(season_yr):
        for g in doc.get("games") or []:
            covered.add(int(g["week"]))
            rows.append({"week": int(g["week"]), "home": g["home_team"],
                         "away": g["away_team"], "spread": g.get("spread_line"),
                         "total": g.get("total_line"), "source": g.get("source"),
                         "status": g.get("status"), "provider": g.get("provider"),
                         "fetched_utc": g.get("fetched_utc"),
                         "details": g.get("details"), "game_id": g.get("game_id")})
    for wk, home, away, spread, total in _schedule(int(season_yr)):
        if wk in covered:
            continue
        rows.append({"week": wk, "home": RELOCATED.get(home, home),
                     "away": RELOCATED.get(away, away), "spread": spread,
                     "total": total, "source": "nflverse" if spread is not None else None,
                     "status": "no_artifact", "provider": None, "fetched_utc": None,
                     "details": None, "game_id": None})
    return rows


@lru_cache(maxsize=4)
def _kickoffs(season_yr: int) -> tuple:
    """(week, epoch_seconds) for every game, from nflverse's gameday + gametime.

    Sleeper's own schedule feed carries a DATE and no time, so it can say a game
    is on Sunday but not that it starts at 13:00 -- which is not enough to know
    whether a roster move is being made an hour before kickoff or a day before.
    nflverse stamps both, in US/Eastern by convention.
    """
    try:
        import polars as pl
        from datetime import datetime
        from zoneinfo import ZoneInfo
        df = pl.read_parquet(PARQUET).filter(pl.col("season") == int(season_yr))
        et = ZoneInfo("America/New_York")
        out = []
        for wk, day, tm in df.select(["week", "gameday", "gametime"]).iter_rows():
            if not day or not tm:
                continue
            try:
                dt = datetime.strptime(f"{day} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=et)
            except ValueError:
                continue
            out.append((int(wk), dt.timestamp()))
        return tuple(sorted(out))
    except Exception:
        return ()


def kickoffs(season_yr, week: int | None = None) -> tuple:
    """Kickoff instants as epoch seconds, ascending. One week, or the season.

    The public read of _kickoffs, so callers that want the times themselves
    rather than a countdown do not have to reach through the private memo.
    robo/prekick.py clusters these into the day's game slots; an empty result
    means the schedule could not be read, and every caller must treat that as
    unknown rather than as "no games".
    """
    rows = _kickoffs(int(season_yr))
    return tuple(t for w, t in rows if week is None or w == int(week))


def next_kickoff(season_yr, week: int, now: float | None = None) -> float | None:
    """Seconds until the next kickoff of `week`, or None if it cannot be read.

    None means UNKNOWN, and every caller must treat unknown as "too close to
    risk it" rather than "plenty of time" -- the whole point of the rule this
    feeds is to not make a long-horizon decision minutes before a game.
    """
    import time as _time
    now = now if now is not None else _time.time()
    ahead = [t for w, t in _kickoffs(int(season_yr)) if w >= week and t > now]
    if not ahead:
        return None
    return min(ahead) - now


def latest_kickoff(season_yr, week: int, now: float | None = None) -> float | None:
    """Seconds until the LAST kickoff of `week` (or future weeks), or None if unreadable."""
    import time as _time
    now = now if now is not None else _time.time()
    ahead = [t for w, t in _kickoffs(int(season_yr)) if w >= week and t > now]
    if not ahead:
        return None
    return max(ahead) - now


@lru_cache(maxsize=36)
def team_kickoffs(season_yr: int, week: int) -> dict[str, float]:
    """team_code -> epoch_seconds of kickoff for `week`, from nflverse.

    Teams on bye, or unlisted teams, are absent.
    Both home and away teams are mapped via team_code().
    """
    try:
        import polars as pl
        from datetime import datetime
        from zoneinfo import ZoneInfo
        df = pl.read_parquet(PARQUET).filter(
            (pl.col("season") == int(season_yr)) & (pl.col("week") == int(week))
        )
        et = ZoneInfo("America/New_York")
        out = {}
        for home, away, day, tm in df.select(["home_team", "away_team", "gameday", "gametime"]).iter_rows():
            if not day or not tm:
                continue
            try:
                dt = datetime.strptime(f"{day} {tm}", "%Y-%m-%d %H:%M").replace(tzinfo=et)
                ts = dt.timestamp()
                h, a = team_code(home), team_code(away)
                if h:
                    out[h] = ts
                if a:
                    out[a] = ts
            except ValueError:
                continue
        return out
    except Exception:
        return {}


def implied_totals(season_yr, week: int, record: dict | None = None) -> dict:
    """team -> {own, opp, opponent, home} for every PRICED game that week.

    A game with no posted line is absent, not zero. Callers must treat a missing
    team as "no line yet" and fall back to something that does not pretend to
    know the matchup.

    `record` keeps the POSTED LINE each implied total was derived from. An audit
    of a defence's number wants to see the spread and the total a book actually
    published, not just the halved figures -- those are two arithmetic steps
    removed from anything anyone could look up -- and which source and fetch
    it came from, since ESPN and the nflverse fallback can disagree.
    """
    out = {}
    for r in lines(int(season_yr)):
        spread, total = r["spread"], r["total"]
        if r["week"] != week or spread is None or total is None:
            continue
        h = total / 2 + spread / 2
        a = total / 2 - spread / 2
        home, away = r["home"], r["away"]
        out[home] = {"own": round(h, 2), "opp": round(a, 2),
                     "opponent": away, "home": True}
        out[away] = {"own": round(a, 2), "opp": round(h, 2),
                     "opponent": home, "home": False}
        if record is not None:
            line = {"spread_line": spread, "total_line": total,
                    "home": home, "away": away, "week": r["week"],
                    "source": r["source"], "status": r["status"],
                    "provider": r["provider"], "fetched_utc": r["fetched_utc"],
                    "details": r["details"]}
            record.setdefault("lines", {})[home] = line
            record["lines"][away] = line
    return out


def coverage(season_yr, week: int) -> tuple[int, int]:
    """(games priced, games scheduled) for one week."""
    games = [r for r in lines(int(season_yr)) if r["week"] == week]
    priced = [r for r in games if r["spread"] is not None and r["total"] is not None]
    return len(priced), len(games)


def horizon(season_yr, from_week: int, max_week: int = 18,
            min_coverage: float = MIN_COVERAGE) -> int:
    """The last week from `from_week` that is priced well enough to plan around.

    Stops at the FIRST underpriced week rather than skipping it: a plan with a
    hole in the middle reads as though week 9 were unreachable when really it is
    just unpriced, and a streaming plan is a sequence.
    """
    last = from_week - 1
    for w in range(from_week, max_week + 1):
        priced, games = coverage(season_yr, w)
        if not games or priced / games < min_coverage:
            break
        last = w
    return last


def report(season_yr, week: int | None = None) -> str:
    from robo import season as _season
    yr = int(season_yr)
    now = _season.current_week()
    if week:
        rec: dict = {}
        imp = implied_totals(yr, week, record=rec)
        priced, games = coverage(yr, week)
        meta = ((_artifact().get("weeks") or {}).get(str(week)) or {})
        src = (f"ESPN ({meta.get('provider')}) fetched {meta.get('fetched_utc')}, "
               f"{meta.get('fallbacks', 0)} fallback(s)" if meta else
               "nflverse schedules.parquet (no lines artifact for this week)")
        L = [f"IMPLIED TOTALS - {yr} week {week}  ({priced}/{games} games priced)",
             f"  lines: {src}", ""]
        if not imp:
            L.append("  no lines posted for this week yet")
            return "\n".join(L)
        L.append(f"  {'team':<6}{'own':>7}{'opp':>7}   {'opponent':<10}source")
        for t, d in sorted(imp.items(), key=lambda kv: kv[1]["opp"]):
            ln = (rec.get("lines") or {}).get(t) or {}
            L.append(f"  {t:<6}{d['own']:>7.1f}{d['opp']:>7.1f}   "
                     f"{('vs ' if d['home'] else '@ ') + d['opponent']:<10}"
                     f"{ln.get('source')}" + ("" if ln.get("status") in ("espn", None)
                                              else f" ({ln.get('status')})"))
        L.append("")
        L.append("  sorted by OPPONENT implied total -- the top of this list is "
                 "the defence streaming board")
        return "\n".join(L)

    L = [f"LINE COVERAGE - {yr}", "",
         f"  {'week':<6}{'priced':>8}{'games':>7}{'share':>8}"]
    for w in range(1, 19):
        priced, games = coverage(yr, w)
        if not games:
            continue
        flag = "" if games and priced / games >= MIN_COVERAGE else "   <- thin"
        L.append(f"  {w:<6}{priced:>8}{games:>7}{priced / games:>7.0%}{flag}")
    h = horizon(yr, now)
    L += ["", f"  current week {now}; usable lookahead runs through week {h} "
              f"({max(0, h - now + 1)} week(s))"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default=None)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--coverage", action="store_true")
    args = ap.parse_args()
    from robo import season as _season
    yr = args.season or _season.SEASON
    print(report(yr, None if args.coverage else args.week))


if __name__ == "__main__":
    main()
