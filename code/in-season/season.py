"""Live in-season league state -- what is true right now, not what the board froze.

Everything the in-season modules share, and nothing they don't: who is on which
roster this minute, who is genuinely available, what a player projects for in a
given week, whether his game has kicked off yet, who may legally go on IR, and
how many roster slots and how much FAAB are left.

WHAT IS DELIBERATELY NOT HERE: rest-of-season value. What a player is worth from
week n forward is a model, it has not been designed yet, and a placeholder for it
sitting in the module every consumer imports is exactly how a placeholder quietly
becomes the engine. It lives behind robo/value.py's gate instead, and moves.py
cannot submit anything while that gate is shut.

The league's own settings are the source of truth for shape (roster size, IR
slots, which designations IR accepts, FAAB budget). The constants below are the
DECLARED values so they are visible and settable; audit() compares them to
Sleeper and reports drift rather than letting the two disagree in silence.

python -m robo.season             # what the league looks like right now
python -m robo.season --week 5    # add that week's projections and byes
python -m robo.season --audit     # our declared shape vs Sleeper's
"""

import argparse
import json
import time
from datetime import datetime, timezone

from robo import LEAGUE_ID_2026, ROBOWNER_USER_ID, settings
from robo import sleeper_read as api
from robo.rankings import custom_points

SEASON = "2026"

# ---- league shape (declared; audit() checks these against Sleeper) ----
ROSTER_MAX = 17          # 10 starting slots + 7 bench
IR_SLOTS = 3             # reserve slots, ON TOP of ROSTER_MAX
SEASON_WEEKS = 18
FAAB_BUDGET = 100
WAIVER_CLEAR_DAYS = 1    # how long a dropped player sits on waivers

# A game is only movable while it has not started. The league sets bench_lock=1,
# so a player freezes at his own kickoff -- not at some league-wide deadline.
# Sleeper's schedule feed carries the game's status directly, which is far more
# honest than deriving kickoff times: it says "pre_game" until the ball is in
# the air and we never have to reason about time zones to know.
MOVABLE_GAME_STATUS = "pre_game"

# Which injury designations this league lets us park on reserve. Read from the
# league's own reserve_allow_* flags rather than hardcoded, because they are
# per-league and ours says Out/Sus/COV yes, Doubtful/NA/DNR no. Note this is a
# DIFFERENT question from draft_agent's BAD_STATUS, which asks "is he
# undraftable" and counts Doubtful. A Doubtful player is a bad start and an
# illegal IR stash at the same time; conflating the two lists would produce a
# rejected write every Sunday.
_RESERVE_FLAG = {
    "reserve_allow_out": "Out",
    "reserve_allow_doubtful": "Doubtful",
    "reserve_allow_sus": "Sus",
    "reserve_allow_cov": "COV",
    "reserve_allow_na": "NA",
    "reserve_allow_dnr": "DNR",
}
# A player already carrying a league-designated reserve tag is always eligible;
# that is what the tag means.
ALWAYS_RESERVE = ("IR", "PUP")

settings.apply(__name__, globals())

_cache: dict = {}
_CACHE_TTL = 120


def _memo(key, fn, ttl: int = _CACHE_TTL):
    """Tiny in-process TTL cache. These modules run as short-lived scheduled
    tasks, so this exists to stop one run fetching the same 3,300-row
    projection feed four times, not to survive between runs."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- league shape

def league(league_id: str = LEAGUE_ID_2026) -> dict:
    return _memo(("league", league_id), lambda: api.league(league_id), ttl=600)


def scoring(league_id: str = LEAGUE_ID_2026) -> dict:
    return league(league_id)["scoring_settings"]


def ir_statuses(league_id: str = LEAGUE_ID_2026) -> set[str]:
    """The injury designations this league accepts on reserve."""
    s = league(league_id)["settings"]
    out = set(ALWAYS_RESERVE)
    for flag, status in _RESERVE_FLAG.items():
        if s.get(flag):
            out.add(status)
    return out


def current_week() -> int:
    """Sleeper's own idea of the week. Clamped to the regular season, because
    display_week keeps climbing past week 18 and every weekly endpoint stops."""
    st = _memo("nfl_state", api.nfl_state, ttl=600)
    return max(1, min(SEASON_WEEKS, int(st.get("week") or 1)))


def audit(league_id: str = LEAGUE_ID_2026) -> list[str]:
    """Our declared shape vs the league's. Reports drift; never 'fixes' it.

    A silent disagreement here is the expensive kind: if the league adds a
    bench slot and ROSTER_MAX still says 17, every add looks illegal and the
    bot simply stops making moves without ever erroring.
    """
    s = league(league_id)["settings"]
    rp = league(league_id)["roster_positions"]
    out = []
    checks = [
        ("ROSTER_MAX", ROSTER_MAX, len(rp)),
        ("IR_SLOTS", IR_SLOTS, s.get("reserve_slots")),
        ("FAAB_BUDGET", FAAB_BUDGET, s.get("waiver_budget")),
        ("WAIVER_CLEAR_DAYS", WAIVER_CLEAR_DAYS, s.get("waiver_clear_days")),
    ]
    for name, declared, actual in checks:
        if actual is not None and declared != actual:
            out.append(f"{name}: we say {declared}, Sleeper says {actual}")
    if s.get("waiver_type") != 2:
        out.append(f"waiver_type is {s.get('waiver_type')}, not 2 (FAAB) - "
                   "bid logic assumes FAAB")
    return out


# -------------------------------------------------------------- live rosters

def live_rosters(league_id: str = LEAGUE_ID_2026) -> list[dict]:
    """Uncached roster truth, via GraphQL. See sleeper_write.live_rosters."""
    from robo.sleeper_write import live_rosters as _lr
    return _memo(("rosters", league_id), lambda: _lr(league_id), ttl=30)


def mine(league_id: str = LEAGUE_ID_2026) -> dict:
    """Our roster row. Matched on owner_id, not a hardcoded roster_id, so a
    league re-seed cannot silently point us at somebody else's team."""
    rs = live_rosters(league_id)
    for r in rs:
        if r.get("owner_id") == ROBOWNER_USER_ID:
            return r
    raise RuntimeError(f"no roster owned by {ROBOWNER_USER_ID} in {league_id}")


def rostered_ids(league_id: str = LEAGUE_ID_2026) -> set[str]:
    """Every player held by anyone -- the complement of the free-agent pool."""
    out: set[str] = set()
    for r in live_rosters(league_id):
        out |= set(r.get("players") or [])
    return out


def free_agents(board: list[dict], league_id: str = LEAGUE_ID_2026) -> list[dict]:
    """Board rows for players nobody holds.

    Kept as board rows rather than bare ids because every consumer immediately
    needs pos/team/projection, and re-joining them player-by-player is how the
    same lookup ends up written three different ways.
    """
    held = rostered_ids(league_id)
    return [r for r in board if r["player_id"] not in held]


def slots(league_id: str = LEAGUE_ID_2026) -> dict:
    """Roster and IR occupancy. `open` is what an add actually needs."""
    r = mine(league_id)
    players = r.get("players") or []
    reserve = r.get("reserve") or []
    # Sleeper counts reserve players inside `players` as well, so the active
    # count is the difference. Counting len(players) alone would report a full
    # roster the moment we used IR, which is the exact opposite of the truth.
    active = [p for p in players if p not in reserve]
    return {
        "active": len(active), "roster_max": ROSTER_MAX,
        "open": max(0, ROSTER_MAX - len(active)),
        "ir_used": len(reserve), "ir_slots": IR_SLOTS,
        "ir_open": max(0, IR_SLOTS - len(reserve)),
    }


def faab_left(league_id: str = LEAGUE_ID_2026) -> int:
    used = (mine(league_id).get("settings") or {}).get("waiver_budget_used") or 0
    return max(0, FAAB_BUDGET - int(used))


# --------------------------------------------------------------- weekly points

def schedule(season: str = SEASON) -> list[dict]:
    return _memo(("sched", season),
                 lambda: api.get(f"https://api.sleeper.app/schedule/nfl/regular/{season}"),
                 ttl=600)


def game_status(season: str = SEASON) -> dict[str, str]:
    """game_id -> status ('pre_game' | 'in_game' | 'complete' | 'canceled')."""
    return {g["game_id"]: g["status"] for g in schedule(season) if g.get("game_id")}


def weekly_raw(week: int, season: str = SEASON) -> list[dict]:
    url = (f"https://api.sleeper.app/projections/nfl/{season}/{week}"
           "?season_type=regular&position[]=QB&position[]=RB&position[]=WR"
           "&position[]=TE&position[]=K&position[]=DEF")
    return _memo(("wk", season, week), lambda: api.get(url), ttl=600)


def week_points(week: int, season: str = SEASON,
                league_id: str = LEAGUE_ID_2026) -> dict[str, dict]:
    """player_id -> {pts, has_game, locked, opponent, game_id, date}.

    HAS_GAME COMES FROM game_id, NOT bool(stats). A player on bye still gets a
    projection row -- it just carries a one-key stats blob and game_id None. The
    old `has_game = bool(stats)` test therefore called every bye player active.
    It happened to bench them anyway, because a bye scores 0 and sorts last, but
    it was benching them by accident rather than by rule and the [BYE] flag it
    was supposed to print never once printed.

    `locked` is the game's own status, so we never have to reason about kickoff
    times or time zones to know whether a player can still be moved.
    """
    sc = scoring(league_id)
    gs = game_status(season)
    out = {}
    for row in weekly_raw(week, season):
        gid = row.get("game_id")
        stats = row.get("stats") or {}
        out[row["player_id"]] = {
            "pts": custom_points(stats, sc) if gid else 0.0,
            "has_game": bool(gid) and gs.get(gid) != "canceled",
            "locked": bool(gid) and gs.get(gid, MOVABLE_GAME_STATUS) != MOVABLE_GAME_STATUS,
            "opponent": row.get("opponent"),
            "game_id": gid,
            "date": row.get("date"),
        }
    return out


def bye_teams(week: int, season: str = SEASON) -> set[str]:
    playing = {t for g in schedule(season) if g["week"] == week
               for t in (g.get("home"), g.get("away")) if t}
    allt = {t for g in schedule(season) for t in (g.get("home"), g.get("away")) if t}
    return allt - playing


# ------------------------------------------------------------------------- IR

def ir_eligible(pid: str, players: dict | None = None,
                league_id: str = LEAGUE_ID_2026) -> bool:
    """May this player legally sit on reserve in THIS league?"""
    players = players if players is not None else api.players()
    st = (players.get(pid) or {}).get("injury_status") or ""
    return st in ir_statuses(league_id)


# -------------------------------------------------------------------- waivers

def on_waivers(league_id: str = LEAGUE_ID_2026) -> set[str]:
    """Players currently sitting on waivers rather than free for the taking.

    A dropped player is unclaimable for WAIVER_CLEAR_DAYS, then becomes an
    ordinary free agent. Getting this partition right is the whole reason the
    bot will not spend FAAB on somebody it could have had for nothing -- and in
    2025 this league ran 306 free-agent adds against 93 waiver wins, so the
    free side is where most of the volume actually is.

    Derived from the transactions feed, which is the only place a drop time is
    recorded. Read across the current and previous week because a Sunday drop
    is still on waivers on Monday, and the feed is bucketed by week.
    """
    wk = current_week()
    cutoff = time.time() - WAIVER_CLEAR_DAYS * 86400
    out: set[str] = set()
    for w in {max(1, wk - 1), wk}:
        try:
            tx = api.transactions(league_id, w)
        except Exception:
            continue
        for t in tx:
            if t.get("status") != "complete":
                continue
            ts = (t.get("status_updated") or t.get("created") or 0) / 1000
            if ts < cutoff:
                continue
            out |= set((t.get("drops") or {}).keys())
    # Anything already picked back up is not on waivers any more.
    return out - rostered_ids(league_id)


# ------------------------------------------------------------------------ cli

def summary(week: int | None = None) -> str:
    wk = week or current_week()
    sl = slots()
    pl = api.players()
    r = mine()
    reserve = set(r.get("reserve") or [])
    wp = week_points(wk)
    L = [f"RURFFL 2026 - week {wk}",
         f"  roster   {sl['active']}/{sl['roster_max']} active"
         f"  ({sl['open']} open), IR {sl['ir_used']}/{sl['ir_slots']}",
         f"  FAAB     {faab_left()} of {FAAB_BUDGET} left",
         f"  waivers  {len(on_waivers())} player(s) currently unclaimable",
         f"  IR takes {', '.join(sorted(ir_statuses()))}",
         ""]
    drift = audit()
    if drift:
        L.append("  !! league shape drift: " + "; ".join(drift))
        L.append("")
    L.append(f"  {'player':<24} {'pos':<4} {'st':<5} {'pts':>6}  note")
    rows = [(pid, pl.get(pid) or {}, wp.get(pid) or {}) for pid in (r.get("players") or [])]
    rows.sort(key=lambda t: -(t[2].get("pts") or 0))
    for pid, p, w in rows:
        note = []
        if pid in reserve:
            note.append("ON IR")
        if not w.get("has_game", True):
            note.append("BYE")
        if w.get("locked"):
            note.append("locked")
        st = (p.get("injury_status") or "")
        if st:
            note.append("IR-ok" if st in ir_statuses() else "IR-no")
        L.append(f"  {api.player_name(pl, pid)[:24]:<24} {p.get('position') or 'DEF':<4} "
                 f"{st[:5]:<5} {w.get('pts', 0):>6.1f}  {', '.join(note)}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.audit:
        drift = audit()
        print("\n".join(drift) if drift else "league shape matches Sleeper")
        return
    if args.json:
        print(json.dumps({"week": current_week(), "slots": slots(),
                          "faab_left": faab_left(),
                          "ir_statuses": sorted(ir_statuses()),
                          "on_waivers": sorted(on_waivers()),
                          "drift": audit()}, indent=1))
        return
    print(summary(args.week))


if __name__ == "__main__":
    main()
