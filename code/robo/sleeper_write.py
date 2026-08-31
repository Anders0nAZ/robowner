"""Authenticated Sleeper GraphQL client (unofficial write API).

Uses the Robowner account token from .env (SLEEPER_TOKEN; JWT, ~1yr expiry).
Mutation signatures were pulled from the server's own GraphQL introspection
(snake_case/Absinthe) on 2026-08-24, and the critical path was verified
end-to-end in a throwaway mock draft: create_draft -> update_draft_status ->
draft_pick_player -> REST-verified -> update_draft_queue -> delete_draft.

Still unverified live: submit_waiver_claim's k_settings/v_settings ARRAY ENCODING.
The bid KEY itself is no longer a guess -- "waiver_bid" was read straight off 93
completed 2025 waiver transactions in this league (30 Aug 2026), so only the
question of whether the parallel-array form reaches Sleeper's settings blob
intact is open. A mis-encoded bid reads as 0, which in FAAB loses to any positive
bid: we lose a player, not the budget.

NOTE: every write here should be paired with a robo.decisions.record() call
by the caller — league rule: all Robowner actions are publicly logged.
"""

import json
import os
from pathlib import Path

import requests

from robo import ROOT, LEAGUE_ID_2026, ROBOWNER_USER_ID

GRAPHQL = "https://sleeper.com/graphql"


def _token() -> str:
    tok = os.environ.get("SLEEPER_TOKEN")
    if not tok:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("SLEEPER_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
    if not tok:
        raise RuntimeError("SLEEPER_TOKEN not set (capture it per plan Phase 0 step 4)")
    return tok


def gql(operation: str, query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        GRAPHQL,
        json={"operationName": operation, "variables": variables or {}, "query": query},
        headers={"Authorization": _token(), "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"graphql errors: {body['errors']}")
    return body["data"]


def whoami() -> str:
    """Smoke test: returns the token's user_id."""
    data = gql("initialize_app", "query initialize_app { me { user_id display_name } }")
    return data["me"]


def live_rosters(league_id: str = LEAGUE_ID_2026) -> list[dict]:
    """Every roster as it stands RIGHT NOW. Read this, never REST /rosters.

    REST caches for hours. That already lagged a live keeper declaration and
    silently poisoned the draft board once; in-season the same staleness would
    have us claim a player another team picked up this morning, or drop someone
    we no longer hold. This is a query, not a mutation -- it is here rather than
    in sleeper_read.py only because the authed client lives here.
    """
    q = ('query league_rosters { league_rosters(league_id: "%s") { '
         'roster_id owner_id players starters reserve taxi settings } }' % league_id)
    return gql("league_rosters", q)["league_rosters"]


def set_starters(roster_id: int, week: int, starters: list[str],
                 league_id: str = LEAGUE_ID_2026) -> None:
    q = f"""
    mutation update_matchup_leg($starters_games: Map) {{
        update_matchup_leg(league_id: "{league_id}", roster_id: {roster_id},
            leg: {week}, round: {week}, starters: {json.dumps(starters)},
            starters_games: $starters_games) {{ league_id }}
    }}"""
    gql("update_matchup_leg", q)


def set_reserve(roster_id: int, reserve: list[str],
                league_id: str = LEAGUE_ID_2026) -> None:
    q = f"""
    mutation roster_update_reserve {{
        roster_update_reserve(league_id: "{league_id}", roster_id: {roster_id},
            reserve: {json.dumps(reserve)}) {{ league_id }}
    }}"""
    gql("roster_update_reserve", q)


def free_agent_transaction(adds: dict[str, int] | None, drops: dict[str, int] | None,
                           league_id: str = LEAGUE_ID_2026) -> dict:
    """adds/drops: {player_id: roster_id}. Free-agent (post-clear) moves."""
    q = f"""
    mutation league_create_transaction($k_adds: [String], $v_adds: [Int],
                                       $k_drops: [String], $v_drops: [Int]) {{
        league_create_transaction(league_id: "{league_id}", type: "free_agent",
            k_adds: $k_adds, v_adds: $v_adds, k_drops: $k_drops, v_drops: $v_drops) {{
            transaction_id status type adds drops
        }}
    }}"""
    v = {
        "k_adds": list((adds or {}).keys()), "v_adds": list((adds or {}).values()),
        "k_drops": list((drops or {}).keys()), "v_drops": list((drops or {}).values()),
    }
    return gql("league_create_transaction", q, v)


def create_league_mock(pick_timer: int = 30, league_id: str = LEAGUE_ID_2026) -> dict:
    """Create a throwaway league mock: our real settings and keeper board, no league.

    Sleeper's "league mock" clones a league's slots, scoring and KEEPER PICKS into
    a standalone draft whose top-level league_id is null -- so picks in it cannot
    reach the real draft. metadata.type = "league_mock" plus metadata.league_id is
    what triggers the clone; without them you get an empty generic draft.

    The signature was lost once already. It was recovered on 30 Aug 2026 by
    probing validation errors, because Sleeper's GraphQL introspection now
    returns 500: sending a mutation with no arguments makes the validator name
    every required one, and sending an unknown argument gets "Unknown argument"
    while a real one gets through to execution. A malformed mutation cannot
    create anything, which is what makes that safe to do.

    The creator gets their own real draft slot -- Robowner lands at 6 -- and CPU
    fills the other eleven. Single-seat: only the creating account can pick, so
    the bot must create its own if the bot is the one being tested.

    NOT started by this call. update_draft_status(draft_id, sport, status) takes
    those three arguments and then 500s on every status value tried, so starting
    it is still a click in the app. Returns {"draft_id", "status"}.
    """
    ks = ["teams", "rounds", "pick_timer", "cpu_autopick", "enforce_position_limits",
          "slots_qb", "slots_rb", "slots_wr", "slots_te", "slots_flex",
          "slots_super_flex", "slots_k", "slots_def", "slots_bn",
          "reversal_round", "player_type", "alpha_sort", "autostart",
          "autopause_enabled", "nomination_timer"]
    vs = [12, 17, pick_timer, 1, 1, 1, 2, 2, 1, 1, 1, 1, 1, 7, 0, 0, 0, 0, 0, 60]
    km = ["league_id", "type", "scoring_type", "mock_traded_picks", "show_team_names"]
    vm = [league_id, "league_mock", "2qb", "on", "0"]
    q = """mutation create_draft($ks: [String], $vs: [Int], $km: [String], $vm: [String]) {
        create_draft(sport: "nfl", season: "2026", season_type: "regular", type: "snake",
                     k_settings: $ks, v_settings: $vs,
                     k_metadata: $km, v_metadata: $vm) { draft_id status }
    }"""
    return gql("create_draft", q,
               {"ks": ks, "vs": vs, "km": km, "vm": vm})["create_draft"]


def draft_pick(draft_id: str, player_id: str, pick_no: int) -> dict:
    """Make a live draft pick. VERIFIED in mock draft 2026-08-24."""
    q = f"""
    mutation draft_pick_player {{
        draft_pick_player(draft_id: "{draft_id}", sport: "nfl",
            player_id: "{player_id}", pick_no: {pick_no}) {{
            pick_no player_id picked_by
        }}
    }}"""
    return gql("draft_pick_player", q)["draft_pick_player"]


def set_draft_queue(draft_id: str, player_ids: list[str]) -> list[str]:
    """Set our autodraft/queue list (fallback if the agent dies). VERIFIED."""
    q = f"""
    mutation update_draft_queue($player_ids: [String]) {{
        update_draft_queue(draft_id: "{draft_id}", player_ids: $player_ids)
    }}"""
    return gql("update_draft_queue", q, {"player_ids": player_ids})["update_draft_queue"]


def submit_waiver_claim(adds: dict[str, int], drops: dict[str, int], bid: int,
                        league_id: str = LEAGUE_ID_2026) -> dict:
    """FAAB waiver claim. Signature from introspection; bid key assumed
    'waiver_bid' — exercise once against a real waiver before trusting."""
    q = f"""
    mutation submit_waiver_claim($k_adds: [String], $v_adds: [Int],
                                 $k_drops: [String], $v_drops: [Int],
                                 $k_settings: [String], $v_settings: [Int]) {{
        submit_waiver_claim(league_id: "{league_id}",
            k_adds: $k_adds, v_adds: $v_adds, k_drops: $k_drops, v_drops: $v_drops,
            k_settings: $k_settings, v_settings: $v_settings) {{
            transaction_id status type settings
        }}
    }}"""
    v = {
        "k_adds": list(adds.keys()), "v_adds": list(adds.values()),
        "k_drops": list(drops.keys()), "v_drops": list(drops.values()),
        "k_settings": ["waiver_bid"], "v_settings": [bid],
    }
    return gql("submit_waiver_claim", q, v)


if __name__ == "__main__":
    print(whoami())


def set_team_name(name: str, league_id: str = LEAGUE_ID_2026,
                  user_id: str = ROBOWNER_USER_ID) -> dict:
    """Set our display team name in a league.

    The name lives in the league_user metadata blob alongside notification
    prefs, and the mutation takes parallel key/value arrays. It is a REPLACE,
    not a merge -- sending only team_name would silently wipe allow_pn and
    mention_pn -- so the current metadata is read back from REST first and the
    new key merged into it.
    """
    from robo import sleeper_read as _read
    md = {}
    for u in _read.users(league_id):
        if u["user_id"] == user_id:
            md = dict(u.get("metadata") or {})
            break
    md["team_name"] = name
    keys, vals = list(md.keys()), [md[k] for k in md]
    q = """mutation update_league_user_metadata($k: [String], $v: [String]) {
        update_league_user_metadata(league_id: "%s", k_metadata: $k, v_metadata: $v) {
            user_id display_name metadata
        }
    }""" % league_id
    return gql("update_league_user_metadata", q,
               {"k": keys, "v": vals})["update_league_user_metadata"]
