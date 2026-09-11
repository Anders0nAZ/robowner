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


def players(refresh: bool = False) -> dict:
    """Full NFL player dump (~5 MB). Cached on disk, refreshed daily."""
    stale = (
        not _PLAYERS_CACHE.exists()
        or time.time() - _PLAYERS_CACHE.stat().st_mtime > _PLAYERS_MAX_AGE_H * 3600
    )
    if refresh or stale:
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
