"""FantasyPros half-PPR superflex expert consensus rankings (ECR).

Two paths, same output file:
- Official API (preferred): needs FANTASYPROS_API_KEY in .env (free tier).
- Fallback: the public cheatsheet page embeds `ecrData` JSON; fetch() + parse().
"""

import json
import os
import re

import requests

from robo import DATA, RAW, ROOT

URL = "https://www.fantasypros.com/nfl/rankings/half-point-ppr-superflex-cheatsheets.php"
HTML = RAW / "fp_superflex_half.html"
OUT = DATA / "ecr_superflex_half.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


API_URL = "https://api.fantasypros.com/public/v2/json/nfl/2026/consensus-rankings"


def _api_key() -> str | None:
    key = os.environ.get("FANTASYPROS_API_KEY")
    if not key:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("FANTASYPROS_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    return key or None


def fetch_api_top10() -> list[str]:
    """Free-tier official API: returns only the top 10 (public_api_limited).
    Used purely as a cross-check on the scrape."""
    r = requests.get(
        API_URL,
        params={"type": "draft", "scoring": "HALF", "position": "OP"},
        headers={"x-api-key": _api_key()},
        timeout=30,
    )
    r.raise_for_status()
    return [p["player_name"] for p in r.json().get("players", [])]


def fetch() -> None:
    r = requests.get(URL, headers=UA, timeout=30)
    r.raise_for_status()
    HTML.write_text(r.text, encoding="utf-8")


def parse() -> dict:
    m = re.search(r"var ecrData = (\{.*?\});", HTML.read_text(encoding="utf-8", errors="replace"))
    if not m:
        raise ValueError("ecrData not found — page layout changed?")
    d = json.loads(m.group(1))
    if d.get("scoring") != "HALF":
        raise ValueError(f"expected HALF scoring page, got {d.get('scoring')}")
    out = {
        "updated": d.get("last_updated"),
        "experts": d.get("total_experts"),
        "players": [
            {
                "name": p["player_name"],
                "team": p.get("player_team_id"),
                "pos": p["player_position_id"],
                "ecr": p["rank_ecr"],
                "pos_rank": p.get("pos_rank"),
                "tier": p.get("tier"),
                "bye": p.get("player_bye_week"),
                "rank_min": p.get("rank_min"),
                "rank_max": p.get("rank_max"),
            }
            for p in d["players"]
        ],
    }
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def load() -> dict:
    return json.loads(OUT.read_text(encoding="utf-8"))


def refresh() -> dict:
    """Scrape is primary (free API tier caps at 10 players). When a key is
    configured, the API top-10 cross-checks the scrape for silent breakage."""
    fetch()
    out = parse()
    if _api_key():
        try:
            api_top = fetch_api_top10()
            scrape_top = [p["name"] for p in out["players"][:10]]
            if api_top != scrape_top:
                print(f"WARNING: scrape/API top-10 drift!\n  api:    {api_top}\n  scrape: {scrape_top}")
            else:
                print("scrape verified against API top-10")
        except Exception as e:
            print(f"FP API cross-check skipped ({e})")
    return out


if __name__ == "__main__":
    out = refresh()
    print(f"[{out.get('source','scrape')}] {len(out['players'])} players, "
          f"{out['experts']} experts, updated {out['updated']}")
