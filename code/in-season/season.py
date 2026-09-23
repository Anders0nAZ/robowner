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
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.13 always has zoneinfo
    ZoneInfo = None

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
PHOENIX = (ZoneInfo("America/Phoenix") if ZoneInfo is not None
           else timezone(timedelta(hours=-7)))
WEEKLY_FALLBACK_HOUR = 3

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


def invalidate_live() -> None:
    """Drop short-lived league/schedule caches before a consequential write."""
    _cache.clear()


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


def monday_guard_active(now: float | None = None) -> bool:
    """Whether local operations are in the post-Sunday Monday guard window."""
    stamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(stamp, PHOENIX).weekday() == 0


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
    """Sleeper's weekly PROJECTIONS. Not results -- see week_points below."""
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

    `pts` IS A PROJECTION, NOT A RESULT, whatever the name suggests. weekly_raw
    reads /projections/, so this answers "what is he expected to score" even for
    a week that finished last Sunday. Actual scoring lives at
    stats/nfl/regular/<season>/<week> and nothing here reads it. The name has
    already cost one investigation two passes -- a comparison of "projected
    versus actual" built on this function compares a projection with itself and
    reports near-perfect agreement.
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
            "team": row.get("team") or (row.get("player") or {}).get("team"),
            "game_status": gs.get(gid) if gid else None,
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


# ------------------------------------------------------- transaction eligibility

def _timestamp(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n / 1000 if n > 10_000_000_000 else n
    try:
        text = str(value).strip()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return datetime.fromisoformat(text).replace(tzinfo=PHOENIX).timestamp()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _game_timestamp(row: dict, schedule_by_id: dict) -> float | None:
    ts = _timestamp(row.get("date"))
    if ts is not None:
        return ts
    game = schedule_by_id.get(row.get("game_id")) or {}
    for key in ("start_time", "date", "start", "kickoff"):
        ts = _timestamp(game.get(key))
        if ts is not None:
            return ts
    return None


def _weekly_fallback(game_at: float | None, now: float) -> float:
    """The first Wednesday 03:00 Phoenix after the DAY the relevant game was played.

    COMPUTED FROM THE GAME'S DAY, NOT ITS CLOCK, because the feed's `date` is a
    calendar date with no kickoff time -- it parses to midnight, which is
    several hours BEFORE the game. Comparing a 03:00 target against that
    midnight made a Wednesday-night opener settle at 03:00 the same morning, a
    time already in the past: every man who played in week 1's Wednesday opener
    read as an ordinary free agent from the moment he kicked off, eight days
    before this league's waivers would actually clear him. A Wednesday game's
    waivers run the FOLLOWING Wednesday, which is what `or 7` says.
    """
    base = datetime.fromtimestamp(game_at if game_at is not None else now, PHOENIX)
    day = base.replace(hour=0, minute=0, second=0, microsecond=0)
    days = (2 - day.weekday()) % 7 or 7
    return (day + timedelta(days=days)).replace(
        hour=WEEKLY_FALLBACK_HOUR).timestamp()


def _transaction_rows(league_id: str, week: int) -> list[dict]:
    rows = []
    for w in {max(1, week - 1), week}:
        try:
            rows.extend(api.transactions(league_id, w) or [])
        except Exception:
            continue
    return rows


def _eligibility_context(league_id: str = LEAGUE_ID_2026,
                         week: int | None = None,
                         now: float | None = None) -> dict:
    """Fetch the shared facts once for a whole transaction decision."""
    week = current_week() if week is None else int(week)
    now = time.time() if now is None else float(now)
    rows = _transaction_rows(league_id, week)
    cutoff = now - WAIVER_CLEAR_DAYS * 86400
    # KEYED BY WHEN HE WAS DROPPED, so the clear time can be stated rather than
    # implied. The latest drop wins: a man dropped twice clears from the second.
    recent_drops: dict[str, float] = {}
    settlements = []
    for tx in rows:
        if tx.get("status") != "complete":
            continue
        ts = _timestamp(tx.get("status_updated") or tx.get("created")) or 0
        if ts >= cutoff:
            for pid in (tx.get("drops") or {}):
                pid = str(pid)
                recent_drops[pid] = max(recent_drops.get(pid, 0.0), ts)
        if tx.get("type") == "waiver" and ts > 0:
            settlements.append(ts)
    schedule_by_id = {str(g.get("game_id")): g for g in schedule(SEASON)
                      if g.get("game_id")}
    by_week = {week: week_points(week, SEASON, league_id)}
    if week > 1:
        by_week[week - 1] = week_points(week - 1, SEASON, league_id)
    return {"league_id": league_id, "week": week, "now": now,
            "held": rostered_ids(league_id), "recent_drops": recent_drops,
            "settlements": settlements, "by_week": by_week,
            "schedule_by_id": schedule_by_id}


def transaction_eligibility(player_id: str,
                            league_id: str = LEAGUE_ID_2026,
                            *, context: dict | None = None,
                            week: int | None = None,
                            now: float | None = None) -> dict:
    """Return independent roster-movement and acquisition truth for one player.

    Roster movement follows the current NFL week and unlocks when Sleeper rolls
    forward. Acquisition follows the player's most recent locked game through
    that game's waiver settlement, so Tuesday's week rollover cannot turn a
    Sunday player into an ordinary free agent several hours too early.
    """
    pid = str(player_id)
    c = context or _eligibility_context(league_id, week, now)
    current = (c["by_week"].get(c["week"]) or {}).get(pid) or {}
    roster_locked = bool(current.get("locked"))
    roster_state = "roster_locked" if roster_locked else "movable"

    if pid in c["held"]:
        return {"player_id": pid, "roster_movement": roster_state,
                "acquisition": "unavailable", "week": c["week"],
                "game_id": current.get("game_id"),
                "reason": "already rostered", "unlock_at": None,
                "unlock_basis": None}

    if pid in c["recent_drops"]:
        # WHEN, not just "soon". The clear time is exactly computable from the
        # drop, and leaving it None made this the one waiver state a reader had
        # to reconstruct by hand -- which is how a claim on a man who had
        # already cleared read as a page bug rather than a stale run.
        dropped_at = c["recent_drops"].get(pid) if isinstance(c["recent_drops"], dict) else None
        return {"player_id": pid, "roster_movement": roster_state,
                "acquisition": "drop_waiver", "week": c["week"],
                "game_id": current.get("game_id"),
                "reason": f"dropped within the last {WAIVER_CLEAR_DAYS} day(s)",
                "unlock_at": (dropped_at + WAIVER_CLEAR_DAYS * 86400
                              if dropped_at else None),
                "unlock_basis": "drop-clear period"}

    locked = []
    for w, points in c["by_week"].items():
        row = points.get(pid) or {}
        if row.get("locked") and row.get("game_id"):
            at = _game_timestamp(row, c["schedule_by_id"])
            locked.append((at or 0, w, row))
    if locked:
        game_at, game_week, row = max(locked, key=lambda item: item[0])
        fallback = _weekly_fallback(game_at or None, c["now"])
        # A SETTLEMENT ONLY COUNTS IF WE KNOW IT CAME AFTER HIS GAME. With an
        # unreadable kickoff, `ts >= 0` is true of every claim this league has
        # ever processed, so any old waiver run would clear a man who kicked off
        # an hour ago. Unknown falls back to the calendar, which is late rather
        # than wrong.
        observed = [ts for ts in c["settlements"]
                    if game_at and ts >= game_at and ts <= c["now"]]
        unlock = min(observed) if observed else fallback
        basis = "observed completed waiver transaction" if observed else \
                "Wednesday 03:00 Phoenix fallback"
        if c["now"] < unlock:
            return {"player_id": pid, "roster_movement": roster_state,
                    "acquisition": "weekly_waiver", "week": game_week,
                    "game_id": row.get("game_id"),
                    "reason": "his NFL game has locked and weekly waivers have not settled",
                    "unlock_at": unlock, "unlock_basis": basis}

    return {"player_id": pid, "roster_movement": roster_state,
            "acquisition": "free_now", "week": c["week"],
            "game_id": current.get("game_id"),
            "reason": "unrostered and immediately acquirable",
            "unlock_at": None, "unlock_basis": None}


def transaction_states(player_ids,
                       league_id: str = LEAGUE_ID_2026,
                       *, week: int | None = None,
                       now: float | None = None) -> dict[str, dict]:
    """Classify many players against one internally consistent live snapshot."""
    c = _eligibility_context(league_id, week, now)
    return {str(pid): transaction_eligibility(str(pid), league_id, context=c)
            for pid in player_ids}


def next_waiver_run(now: float | None = None) -> float:
    """The next weekly waiver settlement at or after `now`, Phoenix.

    A DIFFERENT QUESTION FROM `_weekly_fallback`, which is asked of a GAME and
    therefore rolls a Wednesday kickoff to the FOLLOWING Wednesday. Asked of the
    clock, today's pending run is the answer: at 01:00 on a Wednesday the 03:00
    run has not happened yet, and `or 7` there would push it a week out.
    """
    stamp = time.time() if now is None else float(now)
    base = datetime.fromtimestamp(stamp, PHOENIX)
    target = (base.replace(hour=WEEKLY_FALLBACK_HOUR, minute=0, second=0,
                           microsecond=0)
              + timedelta(days=(2 - base.weekday()) % 7))
    if target.timestamp() < stamp:
        target += timedelta(days=7)
    return target.timestamp()


def _first_game_day(season: str = SEASON) -> dict[int, float]:
    """week -> the Phoenix midnight of that week's earliest scheduled game."""
    out: dict[int, float] = {}
    for g in schedule(season):
        w, at = g.get("week"), _timestamp(g.get("date"))
        if not w or at is None:
            continue
        w = int(w)
        out[w] = min(out[w], at) if w in out else at
    return out


def settlement_week(league_id: str = LEAGUE_ID_2026, *,
                    week: int | None = None, now: float | None = None,
                    settles_at: float | None = None) -> dict:
    """The first NFL week a claim submitted now could actually be played in.

    A CLAIM DOES NOT RESOLVE UNTIL THE WAIVER RUN, so every week that ends
    before that run is a week the claim cannot reach, and pricing one into the
    claim pays for points nobody can receive. Measured on Friday of week 2:
    every man on waivers is one of Thursday night's participants and his week-2
    game is already over, yet he still carried a week-2 projection.

    Compared on the game's DAY, never its clock, for the reason
    `_weekly_fallback` records -- the schedule's `date` is a calendar date that
    parses to midnight, hours before kickoff. A game on the settlement's own
    Wednesday IS reachable, because the run is at 03:00 and the ball is not.

    `settles_at` lets a caller that has already classified the pool pass the
    real unlock it read there, so an OBSERVED settlement beats the calendar.
    Unknown falls back to the calendar, which is late rather than wrong.
    """
    stamp = time.time() if now is None else float(now)
    current = current_week() if week is None else int(week)
    settles = float(settles_at) if settles_at else next_waiver_run(stamp)
    basis = ("unlock read from the live waiver pool" if settles_at
             else "next weekly waiver run (Wednesday 03:00 Phoenix)")
    day = (datetime.fromtimestamp(settles, PHOENIX)
           .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    first = _first_game_day()
    # AN UNREADABLE SCHEDULE MUST NOT SHORTEN THE HORIZON. Unlike the kickoff
    # blackout, where unknown counts as too close, unknown here has to mean
    # "price everything": silently deleting weeks is what makes a bad claim
    # look good, and that is the failure this function exists to stop.
    reachable = [w for w, at in first.items() if at >= day] if first else []
    return {"week": min(reachable) if reachable else current,
            "settles_at": settles, "basis": basis, "current_week": current}


# -------------------------------------------------------------------- waivers

def on_waivers(league_id: str = LEAGUE_ID_2026,
               player_ids=None) -> set[str]:
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
    if player_ids is None:
        wk = current_week()
        ids = set()
        for w in {max(1, wk - 1), wk}:
            ids |= set(week_points(w, SEASON, league_id))
        ids.update(_eligibility_context(league_id, wk)["recent_drops"])
        player_ids = ids
    states = transaction_states(player_ids, league_id)
    return {pid for pid, state in states.items()
            if state["acquisition"] in {"weekly_waiver", "drop_waiver"}}


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
