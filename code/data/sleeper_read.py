"""Read-only Sleeper REST API client (official, no auth)."""

import json
import time
from pathlib import Path

import requests

from robo import RAW

BASE = "https://api.sleeper.app/v1"
_session = requests.Session()


def get(path: str):
    url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
    r = _session.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def league(league_id):        return get(f"league/{league_id}")
def rosters(league_id):       return get(f"league/{league_id}/rosters")
def users(league_id):         return get(f"league/{league_id}/users")
def drafts(league_id):        return get(f"league/{league_id}/drafts")
def draft(draft_id):          return get(f"draft/{draft_id}")
def draft_picks(draft_id):    return get(f"draft/{draft_id}/picks")
def matchups(league_id, wk):  return get(f"league/{league_id}/matchups/{wk}")
def transactions(league_id, wk): return get(f"league/{league_id}/transactions/{wk}")
def trending(kind="add", hours=24, limit=50):
    return get(f"players/nfl/trending/{kind}?lookback_hours={hours}&limit={limit}")
def nfl_state():              return get("state/nfl")


_PLAYERS_CACHE = RAW / "players_nfl.json"
_PLAYERS_MAX_AGE_H = 24

# HOW STALE A SLEEPER DESIGNATION MAY BE BEFORE A ROSTER DECISION READS IT.
# Sleeper enforces its own IR rule against its own `injury_status`, so that field
# is the authority on who may be reserved and who may not be started -- and it is
# the field this dump carries. At the default 24h it can be a full day behind:
# measured 18 Sep 2026, a 6.4-hour-old dump had Nico Collins at Questionable
# while Sleeper live said Out, and 89 players' designations had moved. He was
# IR-eligible with three empty reserve slots and the sweep could not see it.
#
# A FRESHNESS BOUND, NOT A TUNING KNOB. The dump is ~16MB, so this is the trade:
# every roster decision inside one window shares a single pull, and the first
# caller in that window pays it. Lower it and the bot reacts sooner at the cost
# of more pulls; the whole-dump fetch is why it is not simply always refreshed.
FRESH_STATUS_MAX_AGE_H = 0.5


def players_age_h() -> float | None:
    """How old the cached dump is, so staleness can be reported rather than
    silently acted on. None when nothing has been cached yet."""
    if not _PLAYERS_CACHE.exists():
        return None
    return (time.time() - _PLAYERS_CACHE.stat().st_mtime) / 3600.0


def players(refresh: bool = False, max_age_h: float | None = None) -> dict:
    """Full NFL player dump (~16 MB). Cached on disk, refreshed daily.

    `max_age_h` lets a caller that is about to ACT on a designation demand a
    fresher copy than the ordinary daily one -- see FRESH_STATUS_MAX_AGE_H. The
    cost is paid once per window however many callers ask, because the refresh
    rewrites the shared cache file.
    """
    limit = _PLAYERS_MAX_AGE_H if max_age_h is None else float(max_age_h)
    age = players_age_h()
    if refresh or age is None or age > limit:
        data = get("players/nfl")
        _PLAYERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _PLAYERS_CACHE.write_text(json.dumps(data), encoding="utf-8")
        return data
    return json.loads(_PLAYERS_CACHE.read_text(encoding="utf-8"))


def player_name(players_map: dict, pid: str) -> str:
    p = players_map.get(pid)
    if not p:
        return pid  # team DEF like "DEN"
    return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or pid
