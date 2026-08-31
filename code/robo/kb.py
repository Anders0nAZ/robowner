"""Build the league knowledge base: data/league_kb.json + LEAGUE.md.

Combines live Sleeper pulls (data/raw/), the constitution, and the 2026
lottery result into one reference the agents load for context.
"""

import json
from datetime import datetime, timezone

from robo import (
    DATA, RAW, LEAGUE_ID_2026, LEAGUE_ID_2025, LEAGUE_ID_2024,
    DRAFT_ID_2026, ROBOWNER_USER_ID, ROSTER_ID,
)
from robo import sleeper_read as api
from robo.keeper import keeper_table

# 2026 draft lottery result, seed 8291997 (see constitution/lottery-name-mapping.md).
# Slot = order drawn; each non-top-3 finisher may take first-or-last available
# position instead, so final draft_order in Sleeper may differ.
LOTTERY_2026 = [
    "YaBoyMickle", "QBPrincesss", "architina", "lotsyrk", "rpsulli",
    "MorrieDuckett", "SinfonianPoke", "devilz13s", "JCGlock", "Miller5123",
    "anders0nAZ", "ChrisNote",
]

CONSTITUTION_FACTS = {
    "governance": "Supreme Chancellor: Bob (dictator authority). Rule Czar Emeritus: Ryan. Draft Czar Emeritus: Nate.",
    "fee": "$100/owner; pot $1200: 1st $520, 2nd $250, 3rd $100, season high $50, weekly high $20x14",
    "keepers": "max 2; cost round = ceil(avg(prev season overall pick, locked FFC 2QB ADP)/12); waiver adds count as pick 204; a player kept both prior seasons is ineligible; keeper rights not tradeable",
    "draft": "17-round snake, Sun Aug 30 2026 3:00 PM Phoenix; keepers slotted manually by Draft Czar; K/DEF convention: draft exactly 1 of each",
    "lottery": "drawn slot owners (non-top-3 finishers) may take first OR last available position",
    "waivers": "FAAB $100, 1-day clear, ties by inverse-standings waiver order; free agents first-come after clearing",
    "trades": "instant, no veto, deadline Tue after week 11; FAAB and future picks tradeable; collusion fined 1st-place prize",
    "ir": "3 IR slots; only Out / Suspended / COVID eligible",
    "season": "weeks 1-14 regular, 3 divisions randomized yearly, 6-team playoff weeks 15-17, consolation ladder sets lottery weights",
    "conduct": "no tanking, no dropping high-ranked players intentionally, lineup must be competitive",
}


def build() -> dict:
    league = api.league(LEAGUE_ID_2026)
    users = api.users(LEAGUE_ID_2026)
    draft = api.draft(DRAFT_ID_2026)
    players = api.players()
    prev_rosters = json.loads((RAW / "prev_rosters.json").read_text(encoding="utf-8"))
    prev_users = {u["user_id"]: u["display_name"]
                  for u in json.loads((RAW / "prev_users.json").read_text(encoding="utf-8"))}

    rosters_2025 = []
    for r in sorted(prev_rosters, key=lambda x: x["roster_id"]):
        rosters_2025.append({
            "roster_id": r["roster_id"],
            "owner": prev_users.get(r["owner_id"]),
            "record": f"{r['settings'].get('wins')}-{r['settings'].get('losses')}",
            "fpts": r["settings"].get("fpts"),
            "players": {pid: api.player_name(players, pid) for pid in (r.get("players") or [])},
        })

    kb = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "league_ids": {"2026": LEAGUE_ID_2026, "2025": LEAGUE_ID_2025, "2024": LEAGUE_ID_2024},
        "draft_id_2026": DRAFT_ID_2026,
        "robowner_user_id": ROBOWNER_USER_ID,
        "our_franchise": {
            "taken_over_from": "SinfonianPoke",
            "team_name_2025": "Morris' Mafia",
            "roster_id_2025": ROSTER_ID,
            "record_2025": "8-6 (5th overall)",
            "lottery_slot": 7,
        },
        "league_settings": league["settings"],
        "scoring_settings": league["scoring_settings"],
        "roster_positions": league["roster_positions"],
        "divisions": {k: v for k, v in (league.get("metadata") or {}).items() if k.startswith("division")},
        "members_2026": [
            {"display_name": u["display_name"], "user_id": u["user_id"],
             "team_name": (u.get("metadata") or {}).get("team_name")}
            for u in users
        ],
        "draft_2026": {
            "status": draft["status"],
            "start_time": draft["start_time"],
            "settings": draft["settings"],
            "draft_order": draft.get("draft_order"),
        },
        "lottery_2026": {"seed": 8291997, "order_drawn": LOTTERY_2026},
        "rosters_2025_final": rosters_2025,
        "keeper_table_ours": keeper_table(),
        "constitution": CONSTITUTION_FACTS,
    }
    return kb


def write_md(kb: dict) -> str:
    f = kb["our_franchise"]
    lines = [
        "# RURFFL League Knowledge Base",
        f"\n*Generated {kb['generated']} — regenerate with `python -m robo.kb`*\n",
        "## Our franchise",
        f"- Robowner (user_id {kb['robowner_user_id']}) took over **{f['team_name_2025']}** "
        f"({f['taken_over_from']}), {f['record_2025']}, lottery slot {f['lottery_slot']}.",
        "\n## Constitution quick facts",
    ]
    lines += [f"- **{k}**: {v}" for k, v in kb["constitution"].items()]
    lines.append("\n## Keeper cost table (our roster)")
    lines.append("| Player | Pos | Prev pick | ADP | Cost round | Kept '25 | Eligible |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in kb["keeper_table_ours"]:
        lines.append(
            f"| {r['name']} | {r['pos']} | {r['prev_pick']} | {r['adp'] or '—'} "
            f"| R{r['cost_round']}{'~' if r['adp_estimated'] else ''} "
            f"| {'Y' if r['kept_2025'] else ''} | {'Y' if r['eligible'] else 'NO'} |"
        )
    lines.append("\n## 2026 members")
    for m in kb["members_2026"]:
        lines.append(f"- {m['display_name']}" + (f" — {m['team_name']}" if m["team_name"] else ""))
    lines.append("\n## Lottery order drawn (seed 8291997)")
    for i, name in enumerate(kb["lottery_2026"]["order_drawn"], 1):
        us = " ← **Robowner**" if name == "SinfonianPoke" else ""
        lines.append(f"{i}. {name}{us}")
    lines.append("\n## 2025 final rosters")
    for r in kb["rosters_2025_final"]:
        lines.append(f"\n### {r['owner']} ({r['record']}, {r['fpts']} pts)")
        lines.append(", ".join(r["players"].values()))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    kb = build()
    DATA.mkdir(exist_ok=True)
    (DATA / "league_kb.json").write_text(json.dumps(kb, indent=1), encoding="utf-8")
    from robo import ROOT
    (ROOT / "LEAGUE.md").write_text(write_md(kb), encoding="utf-8")
    print("wrote data/league_kb.json and LEAGUE.md")
