"""Roboner's live-data skills — the tools the chat bot can call to look things up.

Every function returns a compact, already-summarized string so tool results
stay small in the local model's context. Sources are all free:
  Sleeper REST      season/weekly stats, rosters, trending adds/drops
  Sleeper GraphQL   per-player news with analyst notes
  ESPN public API   team records, schedules, next opponent
  our own board     projections scored under THIS league's rules

python -m robo.skills <skill> [args...]    # manual test
"""

import json
import sys
from datetime import datetime, timezone
from functools import lru_cache

import requests

from robo import DATA, LEAGUE_ID_2026, LEAGUE_ID_2025
from robo import sleeper_read as api
from robo.keeper import norm

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
CURRENT_SEASON = "2026"
LAST_SEASON = "2025"


# ---------------------------------------------------------------- resolution

@lru_cache(maxsize=1)
def _players() -> dict:
    return api.players()


@lru_cache(maxsize=1)
def _name_index() -> dict:
    idx = {}
    for pid, p in _players().items():
        if not p.get("active") and p.get("position") not in ("DEF",):
            continue
        full = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        if full:
            idx.setdefault(norm(full), pid)
            last = p.get("last_name")
            if last:
                idx.setdefault(norm(f"{last} {p.get('position','')}"), pid)
    return idx


def resolve_player(name: str) -> tuple[str, dict] | None:
    """Fuzzy name -> (player_id, player). Handles 'nico', 'Collins', full names."""
    n = norm(name)
    idx = _name_index()
    if n in idx:
        pid = idx[n]
        return pid, _players()[pid]
    matches = [(k, v) for k, v in idx.items() if n in k]
    if not matches:
        parts = n.split()
        matches = [(k, v) for k, v in idx.items() if parts and parts[-1] in k.split()]
    if len(matches) >= 1:
        # prefer the most fantasy-relevant match (has a team, skill position)
        def score(item):
            p = _players()[item[1]]
            return (bool(p.get("team")), p.get("position") in ("QB", "RB", "WR", "TE"),
                    -(p.get("search_rank") or 9999))
        best = max(matches, key=score)
        return best[1], _players()[best[1]]
    return None


def _fmt_stats(s: dict, pos: str) -> str:
    """Human-readable stat line for a position."""
    g = lambda k: s.get(k, 0) or 0
    if pos == "QB":
        return (f"{g('pass_yd'):.0f} pass yds, {g('pass_td'):.0f} pass TD, {g('pass_int'):.0f} INT, "
                f"{g('rush_yd'):.0f} rush yds, {g('rush_td'):.0f} rush TD")
    if pos == "RB":
        return (f"{g('rush_att'):.0f} car, {g('rush_yd'):.0f} rush yds, {g('rush_td'):.0f} rush TD, "
                f"{g('rec'):.0f} rec, {g('rec_yd'):.0f} rec yds, {g('rec_td'):.0f} rec TD")
    if pos in ("WR", "TE"):
        return (f"{g('rec'):.0f} rec on {g('rec_tgt'):.0f} tgt, {g('rec_yd'):.0f} yds, "
                f"{g('rec_td'):.0f} TD")
    if pos == "K":
        return f"{g('fgm'):.0f} FG, {g('xpm'):.0f} XP"
    return f"{g('sack'):.0f} sk, {g('int'):.0f} INT, {g('def_td'):.0f} def TD"


# -------------------------------------------------------------------- skills

def player_stats(player: str, season: str = LAST_SEASON, week: int | None = None) -> str:
    """Real stats for a player — full season, or one week if week is given."""
    hit = resolve_player(player)
    if not hit:
        return f"No player found matching '{player}'."
    pid, p = hit
    pos = p.get("position") or "?"
    path = f"stats/nfl/regular/{season}" + (f"/{week}" if week else "")
    try:
        all_stats = api.get(path)
    except Exception as e:
        return f"Stats unavailable for {season}{f' week {week}' if week else ''} ({e})."
    s = all_stats.get(pid)
    if not s:
        return (f"{p.get('full_name')} has no recorded stats for {season}"
                f"{f' week {week}' if week else ''} (didn't play, or the season hasn't started).")
    when = f"{season} week {week}" if week else f"{season} season"
    line = _fmt_stats(s, pos)
    half = s.get("pts_half_ppr")
    extra = f" | {half:.1f} half-PPR pts" if half is not None else ""
    gp = s.get("gp")
    games = f" over {gp:.0f} games" if gp and not week else ""
    return f"{p.get('full_name')} ({pos}, {p.get('team') or 'FA'}) {when}{games}: {line}{extra}"


def player_news(player: str, limit: int = 3) -> str:
    """Recent news and analyst notes for a specific player."""
    hit = resolve_player(player)
    if not hit:
        return f"No player found matching '{player}'."
    pid, p = hit
    from robo.sleeper_write import gql
    q = (f'query gpn {{ get_player_news(sport:"nfl", player_id:"{pid}", limit:{min(limit,5)}) '
         '{ published source metadata } }')
    try:
        items = gql("gpn", q)["get_player_news"]
    except Exception as e:
        return f"News lookup failed ({e})."
    if not items:
        return f"No recent news for {p.get('full_name')}."
    out = [f"{p.get('full_name')} ({p.get('position')}, {p.get('team') or 'FA'}) — "
           f"injury status: {p.get('injury_status') or 'healthy'}"]
    for n in items:
        md = n.get("metadata") or {}
        when = datetime.fromtimestamp((n.get("published") or 0) / 1000, timezone.utc).strftime("%b %d")
        body = (md.get("analysis") or md.get("description") or "")[:220].replace("\n", " ")
        out.append(f"[{when}, {n.get('source')}] {md.get('title')}: {body}")
    return "\n".join(out)


def player_projection(player: str) -> str:
    """2026 projection + where our draft board ranks him."""
    import csv
    hit = resolve_player(player)
    if not hit:
        return f"No player found matching '{player}'."
    pid, p = hit
    board_path = DATA / "board_2026.csv"
    if board_path.exists():
        with board_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["player_id"] == pid:
                    return (f"{row['name']} ({row['pos']}, {row['team']}): season "
                            f"projection {row['proj_pts']} pts under our scoring, "
                            f"{row['pos']}{row['pos_rank']} at the position. Our blended "
                            f"board rank #{row['blend_rank']} (expert consensus "
                            f"#{row['ecr'] or 'NR'}, locked ADP "
                            f"{row['adp_ffc'] or 'undrafted'}). Bye week "
                            f"{row['bye'] or '?'}.") + _this_week(pid)
    return (f"{p.get('full_name')} isn't on our 2026 board (undrafted/irrelevant "
            f"in 2QB formats).") + _this_week(pid)


def _this_week(pid: str) -> str:
    """This week's projection for one player, and whether he even plays.

    The board number is a SEASON total frozen before week 1, which is the wrong
    answer to "what is he good for on Sunday" -- and says nothing about a bye.
    """
    try:
        from robo import season
        wk = season.current_week()
        w = season.week_points(wk).get(pid) or {}
    except Exception:
        return ""
    if not w:
        return ""
    if not w.get("has_game"):
        return f" Week {wk}: ON BYE, no game."
    opp = f" vs {w['opponent']}" if w.get("opponent") else ""
    lock = " (his game has already started)" if w.get("locked") else ""
    return f" Week {wk}: projected {w.get('pts', 0)} pts{opp}{lock}."


def compare_players(players: str) -> str:
    """Compare 2-4 players side by side. Pass comma-separated names."""
    names = [n.strip() for n in players.split(",") if n.strip()][:4]
    return "\n".join(player_projection(n) for n in names) if names else "Give me some names."


def team_info(team: str) -> str:
    """NFL team record, next game, and bye week."""
    try:
        r = requests.get(f"{ESPN}/teams/{team.strip()}", timeout=20)
        r.raise_for_status()
        t = r.json()["team"]
    except Exception:
        return f"No NFL team found for '{team}' (use an abbreviation like HOU, KC, LAR)."
    rec = (t.get("record", {}).get("items") or [{}])[0].get("summary", "?")
    nxt = (t.get("nextEvent") or [{}])[0]
    when = (nxt.get("date") or "")[:10]
    return (f"{t['displayName']}: {rec}. Next: {nxt.get('name', 'TBD')} {when}. "
            f"{t.get('standingSummary') or ''}").strip()


def trending_players(kind: str = "add") -> str:
    """Most added or dropped players across all Sleeper leagues (last 24h)."""
    kind = "drop" if "drop" in kind.lower() else "add"
    try:
        rows = api.trending(kind, hours=24, limit=8)
    except Exception as e:
        return f"Trending lookup failed ({e})."
    pl = _players()
    out = []
    for r in rows:
        p = pl.get(r["player_id"], {})
        out.append(f"{p.get('full_name') or r['player_id']} ({p.get('position')}, "
                   f"{p.get('team') or 'FA'}) — {r['count']:,}")
    return f"Most {kind}ed in the last 24h:\n" + "\n".join(out)


def league_standings() -> str:
    """Current standings in OUR league (falls back to last season pre-kickoff)."""
    for lid, label in ((LEAGUE_ID_2026, "2026"), (LEAGUE_ID_2025, "2025 final")):
        try:
            rosters = api.rosters(lid)
            users = {u["user_id"]: u["display_name"] for u in api.users(lid)}
        except Exception:
            continue
        rows = [r for r in rosters if (r["settings"].get("wins", 0) or r["settings"].get("losses", 0))]
        if not rows:
            continue
        rows.sort(key=lambda r: (-r["settings"].get("wins", 0), -r["settings"].get("fpts", 0)))
        return f"{label} standings:\n" + "\n".join(
            f"{i}. {users.get(r['owner_id'], '?')} {r['settings'].get('wins',0)}-"
            f"{r['settings'].get('losses',0)} ({r['settings'].get('fpts',0)} pts)"
            for i, r in enumerate(rows, 1))
    return "No standings yet — the season hasn't started."


def who_owns(player: str) -> str:
    """Which team in OUR league rosters a given player."""
    hit = resolve_player(player)
    if not hit:
        return f"No player found matching '{player}'."
    pid, p = hit
    for lid, label in ((LEAGUE_ID_2026, "this season"), (LEAGUE_ID_2025, "at the end of last season")):
        try:
            rosters = api.rosters(lid)
            users = {u["user_id"]: u["display_name"] for u in api.users(lid)}
        except Exception:
            continue
        for r in rosters:
            if pid in (r.get("players") or []):
                return f"{p.get('full_name')} is on {users.get(r['owner_id'], 'an unknown team')}'s roster ({label})."
        if any(r.get("players") for r in rosters):
            return f"{p.get('full_name')} is a free agent {label}."
    return f"Couldn't determine ownership of {p.get('full_name')}."


# ------------------------------------------------- institutional memory

def league_chat_history(query: str) -> str:
    """What league members have actually said, searched over league chat."""
    from robo import chat_memory
    return chat_memory.fmt(chat_memory.search(query, limit=6))


def league_records(topic: str = "champions") -> str:
    """All-time championships, scoring records, or FAAB spending."""
    from robo import lore
    t = (topic or "").lower()
    if "faab" in t or "waiver" in t or "bid" in t:
        return lore.biggest_faab()
    if any(k in t for k in ("record", "high", "low", "blowout", "close", "score")):
        return lore.record_book()
    return lore.champions()


def manager_history(manager: str) -> str:
    """A manager's all-time record, titles, and season-by-season results."""
    from robo import lore
    amb = lore.ambiguous(manager)
    if amb:
        return (f"'{manager}' is ambiguous — could be "
                + " or ".join(lore.name_of(lore.resolve_manager(h)) or h for h in amb)
                + ". Ask which one, or use their full name.")
    return lore.manager_profile(manager)


def my_franchise() -> str:
    """The history of the franchise Robowner inherited — its previous owners."""
    from robo import lore
    return lore.franchise_history(4)


def manager_drafts(manager: str, season: str = "") -> str:
    """What a manager has drafted and kept in past seasons."""
    from robo import lore
    return lore.draft_history(manager, season or "")


def head_to_head(manager_a: str, manager_b: str) -> str:
    """All-time record between two managers."""
    from robo import lore
    return lore.head_to_head(manager_a, manager_b)


def season_summary(season: str) -> str:
    """Final standings and playoff results for a past season."""
    from robo import lore
    return lore.season_summary(season)


def keeper_board(team: str = "") -> str:
    """Who every team is keeping in 2026, and at which pick.

    The bot kept telling the league it had no keeper sheet while holding a
    frozen copy of the filled draft board. It has one; this is it.
    """
    from robo.league_keepers import board_keepers
    from robo import sleeper_read as api, LEAGUE_ID_2026
    rows = board_keepers()
    if not rows:
        return "The draft board has no keepers assigned yet."
    users = {u["user_id"]: u["display_name"] for u in api.users(LEAGUE_ID_2026)}
    owner = {r["roster_id"]: users.get(r["owner_id"], "?")
             for r in api.rosters(LEAGUE_ID_2026)}
    by_team: dict[str, list[str]] = {}
    for r in sorted(rows, key=lambda x: x["pick_no"]):
        by_team.setdefault(owner.get(r["roster_id"], "?"), []).append(
            f"{r['name']} ({r['pos']}, pick {r['pick_no']})")
    if team:
        t = team.strip().lower()
        hit = [k for k in by_team if t in k.lower()]
        if not hit:
            return f"No team matching '{team}'. Teams: {', '.join(sorted(by_team))}."
        return "; ".join(f"{k} keeps {', '.join(by_team[k])}" for k in hit)
    return " | ".join(f"{k}: {', '.join(v)}" for k, v in sorted(by_team.items()))


def best_available(position: str = "", count: int = 10) -> str:
    """Best players actually available: our board minus every kept player, and
    minus anyone already drafted once the draft is live."""
    import csv
    from robo.league_keepers import kept_ids
    from robo import sleeper_read as api, DRAFT_ID_2026
    gone = set(kept_ids())
    try:
        gone |= {p["player_id"] for p in api.draft_picks(DRAFT_ID_2026)}
    except Exception:
        pass
    board_path = DATA / "board_2026.csv"
    if not board_path.exists():
        return "The draft board has not been built yet."
    rows = []
    with board_path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["player_id"] in gone:
                continue
            if position and r["pos"].lower() != position.strip().lower():
                continue
            rows.append(r)
    rows.sort(key=lambda r: float(r["blend_rank"]))
    n = max(1, min(int(count or 10), 25))
    out = ", ".join(f"{r['name']} ({r['pos']})" for r in rows[:n])
    what = f" at {position.upper()}" if position else ""
    return (f"Best available{what} with {len(gone)} players off the board "
            f"(keepers plus anyone already drafted): {out}")


def my_status() -> str:
    """The bot's own liveness: replies left this hour, uptime, data freshness.

    The persona forbids misstating what it can do, and "how much have you got
    left" is exactly that kind of question -- previously unanswerable, so it
    would either guess or deflect. Reads the same per-channel reply log the
    rate limiter enforces, so the number it quotes is the number that governs
    it, not an estimate of one.
    """
    import time
    from robo import chat_responder as cr
    L = []
    for ch in ("groupme", "sleeper", "draft"):
        try:
            log = cr._reply_log(ch)
            stamps = json.loads(log.read_text()) if log.exists() else []
        except Exception:
            stamps = []
        recent = [s for s in stamps if s > time.time() - 3600]
        left = max(0, cr.MAX_REPLIES_PER_HOUR - len(recent))
        last = max(stamps) if stamps else None
        L.append("  %s: %d of %d replies left this hour%s"
                 % (ch, left, cr.MAX_REPLIES_PER_HOUR,
                    f", last spoke {int((time.time() - last) / 60)}m ago" if last else
                    ", not spoken yet"))
    L.append("  at most %d replies per poll, and I poll every %ds, so I cannot "
             "empty the hour all at once" % (cr.MAX_REPLIES_PER_CYCLE, cr.POLL_SECS))
    try:
        from robo import status
        up = (status.responder() or {}).get("uptime")
        if up:
            # Seconds off the process table. Printing it raw gave "up for
            # 2868.3713808059692", which is not something to say out loud.
            h, rem = divmod(int(up), 3600)
            L.append("  up for %s%dm" % (f"{h}h " if h else "", rem // 60))
    except Exception:
        pass
    return "My reply allowance right now:\n" + "\n".join(L)


def _league_owners() -> dict:
    """roster_id -> display name, from the live roster read."""
    from robo import season
    users = {u["user_id"]: u["display_name"] for u in api.users(LEAGUE_ID_2026)}
    return {r["roster_id"]: users.get(r.get("owner_id"), "?")
            for r in season.live_rosters(LEAGUE_ID_2026)}


def team_roster(team: str = "", week: str = "") -> str:
    """Any team's roster AS IT STANDS, split into starters, bench and IR.

    Distinct from draft_results, and the difference grows all season: that says
    what a team drafted, this says what they hold. One waiver claim separates
    the two. Read over the authenticated API rather than the public one, which
    caches for hours -- long enough to describe a roster somebody has already
    changed.
    """
    from robo import lineup as lu, season
    wk = int(week) if str(week).strip().isdigit() else season.current_week()
    try:
        rosters = season.live_rosters(LEAGUE_ID_2026)
    except Exception as e:
        return f"Could not read rosters ({e})."
    owner = _league_owners()
    pl = _players()
    wp = season.week_points(wk)

    if not team:
        L = [f"Every roster, week {wk} (ask for a team by name for the detail):"]
        for r in sorted(rosters, key=lambda x: owner.get(x["roster_id"], "")):
            res = set(r.get("reserve") or [])
            act = [p for p in (r.get("players") or []) if p not in res]
            counts: dict = {}
            for p in act:
                pos = (pl.get(p) or {}).get("position") or "DEF"
                counts[pos] = counts.get(pos, 0) + 1
            proj = round(sum((wp.get(p) or {}).get("pts", 0)
                             for p in (r.get("starters") or [])), 1)
            L.append("  %s: %s%s, starters project %s"
                     % (owner.get(r["roster_id"], "?"),
                        " ".join(f"{k}{v}" for k, v in sorted(counts.items(),
                                                              key=lambda kv: -kv[1])),
                        f", {len(res)} on IR" if res else "", proj))
        return "\n".join(L)

    t = team.strip().lower()
    hits = [r for r in rosters if t in (owner.get(r["roster_id"], "") or "").lower()]
    if not hits:
        return (f"No team matching '{team}'. Teams: "
                f"{', '.join(sorted(set(owner.values())))}.")
    out = []
    for r in hits:
        res = set(r.get("reserve") or [])
        starters = list(r.get("starters") or [])
        name = owner.get(r["roster_id"], "?")
        pts = round(sum((wp.get(p) or {}).get("pts", 0) for p in starters), 1)
        out.append(f"{name}, week {wk} - starters project {pts}:")
        for slot, pid in zip(lu.SLOTS, starters):
            if pid in ("0", "", None):
                out.append(f"  {slot:<11} EMPTY")
                continue
            w = wp.get(pid) or {}
            flag = []
            if (pl.get(pid) or {}).get("injury_status"):
                flag.append(pl[pid]["injury_status"])
            if not w.get("has_game", True):
                flag.append("BYE")
            out.append(f"  {slot:<11} {api.player_name(pl, pid)} {w.get('pts', 0)}"
                       + (f" [{', '.join(flag)}]" if flag else ""))
        bench = [p for p in (r.get("players") or [])
                 if p not in starters and p not in res]
        if bench:
            out.append("  bench: " + ", ".join(
                f"{api.player_name(pl, p)} {(wp.get(p) or {}).get('pts', 0)}"
                for p in sorted(bench, key=lambda p: -(wp.get(p) or {}).get("pts", 0))))
        if res:
            out.append("  IR: " + ", ".join(api.player_name(pl, p) for p in res))
    return "\n".join(out)


def league_transactions(kind: str = "", limit: int = 15) -> str:
    """Adds, drops, trades and waiver claims that have actually happened.

    The bot had no way to see these at all, so it could describe a roster
    without knowing how it got that way -- and would have answered "did anyone
    pick up X" from nothing. Only COMPLETED transactions are reported: this
    league ran 246 waiver claims in 2025 and 153 of them failed, and a failed
    claim is not news, it is the tail of somebody's priority list.
    """
    from robo import season
    wk = season.current_week()
    pl = _players()
    owner = _league_owners()
    rows = []
    for w in range(max(1, wk - 4), wk + 1):
        try:
            rows += api.transactions(LEAGUE_ID_2026, w)
        except Exception:
            continue
    rows = [t for t in rows if t.get("status") == "complete"]
    if kind:
        k = kind.strip().lower().replace(" ", "_")
        rows = [t for t in rows if (t.get("type") or "").lower() == k]
    if not rows:
        return ("No completed transactions in this league yet this season"
                + (f" of type '{kind}'" if kind else "")
                + " — the season has not started.")
    rows.sort(key=lambda t: t.get("status_updated") or t.get("created") or 0,
              reverse=True)
    out = [f"Recent completed transactions (newest first, last {min(len(rows), int(limit))}):"]
    for t in rows[:int(limit)]:
        when = datetime.fromtimestamp(
            (t.get("status_updated") or t.get("created") or 0) / 1000).strftime("%b %d")
        who = ", ".join(sorted({owner.get(r, "?") for r in (t.get("roster_ids") or [])}))
        bid = (t.get("settings") or {}).get("waiver_bid")
        adds = ", ".join(api.player_name(pl, p) for p in (t.get("adds") or {}))
        drops = ", ".join(api.player_name(pl, p) for p in (t.get("drops") or {}))
        bits = []
        if adds:
            bits.append("added " + adds)
        if drops:
            bits.append("dropped " + drops)
        out.append(f"  [{when}] {who} ({t.get('type')}"
                   + (f", ${bid}" if bid is not None else "") + "): "
                   + ("; ".join(bits) or "no player movement"))
    return "\n".join(out)


def draft_results(team: str = "", rnd: str = "") -> str:
    """What actually happened in the 2026 draft -- all 204 picks, from Sleeper.

    The bot spent draft day under a hard rule that it could not see the board
    and must never state what happened at a pick, because it had no way to look
    and had already narrated a pick that never occurred. That rule was right
    while the draft was live and is wrong now: the draft is complete and its
    results are public. This is the way to look, so the rule becomes "read it,
    don't remember it" rather than "never discuss it".

    Keeper flags come from the frozen pre-draft board, not Sleeper's is_keeper,
    which returned null for three of the twenty-four -- Jaxon Smith-Njigba,
    James Cook and Javonte Williams would otherwise read as ordinary picks.
    """
    from robo import DRAFT_ID_2026
    try:
        picks = sorted(api.draft_picks(DRAFT_ID_2026), key=lambda p: p["pick_no"])
    except Exception as e:
        return f"Could not read the draft board ({e})."
    if not picks:
        return "The 2026 draft has no picks on it."
    try:
        from robo.league_keepers import board_keepers
        kept = {k["pick_no"] for k in board_keepers()}
    except Exception:
        kept = set()
    users = {u["user_id"]: u["display_name"] for u in api.users(LEAGUE_ID_2026)}
    owner = {r["roster_id"]: users.get(r["owner_id"], "?")
             for r in api.rosters(LEAGUE_ID_2026)}

    def label(p):
        m = p.get("metadata") or {}
        nm = f"{m.get('first_name','')} {m.get('last_name','')}".strip() or p.get("player_id")
        k = " [KEEPER]" if p["pick_no"] in kept else ""
        return f"{nm} ({m.get('position') or '?'}){k}"

    if rnd and str(rnd).strip().isdigit():
        r = int(rnd)
        sel = [p for p in picks if p.get("round") == r]
        if not sel:
            return f"Round {r} has no picks (the draft ran 17 rounds)."
        return f"Round {r}:\n" + "\n".join(
            f"  #{p['pick_no']:>3} {owner.get(p.get('roster_id'), '?')}: {label(p)}"
            for p in sel)

    if team:
        t = team.strip().lower()
        rids = [rid for rid, nm in owner.items() if t in (nm or "").lower()]
        if not rids:
            return (f"No team matching '{team}'. Teams: "
                    f"{', '.join(sorted(set(owner.values())))}.")
        out = []
        for rid in rids:
            sel = [p for p in picks if p.get("roster_id") == rid]
            out.append(f"{owner[rid]} (17 picks):")
            out += [f"  R{p.get('round'):>2} #{p['pick_no']:>3} {label(p)}" for p in sel]
        return "\n".join(out)

    # No filter: round one in full, then each team's shape. 204 picks would
    # swamp the context and answer nothing anybody asked.
    shape: dict[str, dict] = {}
    for p in picks:
        pos = (p.get("metadata") or {}).get("position") or "?"
        shape.setdefault(owner.get(p.get("roster_id"), "?"), {}).setdefault(pos, 0)
        shape[owner.get(p.get("roster_id"), "?")][pos] += 1
    L = ["2026 draft, round 1:"]
    L += [f"  #{p['pick_no']:>2} {owner.get(p.get('roster_id'), '?')}: {label(p)}"
          for p in picks if p.get("round") == 1]
    L.append("Rosters drafted (ask for a team or a round for the detail):")
    for tname, counts in sorted(shape.items()):
        L.append("  %s: %s" % (tname, " ".join(
            f"{k}{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))))
    return "\n".join(L)


def my_lineup(week: str = "") -> str:
    """Who Robowner is actually starting this week, read live from Sleeper.

    Exists because the bot has already been caught narrating a roster decision
    it could not see -- on draft day it announced a pick that never happened,
    having reasoned from its own erroneous alert rather than from state. The
    lesson generalises: if it can be asked about the lineup, it needs a way to
    LOOK at the lineup, otherwise it will confabulate a plausible one.
    """
    from robo import lineup as lu, season
    wk = int(week) if str(week).strip().isdigit() else season.current_week()
    res = lu.run(week=wk, apply=False, verbose=False)
    pl = _players()
    cur = res["previous"][:len(lu.SLOTS)]
    L = [f"Week {wk} starters (live from Sleeper), projected "
         f"{res['current_total']} pts:"]
    for slot, pid in zip(lu.SLOTS, cur):
        L.append(f"  {slot}: {api.player_name(pl, pid) if pid not in ('0','') else 'EMPTY'}")
    if res["illegal"]:
        L.append("Problems with it right now: " + "; ".join(res["illegal"]))
    if res["changed"]:
        L.append(f"A better legal lineup is available, worth {res['gain']:+.1f} pts; "
                 f"the optimiser runs daily and will set it.")
    else:
        L.append("This is the optimal legal lineup for the week.")
    return "\n".join(L)


def roster_state() -> str:
    """Roster, IR and FAAB as they stand, plus what the bot may currently do.

    The last line is the important one. The bot is not currently allowed to add
    or drop anybody, and if asked it must say so rather than describing the
    waiver plans it does not have.
    """
    from robo import ir, season, value
    sl = season.slots()
    L = [f"Roster: {sl['active']}/{sl['roster_max']} active, {sl['open']} open. "
         f"IR: {sl['ir_used']}/{sl['ir_slots']}. FAAB left: {season.faab_left()} "
         f"of {season.FAAB_BUDGET}."]
    onw = season.on_waivers()
    if onw:
        L.append(f"{len(onw)} player(s) currently on waivers rather than free.")
    for w in ir.warnings():
        L.append("Needs attention: " + w)
    if value.ready():
        L.append("Adds, drops and waiver claims are live.")
    else:
        L.append("I am NOT making adds, drops or waiver claims at the moment. The "
                 "rest-of-season valuation that would justify one has not been "
                 "built yet, so that part of me is deliberately switched off. I "
                 "will move an injured player to IR and I will set the lineup.")
    return "\n".join(L)


def explain_myself(topic: str = "") -> str:
    """How the bot itself is built. Optionally the source of one module.

    A tool rather than a keyword trigger. The digest used to be injected into
    the prompt whenever a message matched one of 33 substrings, which fired on
    "do you read the injury reports" ("repo" inside "reports") and on any use of
    "decide" or "calculate" -- 5,846 tokens of architecture attached to a
    lineup question, pulling the answer toward talking about itself. Now it is
    fetched only when the question is genuinely about the bot.
    """
    from robo import selfdoc
    t = (topic or "").strip().lower().replace(".py", "")
    if t:
        src = selfdoc.module_source(t, max_chars=6000)
        if src:
            return f"--- robo/{t}.py ---\n{src}"
        mods = selfdoc.relevant_modules(t)
        if mods:
            out = []
            for m in mods[:2]:
                src = selfdoc.module_source(m, max_chars=5000)
                if src:
                    out.append(f"--- robo/{m}.py ---\n{src}")
            if out:
                return "\n\n".join(out)
    return selfdoc.digest()


SKILLS = {
    "league_chat_history": league_chat_history,
    "my_franchise": my_franchise,
    "league_records": league_records,
    "manager_history": manager_history,
    "manager_drafts": manager_drafts,
    "head_to_head": head_to_head,
    "season_summary": season_summary,
    "player_stats": player_stats,
    "player_news": player_news,
    "player_projection": player_projection,
    "compare_players": compare_players,
    "team_info": team_info,
    "trending_players": trending_players,
    "league_standings": league_standings,
    "who_owns": who_owns,
    "keeper_board": keeper_board,
    "best_available": best_available,
    "explain_myself": explain_myself,
    "my_status": my_status,
    "team_roster": team_roster,
    "league_transactions": league_transactions,
    "draft_results": draft_results,
    "my_lineup": my_lineup,
    "roster_state": roster_state,
}

# Ollama/OpenAI-style tool schemas for the local model
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "my_status",
        "description": "How many replies I have left in my hourly allowance in each chat, when I last spoke, and how long I have been up. Call this for 'how much do you have left', 'are you rate limited', 'how long have you been running', or anyone checking whether I am about to go quiet.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "team_roster",
        "description": "Any team's roster AS IT STANDS RIGHT NOW - starters by slot with this week's projections, bench, and injured reserve. Use for 'what does X have', 'who is starting for X', 'show me X's team', 'is X starting player Y'. This is different from draft_results, which is only what they DRAFTED - one waiver claim makes them disagree. Omit the team for a one-line summary of all twelve.",
        "parameters": {"type": "object", "properties": {
            "team": {"type": "string", "description": "Optional owner name; omit for all twelve teams"},
            "week": {"type": "string", "description": "Optional week number; omit for the current week"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "league_transactions",
        "description": "Adds, drops, trades and waiver claims that have actually happened in this league, newest first, with FAAB amounts. Use for 'did anyone pick up X', 'who dropped Y', 'any trades', 'what did that cost'. Only completed transactions - a failed waiver claim is not news.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "description": "Optional filter: free_agent, waiver, trade, commissioner"},
            "limit": {"type": "integer", "description": "How many to list (default 15)"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "draft_results",
        "description": "What ACTUALLY happened in the 2026 draft - every pick, who took whom, and which were keepers. Call this for any question about the draft: what a team drafted, what you drafted, who went in a round, who was taken where, or how somebody's draft went. The draft is complete and this is the record of it - read it rather than recalling it, and never state a pick you have not looked up.",
        "parameters": {"type": "object", "properties": {
            "team": {"type": "string", "description": "Optional owner name for one team's full 17 picks"},
            "rnd": {"type": "string", "description": "Optional round number for that round across all 12 teams"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "my_lineup",
        "description": "Who I am ACTUALLY starting this week, read live from Sleeper, and whether anything is wrong with it. Call this for any question about my lineup, who I am starting, who I benched, or whether I have set my team - and never answer from memory. I have already once announced a roster move that had not happened.",
        "parameters": {"type": "object", "properties": {
            "week": {"type": "string", "description": "Optional week number; omit for the current week"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "roster_state",
        "description": "My roster count, IR usage, FAAB budget left, who is on waivers, and WHAT I AM CURRENTLY ALLOWED TO DO about it. Call this for any question about adds, drops, waivers, free agents, my budget, or whether I am going to pick somebody up. It will tell you plainly if that part of me is switched off, which it currently is.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "explain_myself",
        "description": "How I am built AND what has recently changed about me. Returns my architecture, my public dev log of recent fixes and new capabilities, and optionally the actual source of one part of me. Call this for any question about myself - how I work, how I decide something, what I am made of, what I can or cannot do, who wrote me, what model I run on. Never describe my own workings from memory; read them.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "Optional area or module name, e.g. 'drafting', 'lineup', 'chat' - omit for the overall architecture"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "keeper_board",
        "description": "Who each team is keeping in 2026 and at which pick. Use for any question about keepers, who is kept, what a team gave up, or which players are off the board before the draft.",
        "parameters": {"type": "object", "properties": {
            "team": {"type": "string", "description": "Optional owner name to filter to one team"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "best_available",
        "description": "The best players ACTUALLY available - our draft board with every kept player, and anyone already drafted, removed. Use whenever asked who is available, who is left, who the best remaining player is, or who to draft. Never answer that from memory: the raw board still contains kept players.",
        "parameters": {"type": "object", "properties": {
            "position": {"type": "string", "description": "Optional QB/RB/WR/TE/K/DEF filter"},
            "count": {"type": "integer", "description": "How many to list (default 10)"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "league_chat_history",
        "description": "Search everything said in this league's chats, GroupMe and Sleeper, back to 2020 -- 2,600+ messages. The last week of conversation is already in front of you; this reaches everything OLDER. Call it whenever a question refers to the past at all: who said something, what was agreed, a running joke, an old argument, what happened in a previous season or draft, or anyone claiming you said something. Search rather than assume you remember -- you do not remember anything older than the last week unless you look.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "my_franchise",
        "description": "The history of the franchise Robowner inherited (roster 4) — which owners held it before and how badly they did.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "league_records",
        "description": "League all-time records: championship history (topic 'champions'), scoring records and biggest blowouts (topic 'records'), or largest FAAB waiver bids (topic 'faab').",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "'champions', 'records', or 'faab'"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "manager_history",
        "description": "A league manager's all-time win-loss record, championships, and season-by-season finishes, 2020-present. Use when someone brags or when you need ammunition about a specific owner.",
        "parameters": {"type": "object", "properties": {
            "manager": {"type": "string", "description": "Manager name, e.g. anders0nAZ, Miller5123"}},
            "required": ["manager"]}}},
    {"type": "function", "function": {
        "name": "manager_drafts",
        "description": "What a manager drafted and kept in past seasons — useful for calling out old draft busts.",
        "parameters": {"type": "object", "properties": {
            "manager": {"type": "string"},
            "season": {"type": "string", "description": "Optional year, e.g. 2023"}},
            "required": ["manager"]}}},
    {"type": "function", "function": {
        "name": "head_to_head",
        "description": "All-time head-to-head record between two league managers, with recent results.",
        "parameters": {"type": "object", "properties": {
            "manager_a": {"type": "string"}, "manager_b": {"type": "string"}},
            "required": ["manager_a", "manager_b"]}}},
    {"type": "function", "function": {
        "name": "season_summary",
        "description": "Final standings and playoff result for one past season (2020-2025).",
        "parameters": {"type": "object", "properties": {
            "season": {"type": "string", "description": "Year, e.g. 2023"}}, "required": ["season"]}}},
    {"type": "function", "function": {
        "name": "player_stats",
        "description": "Real recorded stats for an NFL player — full season, or a single week. Use for 'how many yards did X have', 'what did X do last year/week 5'.",
        "parameters": {"type": "object", "properties": {
            "player": {"type": "string", "description": "Player name"},
            "season": {"type": "string", "description": "Season year, e.g. 2025. Defaults to 2025."},
            "week": {"type": "integer", "description": "Optional week number"}},
            "required": ["player"]}}},
    {"type": "function", "function": {
        "name": "player_news",
        "description": "Latest news, injury status, and analyst notes for a player. Use for 'is X hurt', 'any news on X', 'should I worry about X'.",
        "parameters": {"type": "object", "properties": {
            "player": {"type": "string"}}, "required": ["player"]}}},
    {"type": "function", "function": {
        "name": "player_projection",
        "description": "This season's projection for a player under OUR league's scoring, plus his rank on our draft board and his bye week.",
        "parameters": {"type": "object", "properties": {
            "player": {"type": "string"}}, "required": ["player"]}}},
    {"type": "function", "function": {
        "name": "compare_players",
        "description": "Compare 2-4 players' projections side by side. Pass names comma-separated.",
        "parameters": {"type": "object", "properties": {
            "players": {"type": "string", "description": "Comma-separated names"}},
            "required": ["players"]}}},
    {"type": "function", "function": {
        "name": "team_info",
        "description": "An NFL team's record, next game, and standing. Pass an abbreviation like HOU, KC, LAR.",
        "parameters": {"type": "object", "properties": {
            "team": {"type": "string"}}, "required": ["team"]}}},
    {"type": "function", "function": {
        "name": "trending_players",
        "description": "Most added or dropped players across fantasy leagues in the last 24 hours — the waiver wire buzz.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "description": "'add' or 'drop'"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "league_standings",
        "description": "Current standings in OUR fantasy league (RURFFL).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "who_owns",
        "description": "Which manager in OUR league has a given player on their roster.",
        "parameters": {"type": "object", "properties": {
            "player": {"type": "string"}}, "required": ["player"]}}},
]


def call(name: str, args: dict) -> str:
    fn = SKILLS.get(name)
    if not fn:
        return f"unknown skill {name}"
    try:
        return fn(**args)
    except TypeError as e:
        return f"bad arguments for {name}: {e}"
    except Exception as e:
        return f"{name} failed: {e}"


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(call(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}))
    else:
        print("skills:", ", ".join(SKILLS))
