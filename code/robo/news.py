"""NOT IN USE. Superseded by scout.py — nothing imports this module.

It was built to watch FantasyPros' free news API, but that tier hard-caps at the
ten most recent items league-wide and ignores limit/offset/page, so it can never
supply news about a specific player. scout.py uses Sleeper's authed feed instead,
which carries RotoWire and RotoBaller items per player.

Kept only as a manual spot-check of the FantasyPros wire. IT DOES NOT RUN ON A
SCHEDULE and nothing calls it. That matters because selfdoc reads this docstring
into the bot's self-description: while this said "a poll every ~45 min", the bot
told the league it polls FantasyPros every 45 minutes, which was never true.

python -m robo.news            # one manual poll, print items
"""

import json

import requests

from robo import DATA, LEAGUE_ID_2026, ROBOWNER_USER_ID
from robo import sleeper_read as api
from robo.fantasypros import _api_key
from robo.keeper import norm

NEWS_URL = "https://api.fantasypros.com/public/v2/json/nfl/news"
STATE = DATA / "news_seen.json"


def fetch_news(limit: int = 10) -> list[dict]:
    r = requests.get(NEWS_URL, params={"limit": limit},
                     headers={"x-api-key": _api_key()}, timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])


def our_player_names() -> set[str]:
    rosters = api.rosters(LEAGUE_ID_2026)
    mine = next((r for r in rosters if r.get("owner_id") == ROBOWNER_USER_ID), None)
    if not mine or not mine.get("players"):
        return set()
    players_map = api.players()
    return {norm(api.player_name(players_map, pid)) for pid in mine["players"]}


def poll(mark_seen: bool = True) -> list[dict]:
    """Return unseen news items, most recent first, tagged with relevance."""
    seen = set(json.loads(STATE.read_text()) ) if STATE.exists() else set()
    ours = our_player_names()
    fresh = []
    items = fetch_news()
    for it in items:
        nid = it.get("id")
        if nid in seen:
            continue
        title = it.get("title") or ""
        player = norm((it.get("player_name") or title.split("(")[0]).strip())
        it["about_our_player"] = any(player and player in n or n in player for n in ours) if ours else False
        fresh.append(it)
    if mark_seen and fresh:
        seen.update(it["id"] for it in fresh)
        STATE.write_text(json.dumps(sorted(seen)[-500:]))
    return fresh


if __name__ == "__main__":
    for it in poll(mark_seen=False):
        flag = " <== OUR PLAYER" if it["about_our_player"] else ""
        print(f"[{it.get('created')}] {it.get('title')}{flag}")
