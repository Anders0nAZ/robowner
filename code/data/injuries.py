"""Who cannot play, and the earliest week the rules let him back.

WHY THIS EXISTS. `expected.py` needs to know when a man is allowed to play
again, and it was deriving that from Sleeper's weekly feed -- the first week
Sleeper projects him for anything. That is a forecast standing in for a rule,
and it is wrong for most of the men it matters to. James Conner went on IR on
30 August and cannot play until week 5; Sleeper projected him 2.7 points in
weeks 2, 3 and 4, so we priced him for three weeks he is barred from. The
docstring in returns.py says "IR IS A RULE, NOT A FORECAST" and the input was
defeating it.

ESPN publishes the rule. Its public injuries endpoint needs no key and no
account, carries all 32 teams, and 349 of its 800 rows include a structured
block:

    "status": "Injured Reserve",
    "date":   "2026-09-02T15:10Z",
    "details": {"fantasyStatus": {"abbreviation": "IR"}, "type": "Back",
                "returnDate": "2026-10-11"}

So this module reads four things nobody else here has: the eligibility FLOOR,
a TYPED body part (Sleeper's is free text), the SPELL START -- the `missed`
clock that returns.py had no source for -- and a SEASON-ENDING marker, which
arrives as a return date in the following calendar year.

WHAT IT DELIBERATELY DOES NOT DO IS FORECAST. For a man on injured reserve
`returnDate` is the earliest-eligible date restated, which is the same premise
Sleeper encodes, only encoded correctly. Whether he is actually back that week
is a judgment, it lives in the prose, and it belongs to scout.py. Reading this
file as a prediction would repeat the mistake it was written to fix.

JOINED BY ID, NEVER BY NAME. Every row carries an ESPN athlete id in its
player-card URL, and nflverse's ff_playerids crosses that to a Sleeper id for
6,239 players. This repo has already paid for name matching once: "Josh Allen"
is a quarterback and a linebacker.

NOTHING HERE RAISES. Every failure returns an empty map and a reason, the same
contract as robo/model_proj.py, because a scheduled roster job must degrade to
"use the feed" rather than die.

    python -m robo.injuries              # what it knows right now
    python -m robo.injuries --fetch      # pull a fresh copy
"""

import json
import re
import time
from datetime import datetime, timezone
from functools import lru_cache

from robo import DATA, roles, season, settings, vegas

FEED = ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries")
CACHE = DATA / "injuries_espn.json"

SCHEMA = 1

# How stale the cached feed may be before callers are told to ignore it. Sized
# to the daily refresh with room for one missed run: a designation two days old
# is still better than inferring the floor from a projection, but a week-old one
# is describing last week's roster.
MAX_AGE_H = 54.0

# Designations under which a man cannot play now, so his return date is a RULE
# and can be trusted as a floor. Everything outside this set -- Questionable
# above all -- gets a `returnDate` that is merely the date of his next game,
# which says nothing at all and must never be read as a forecast.
ABSENT = ("IR", "IR-R", "PUP-P", "PUP-R", "NFI-R",
          "RESERVE-SUS", "RESERVE-CEL", "OUT")

# Absences that are not injuries: a suspension and the commissioner exempt list.
# They have a date and no body part, and returns.py must honour the date without
# fitting a hamstring curve to a legal matter.
NON_INJURY = ("RESERVE-SUS", "RESERVE-CEL")

settings.apply(__name__, globals())


# ------------------------------------------------------------------ the fetch

def _espn_id(athlete: dict) -> str | None:
    """The athlete id, out of the player-card URL that always carries it."""
    for link in (athlete.get("links") or []):
        m = re.search(r"/id/(\d+)/", link.get("href") or "")
        if m:
            return m.group(1)
    return None


def _designation(item: dict) -> str:
    """One uppercase token for what the league says his status is.

    `details.fantasyStatus` is the richest of the three status fields ESPN
    ships -- it separates IR from IR-R and names the commissioner exempt list --
    but it is absent on every healthy row, so the coarse item status is the
    fallback rather than the other way round.
    """
    fs = ((item.get("details") or {}).get("fantasyStatus") or {}).get("abbreviation")
    if fs:
        return str(fs).upper()
    return str(item.get("status") or "").upper()


def fetch(timeout: int = 30) -> tuple[dict, str]:
    """Pull the feed and reshape it to sleeper_id -> row. ({}, why) on failure.

    VALIDATED BEFORE IT IS WRITTEN, the rule refresh.py already applies to every
    other source: a feed that came back short keeps the old file rather than
    replacing a good copy with a bad one. There is no way to tell a genuinely
    quiet Tuesday from a half-delivered response after the fact.
    """
    import requests
    try:
        r = requests.get(FEED, timeout=timeout)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        return {}, f"ESPN injuries feed unreachable: {str(e)[:100]}"
    teams = raw.get("injuries")
    if not isinstance(teams, list) or len(teams) < 30:
        return {}, f"ESPN returned {len(teams or [])} teams, expected 32"

    xw = roles._by_espn()
    rows, unmatched = {}, 0
    for t in teams:
        for item in (t.get("injuries") or []):
            a = item.get("athlete") or {}
            eid = _espn_id(a)
            pid = xw.get(eid) if eid else None
            if not pid:
                unmatched += 1
                continue
            det = item.get("details") or {}
            rows[pid] = {
                "name": a.get("displayName"),
                "team": t.get("abbreviation") or t.get("displayName"),
                "designation": _designation(item),
                "espn_status": item.get("status"),
                "body_part": det.get("type"),
                "return_date": det.get("returnDate"),
                # When ESPN last wrote about him. It dates the ITEM, not the
                # injury, so it is a lower bound on how long he has been out --
                # which is the honest direction for a clock that feeds a
                # survival curve conditioned on time already served.
                "as_of": item.get("date"),
                "short": item.get("shortComment"),
                "long": item.get("longComment"),
            }
    if not rows:
        return {}, "ESPN feed joined to no Sleeper ids at all"
    out = {"schema": SCHEMA, "generated_utc": datetime.now(timezone.utc).isoformat(),
           "season": season.SEASON, "teams": len(teams),
           "unmatched": unmatched, "players": rows}
    CACHE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    _cached.cache_clear()
    return out, ""


# ------------------------------------------------------------------- the read

@lru_cache(maxsize=1)
def _cached() -> tuple[dict, str]:
    if not CACHE.exists():
        return {}, f"no ESPN injury cache at {CACHE.name}"
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception as e:
        return {}, f"ESPN injury cache unreadable: {str(e)[:80]}"
    if d.get("schema") != SCHEMA:
        return {}, f"ESPN injury cache schema {d.get('schema')}, expected {SCHEMA}"
    try:
        t = datetime.fromisoformat(d["generated_utc"])
        age = (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return {}, "ESPN injury cache has no readable generated_utc"
    if age > MAX_AGE_H:
        return {}, f"ESPN injury cache is {age:.0f}h old (limit {MAX_AGE_H:.0f}h)"
    return d, ""


def load() -> tuple[dict, str]:
    """(player_id -> row, reason). ({}, why) whenever it must not be used."""
    d, why = _cached()
    return (d.get("players") or {}, why) if d else ({}, why)


def row(pid: str) -> dict:
    return (load()[0]).get(str(pid)) or {}


# ------------------------------------------------------------------ the weeks

@lru_cache(maxsize=4)
def _week_ends(season_yr: int) -> tuple:
    """(week, last gameday) per week, ascending. Empty when unreadable."""
    try:
        import polars as pl
        df = pl.read_parquet(vegas.PARQUET).filter(pl.col("season") == season_yr)
        ends: dict = {}
        for wk, day in df.select(["week", "gameday"]).iter_rows():
            d = str(day)
            if wk is not None and (wk not in ends or d > ends[wk]):
                ends[wk] = d
        return tuple(sorted(ends.items()))
    except Exception:
        return ()


def week_of(date_str: str | None, season_yr=None) -> int | None:
    """The NFL week a calendar date falls in, or None if it is past the season.

    None is the season-ending answer and is load-bearing: ESPN writes a
    torn-ACL return as a date in the FOLLOWING calendar year, and a caller that
    silently mapped that to week 1 would turn "out for the year" into "available
    immediately".
    """
    if not date_str:
        return None
    ends = _week_ends(int(season_yr or season.SEASON))
    for wk, last in ends:
        if str(date_str)[:10] <= last:
            return wk
    return None


# ---------------------------------------------------------------- the answers

def designation(pid: str) -> str | None:
    return row(pid).get("designation") or None


def absent(pid: str) -> bool:
    """Is he under a designation that forbids him playing right now?"""
    return (designation(pid) or "") in ABSENT


def out_for_season(pid: str) -> bool:
    """A return date past the last week of the season."""
    r = row(pid)
    return bool(r.get("return_date")) and absent(pid) and week_of(r["return_date"]) is None


def floor_week(pid: str, record: dict | None = None) -> int | None:
    """The first week the rules allow him to play, or None if unknown.

    Only ever answered for a man who is actually barred from playing. ESPN gives
    a `returnDate` for Questionable players too, and it is just the date of
    their next game -- Mahomes reads 2026-09-14 -- so honouring it there would
    invent an absence out of a routine practice report.
    """
    r = row(pid)
    if not r or not r.get("return_date") or not absent(pid):
        if record is not None:
            record.update({"floor": None, "why": "no absence on file at ESPN"
                           if not absent(pid) else "no return date published"})
        return None
    wk = week_of(r["return_date"])
    if record is not None:
        record.update({"floor": wk, "return_date": r["return_date"],
                       "designation": r.get("designation"), "as_of": r.get("as_of"),
                       "why": ("out for the season" if wk is None else
                               f"{r.get('designation')}, eligible {r['return_date']}")})
    return wk


def body_part(pid: str) -> str | None:
    """The injury, normalised onto the vocabulary returns.py fitted on.

    ESPN qualifies a diagnosis where nflverse's injury report does not -- "Knee
    - ACL" against a plain "Knee" -- so the qualifier is dropped rather than
    handed to a curve that has never seen it and would fall through to the
    pooled fit. "Undisclosed" is not a body part and reads as no information,
    which is the same thing the pooled curve already says, honestly.
    """
    d = designation(pid) or ""
    if d in NON_INJURY:
        return None
    b = row(pid).get("body_part")
    if not b or b in ("Undisclosed", "Suspension", "Personal"):
        return None
    return str(b).split(" - ")[0].strip()


def since(pid: str, week: int, season_yr=None) -> int:
    """Weeks he has already been out, as of `week`. 0 when unknown.

    This is what conditions the survival curve, and the honest reading of
    `as_of` is a LOWER BOUND: it dates ESPN's last item about him, not the
    injury. Under-stating time served makes the curve mildly optimistic, which
    is the failure mode that leaves a number too high rather than a man
    written off -- and 0, the answer before this module existed, is the most
    optimistic value of all.
    """
    r = row(pid)
    if not r.get("as_of") or not absent(pid):
        return 0
    w0 = week_of(r["as_of"], season_yr)
    if w0 is None:
        return 0
    return max(0, int(week) - int(w0))


def prose(pid: str) -> list[dict]:
    """ESPN's own reporting on him, in the shape scout.py's corpus uses.

    Two items, not one: `shortComment` is the transaction and names the
    reporter who broke it, `longComment` is the analyst's read of what it means
    for his role. They answer different questions and the second is where a
    date beyond the eligibility floor actually appears.
    """
    r = row(pid)
    out = []
    for key, kind in (("short", "report"), ("long", "analysis")):
        if r.get(key):
            out.append({"source": f"ESPN ({kind})", "published": r.get("as_of"),
                        "title": r[key]})
    return out


# ---------------------------------------------------------------- reporting

def report(limit: int = 40) -> str:
    players, why = load()
    if not players:
        return f"ESPN injuries: NOT USED - {why}"
    d, _ = _cached()
    rows = [(pid, r) for pid, r in players.items() if (r.get("designation") or "") in ABSENT]
    rows.sort(key=lambda kv: (kv[1].get("return_date") or "", kv[1]["name"] or ""))
    L = [f"ESPN INJURIES - {len(players)} joined, {len(rows)} unable to play, "
         f"{d.get('unmatched')} unmatched",
         f"  pulled {d.get('generated_utc', '')[:19]}", "",
         f"  {'player':<24}{'desig':<12}{'part':<14}{'ret':<12}{'wk':>4}  since"]
    for pid, r in rows[:limit]:
        wk = floor_week(pid)
        L.append(f"  {(r['name'] or '')[:24]:<24}{(r.get('designation') or ''):<12}"
                 f"{(body_part(pid) or '-'):<14}{(r.get('return_date') or '-'):<12}"
                 f"{('OUT' if wk is None else wk):>4}"
                 f"  {since(pid, season.current_week())}")
    return "\n".join(L)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="ESPN injury designations and return dates")
    ap.add_argument("--fetch", action="store_true", help="pull a fresh copy first")
    ap.add_argument("--player", help="everything known about one player")
    a = ap.parse_args()
    if a.fetch:
        d, why = fetch()
        print(f"fetched {len(d.get('players', {}))} players"
              f" ({d.get('unmatched')} unmatched)" if d else f"FAILED: {why}")
    if a.player:
        from robo import sleeper_read as api
        players = api.players()
        hits = [p for p, v in players.items()
                if a.player.lower() in (v.get("full_name") or "").lower()]
        for pid in hits[:5]:
            rec: dict = {}
            floor_week(pid, record=rec)
            print(f"\n{players[pid].get('full_name')} ({pid})")
            print(json.dumps({**row(pid), "floor": rec}, indent=1))
        if not hits:
            print(f"no player matching {a.player!r}")
        return
    print(report())


if __name__ == "__main__":
    main()
