"""Live FFC 2QB ADP — the market model for draft-day survival odds.

Distinct from robo.adp (the LOCKED PDF snapshot): the lock is the keeper-cost
source of truth and must never move; this feed is refreshed daily because
"will he survive to our next pick" needs the market as it is NOW, not as it
was at lock. Includes per-player stdev, which drives the survival model.

python -m robo.adp_live          # fetch + show sample
"""

import json

import requests

from robo import DATA
from robo.keeper import norm, DEF_CITY

URL = "https://fantasyfootballcalculator.com/api/v1/adp/2qb?teams=12&year=2026"
OUT = DATA / "adp_live.json"


def fetch() -> dict:
    r = requests.get(URL, timeout=30)
    r.raise_for_status()
    d = r.json()
    players = d.get("players", [])
    if len(players) < 100:
        raise ValueError(f"suspiciously few players ({len(players)}); keeping old file")
    out = {
        "meta": d.get("meta"),
        "players": [{"name": p["name"], "pos": p["position"], "team": p.get("team"),
                     "adp": p["adp"], "stdev": p.get("stdev"), "bye": p.get("bye")}
                    for p in players],
    }
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def load() -> dict:
    """(normalized name or DEF team code, pos) is too fragile across sources;
    key on normalized name alone with DEF mapped to team code, same convention
    as the locked-ADP join in rankings."""
    if not OUT.exists():
        return {}
    d = json.loads(OUT.read_text(encoding="utf-8"))
    idx = {}
    for p in d.get("players", []):
        key = DEF_CITY.get(p["name"].lower()) if p["pos"] == "DEF" else norm(p["name"])
        if key:
            idx[key] = p
    return idx


if __name__ == "__main__":
    out = fetch()
    print(f"{len(out['players'])} players, drafts through {out['meta'].get('end_date')}")
    for p in out["players"][:5]:
        print(f"  {p['name']:<22} adp={p['adp']:>6} stdev={p['stdev']}")
