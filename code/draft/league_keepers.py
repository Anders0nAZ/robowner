"""Model the whole league's keeper landscape, so draft sims know who's really available.

Three sources, in descending order of trust:

1. THE FILLED DRAFT BOARD (`board_keepers`) -- once the commissioner assigns
   keepers to their pick numbers, this is fact: it says both WHO is kept and
   WHICH pick it costs. It is also the only source that survives a team
   changing its mind, because the board is what the draft actually runs off.
2. The roster `keepers` field (`declared`) -- a declaration of intent. It goes
   stale (Sleeper has no set-timestamp) and teams leave it blank or wrong; on
   27 Aug 2026 it still had Jordan Love for roster 8 whose board pick is James
   Cook, and roster 7 was empty despite two keepers on the board.
3. Prediction (`predict`) -- for teams with nothing anywhere. Ranked by VORP
   retained over what the forfeited pick would fetch.

python -m robo.league_keepers
"""

import csv
import json

from robo import DATA, DRAFT_ID_2026, LEAGUE_ID_2026
from robo import sleeper_read as api
from robo.keeper import keeper_table

SNAPSHOT = DATA / "keepers_2026.json"

MAX_KEEPERS = 2
# Every keeper declared in 2024 and 2025 was a skill player — nobody in this
# league burns a pick to retain a kicker or defense, however good the math looks.
KEEPABLE_POS = {"QB", "RB", "WR", "TE"}
# Our own declaration is settled (see decision log #4), so don't predict it.
OUR_KEEPERS = {4: ["12507", "7569"]}  # Hampton, Nico Collins


TEAMS = 12


def _board() -> list[dict]:
    with (DATA / "board_2026.csv").open(encoding="utf-8") as f:
        return [{"player_id": r["player_id"], "name": r["name"],
                 "blend_rank": float(r["blend_rank"]), "vorp": float(r["vorp"])}
                for r in csv.DictReader(f)]


def _board_rank() -> dict[str, float]:
    return {r["player_id"]: r["blend_rank"] for r in _board()}


def _vorp_at_pick(board: list[dict]) -> callable:
    """VORP of the player you'd realistically get at a given pick number.

    Keeping a player costs you that round's pick, so the real question isn't
    'how many picks did I save' (which flatters fringe kickers and defenses)
    but 'how many points above replacement did I retain versus what that pick
    would have bought me'.
    """
    ordered = sorted(board, key=lambda r: r["blend_rank"])

    def at(pick: int) -> float:
        i = min(max(pick - 1, 0), len(ordered) - 1)
        return ordered[i]["vorp"]
    return at


def snapshot(draft_id: str = DRAFT_ID_2026) -> dict:
    """Freeze the pre-draft board to data/keepers_2026.json.

    Must run while the draft is still `pre_draft`: at that point EVERY pick on
    the board is a keeper assignment, which is the only reliable way to tell
    keepers from real picks later. Sleeper's own `is_keeper` flag is not it --
    3 of our 24 assignments (Smith-Njigba, Cook, Javonte Williams) came back
    with is_keeper null while being keepers exactly like the other 21.
    """
    d = api.draft(draft_id)
    if d.get("status") != "pre_draft":
        raise RuntimeError(f"draft status is {d.get('status')!r}; too late to snapshot")
    rows = [{"pick_no": p["pick_no"], "round": p["round"], "roster_id": p["roster_id"],
             "player_id": p["player_id"],
             "name": f'{p["metadata"]["first_name"]} {p["metadata"]["last_name"]}',
             "pos": p["metadata"].get("position")}
            for p in api.draft_picks(draft_id)]
    rows.sort(key=lambda r: r["pick_no"])
    out = {"draft_id": draft_id, "picks": rows}
    SNAPSHOT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def board_keepers(draft_id: str = DRAFT_ID_2026) -> list[dict]:
    """Keeper assignments from the draft board: [{pick_no, roster_id, player_id, ...}].

    Prefers the frozen snapshot; falls back to the live board while the draft
    has not started. Empty list means the board isn't filled yet.
    """
    if SNAPSHOT.exists():
        snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        if snap.get("draft_id") == draft_id:
            return snap["picks"]
    try:
        return sorted(snapshot(draft_id)["picks"], key=lambda r: r["pick_no"])
    except Exception:
        return []


def kept_ids(draft_id: str = DRAFT_ID_2026) -> set[str]:
    """Every player the league has already kept — unavailable in any real draft.

    They stay ON the board deliberately: bench.py has to look up a keeper to
    price a lottery ticket behind him (Isiah Pacheco is only interesting because
    Jahmyr Gibbs is kept). So they are excluded where players are SELECTED, not
    where they are ranked.
    """
    return {r["player_id"] for r in board_keepers(draft_id)}


def keeper_pick_numbers(draft_id: str = DRAFT_ID_2026) -> dict[int, list[int]]:
    """roster_id -> the overall pick numbers that team forfeits to keepers."""
    out: dict[int, list[int]] = {}
    for r in board_keepers(draft_id):
        out.setdefault(r["roster_id"], []).append(r["pick_no"])
    return {k: sorted(v) for k, v in out.items()}


def declared() -> dict[int, list[str]]:
    """roster_id -> declared keeper player_ids.

    Reads GraphQL, not REST: the public /rosters endpoint caches and lagged
    hours behind a live keeper declaration, which would silently feed the
    draft sim a stale board. Falls back to REST if the authed call fails.
    """
    try:
        from robo.sleeper_write import gql
        d = gql("rr", 'query rr { league_rosters(league_id: "%s") { roster_id keepers } }'
                % LEAGUE_ID_2026)
        return {r["roster_id"]: (r.get("keepers") or []) for r in d["league_rosters"]}
    except Exception:
        return {r["roster_id"]: (r.get("keepers") or [])
                for r in api.rosters(LEAGUE_ID_2026)}


def predict(roster_id: int, board: list[dict]) -> list[dict]:
    """Most likely keepers for a team that hasn't declared.

    Ranked by VORP retained over what the forfeited pick would fetch — the
    calculus a real owner uses. Nobody keeps a kicker to 'save' 3 rounds.
    """
    vorp = {r["player_id"]: r["vorp"] for r in board}
    at = _vorp_at_pick(board)
    rows = []
    for r in keeper_table(roster_id):
        if not r["eligible"] or r["player_id"] not in vorp or r["pos"] not in KEEPABLE_POS:
            continue
        first_pick_of_round = (r["cost_round"] - 1) * TEAMS + 1
        r["gain"] = round(vorp[r["player_id"]] - at(first_pick_of_round), 1)
        rows.append(r)
    rows.sort(key=lambda r: -r["gain"])
    return [r for r in rows if r["gain"] > 0][:MAX_KEEPERS]


def _prior_keepers() -> dict:
    """(season, roster_id) -> set of kept player_ids, from history.db."""
    import sqlite3
    out = {}
    with sqlite3.connect(DATA / "history.db") as c:
        for season, rid, pid in c.execute(
                "SELECT season, roster_id, player_id FROM picks WHERE is_keeper=1"):
            out.setdefault((season, rid), set()).add(pid)
    return out


def audit_declared(kept: list[str], rid: int, prior: dict) -> list[str]:
    """Reasons a declaration looks stale/illegal. Empty list = looks fresh.

    Sleeper has no set-timestamp on keepers, and in prior years teams showed
    last season's keepers simply because they hadn't touched anything yet. A
    declaration identical to the team's 2025 set is suspicious; one containing
    a player kept in BOTH prior seasons (third consecutive keep) is invalid.
    """
    issues = []
    k = set(kept)
    k25 = prior.get(("2025", rid), set())
    k24 = prior.get(("2024", rid), set())
    if k and k == k25:
        issues.append("identical to 2025 keeper set (possible stale carryover)")
    inel = k & k25 & k24
    if inel:
        issues.append(f"ineligible third-year keep: {sorted(inel)}")
    return issues


def landscape() -> dict:
    """Who is off the board, and how confident we are about each team.

    The filled draft board wins outright where it exists: it is what the draft
    will actually run off, so a roster `keepers` field that disagrees is just
    an un-updated declaration, not a competing claim. Those disagreements are
    reported under "contradicted" rather than silently dropped -- a player the
    board frees but a roster still claims is exactly the case that puts a real
    asset back in our pool (Jordan Love, 27 Aug 2026), and the Draft Czar
    should see it, not just the model.
    """
    board = _board()
    dec = declared()
    players = api.players()
    prior = _prior_keepers()
    from_board: dict[int, list[str]] = {}
    for r in board_keepers():
        from_board.setdefault(r["roster_id"], []).append(r["player_id"])

    out = {"board": {}, "declared": {}, "predicted": {}, "off_board": [],
           "suspect": {}, "contradicted": {}, "freed": []}
    for rid in sorted(set(dec) | set(from_board)):
        if rid in from_board:
            kept = from_board[rid]
            out["board"][rid] = kept
            # anything a roster still claims that the board does not honor is
            # back in the draft pool
            stale = [p for p in (dec.get(rid) or []) if p not in kept]
            if stale:
                out["contradicted"][rid] = stale
                out["freed"] += stale
            out["off_board"] += kept
            continue
        kept = (dec.get(rid) or []) or OUR_KEEPERS.get(rid, [])
        issues = audit_declared(kept, rid, prior) if kept else []
        if issues:
            # don't trust it: keep only the players who are individually
            # eligible, and note the problem for the Draft Czar
            k25, k24 = prior.get(("2025", rid), set()), prior.get(("2024", rid), set())
            kept = [p for p in kept if not (p in k25 and p in k24)]
            out["suspect"][rid] = issues
        if kept:
            out["declared"][rid] = kept
            out["off_board"] += kept
        else:
            picks = predict(rid, board)
            out["predicted"][rid] = [p["player_id"] for p in picks]
            out["off_board"] += [p["player_id"] for p in picks]
    out["off_board"] = sorted(set(out["off_board"]))
    out["freed"] = sorted(set(out["freed"]) - set(out["off_board"]))
    out["names"] = {pid: (players.get(pid, {}).get("full_name") or pid)
                    for pid in out["off_board"] + out["freed"]}
    return out


if __name__ == "__main__":
    ranks = _board_rank()
    land = landscape()
    kpicks = keeper_pick_numbers()
    users = {u["user_id"]: u["display_name"] for u in api.users(LEAGUE_ID_2026)}
    owner = {r["roster_id"]: users.get(r["owner_id"], "?") for r in api.rosters(LEAGUE_ID_2026)}

    def show(pid):
        return f"{land['names'].get(pid, pid)} (board {ranks.get(pid, '-')})"

    if land["board"]:
        print(f"FROM THE DRAFT BOARD ({len(land['board'])}/{TEAMS} teams assigned):")
        for rid, ks in land["board"].items():
            picks = ", ".join(f"#{n}" for n in kpicks.get(rid, []))
            names = ", ".join(show(k) for k in ks)
            print(f"  {owner.get(rid, '?'):<14} {names:<56} {picks}")
    if land["contradicted"]:
        print("\n!! ROSTER KEEPERS THE BOARD DOES NOT HONOR (stale declarations):")
        for rid, ks in land["contradicted"].items():
            print(f"  {owner.get(rid, '?'):<14} still claims {', '.join(show(k) for k in ks)}")
    if land["freed"]:
        print("\nBACK IN THE DRAFT POOL:")
        for p in sorted(land["freed"], key=lambda p: ranks.get(p, 999)):
            print(f"  {show(p)}")
    if land.get("suspect"):
        print("\n!! SUSPECT DECLARATIONS (flag for the Draft Czar):")
        for rid, issues in land["suspect"].items():
            print(f"   {owner.get(rid, '?')}: {'; '.join(issues)}")
    if land["declared"]:
        print("\nDECLARED (roster field only, not on the board):")
        for rid, ks in land["declared"].items():
            print(f"  {owner.get(rid, '?'):<14} {', '.join(show(k) for k in ks)}")
    if land["predicted"]:
        print("\nPREDICTED (nothing declared anywhere):")
        for rid, ks in land["predicted"].items():
            print(f"  {owner.get(rid, '?'):<14} {', '.join(show(k) for k in ks) or '(none worth keeping)'}")
    top = sorted(land["off_board"], key=lambda p: ranks.get(p, 999))[:15]
    print(f"\n{len(land['off_board'])} players off the board; most valuable:")
    for p in top:
        print(f"  board {ranks.get(p, '-'):>6}  {land['names'][p]}")
