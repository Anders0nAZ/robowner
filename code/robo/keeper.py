"""Keeper eligibility + draft-cost formula (constitution §5).

Cost round = round containing avg(previous season's overall draft pick,
locked FFC 2QB ADP). Players acquired on waivers (undrafted in the previous
season's draft) count as the last pick drafted (pick 204 in a 17x12 draft).
A player kept in BOTH of the previous two seasons is ineligible (league
precedent allows two consecutive keeps, bars the third).
"""

import json
import math
import re

from robo import RAW, ROSTER_ID
from robo import adp as adp_mod

TEAMS = 12
ROUNDS = 17
LAST_PICK = TEAMS * ROUNDS  # 204

_SUFFIXES = re.compile(r"\s+(jr|sr|ii|iii|iv|v)\.?$", re.I)

# FFC name -> Sleeper name quirks
ALIASES = {
    "marquise brown": "hollywood brown",
}

# Sleeper team-DEF ids ("DEN") vs FFC "Denver Defense"
DEF_CITY = {
    "denver defense": "DEN", "pittsburgh defense": "PIT", "minnesota defense": "MIN",
    "la rams defense": "LAR", "new england defense": "NE", "detroit defense": "DET",
    "jacksonville defense": "JAX", "baltimore defense": "BAL", "buffalo defense": "BUF",
    "cleveland defense": "CLE", "new orleans defense": "NO",
}


def norm(name: str) -> str:
    n = re.sub(r"[.'’-]", "", _SUFFIXES.sub("", name.strip())).lower()
    return ALIASES.get(n, n)


def _load(fn):
    return json.loads((RAW / fn).read_text(encoding="utf-8"))


def cost_round(prev_pick: int, adp: float) -> int:
    return math.ceil(((prev_pick + adp) / 2) / TEAMS)


def keeper_table(roster_id: int = ROSTER_ID) -> list[dict]:
    """Cost table for every player on a team's end-2025 roster (ours by default)."""
    players = _load("players_nfl.json")
    prev_rosters = _load("prev_rosters.json")
    picks25 = {p["player_id"]: p for p in _load("prev_picks.json")}
    picks24 = {p["player_id"]: p for p in _load("picks2024.json")}
    adp_rows = adp_mod.load()
    adp_by_name = {}
    for r in adp_rows:
        key = DEF_CITY.get(r["name"].lower(), None) if r["pos"] == "DEF" else norm(r["name"])
        adp_by_name[key] = r

    roster = next(r for r in prev_rosters if r["roster_id"] == roster_id)
    out = []
    for pid in roster["players"]:
        p = players.get(pid, {})
        name = p.get("full_name") or pid
        pos = p.get("position") or "DEF"
        pk25 = picks25.get(pid)
        prev_pick = pk25["pick_no"] if pk25 else LAST_PICK
        kept25 = bool(pk25 and pk25.get("is_keeper"))
        kept24 = bool(picks24.get(pid, {}).get("is_keeper"))
        adp_row = adp_by_name.get(pid if pos == "DEF" else norm(name))
        adp_val = adp_row["adp"] if adp_row else None
        eligible = not (kept25 and kept24)
        row = {
            "player_id": pid,
            "name": name,
            "pos": pos,
            "team": p.get("team"),
            "prev_pick": prev_pick,
            "via_waiver": pk25 is None,
            "kept_2025": kept25,
            "kept_2024": kept24,
            "eligible": eligible,
            "adp": adp_val,
            "adp_rank": adp_row["rank"] if adp_row else None,
            # no ADP listing = went undrafted in current mocks; treat like last pick
            "cost_round": cost_round(prev_pick, adp_val if adp_val is not None else LAST_PICK),
            "adp_estimated": adp_val is None,
            # value = how many picks later the cost round starts vs the player's ADP;
            # positive means we keep him cheaper than the market drafts him
            "surplus": None,
        }
        if adp_val is not None:
            first_pick_of_cost_round = (row["cost_round"] - 1) * TEAMS + 1
            row["surplus"] = round(first_pick_of_cost_round - adp_val, 1)
        out.append(row)
    out.sort(key=lambda r: (r["surplus"] is None, -(r["surplus"] or 0)))
    return out


if __name__ == "__main__":
    for r in keeper_table():
        tag = "" if r["eligible"] else "  INELIGIBLE"
        kept = " (kept '25)" if r["kept_2025"] else ""
        est = " ~" if r["adp_estimated"] else ""
        print(
            f"{r['name']:<24} {r['pos']:<3} prev_pick={r['prev_pick']:>3} "
            f"adp={r['adp'] if r['adp'] is not None else '---':>6} -> round {r['cost_round']:>2}{est} "
            f"surplus={r['surplus'] if r['surplus'] is not None else '---':>6}{kept}{tag}"
        )
