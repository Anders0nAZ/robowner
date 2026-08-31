"""League lore — derived narrative facts from data/history.db.

Turns six seasons of raw Sleeper data into the things people actually argue
about: who won, who choked, head-to-head records, blowouts, draft busts.

Manager identity is keyed on Sleeper user_id (stable across seasons, unlike
display names — JCGlock spent years as JGluck).

python -m robo.lore <function> [args]
"""

import json
import sqlite3
import sys
from functools import lru_cache

from robo import DATA

DB = DATA / "history.db"


def _c() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


@lru_cache(maxsize=1)
def managers() -> dict[str, dict]:
    """user_id -> {name (most recent), aliases, seasons}."""
    out = {}
    with _c() as c:
        for r in c.execute("SELECT season, user_id, display_name, team_name FROM managers ORDER BY season"):
            m = out.setdefault(r["user_id"], {"name": None, "aliases": set(), "seasons": [], "teams": set()})
            m["name"] = r["display_name"] or m["name"]      # last season wins
            if r["display_name"]:
                m["aliases"].add(r["display_name"])
            if r["team_name"]:
                m["teams"].add(r["team_name"])
            m["seasons"].append(r["season"])
    return out


@lru_cache(maxsize=1)
def people() -> list[dict]:
    """Real-name <-> Sleeper-handle map (data/people.json). GroupMe uses real
    names and Sleeper uses handles; without this the bot misattributes quotes."""
    path = DATA / "people.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("people", [])


def _handles_for_realname(term: str) -> list[str]:
    """All people matching a real name. More than one means it's ambiguous —
    'Chris' is both Chris Miller and Chris Sindik, and guessing wrong means
    publicly misattributing a quote."""
    t = term.strip().lower()
    exact, loose = [], []
    for p in people():
        names = [n.lower() for n in p.get("groupme", [])]
        if t in names or any(t == a.lower() for a in p.get("aliases", [])):
            exact.append(p["sleeper"])
        elif (p.get("first") or "").lower() == t:
            loose.append(p["sleeper"])
    return exact or loose


def ambiguous(term: str) -> list[str]:
    """Candidate display names when a term matches more than one person."""
    hits = _handles_for_realname(term)
    return hits if len(hits) > 1 else []


def _handle_for_realname(term: str) -> str | None:
    hits = _handles_for_realname(term)
    return hits[0] if len(hits) == 1 else None


def resolve_manager(term: str) -> str | None:
    """Name / alias / real name -> user_id.

    Team names are matched only as a last resort: 'Bob' would otherwise hit
    Miller5123's old team 'In the Shadow of the Bob' rather than the actual Bob.
    """
    t = (term or "").strip().lower()
    if not t:
        return None
    mgrs = managers()
    handle = _handle_for_realname(t)
    if handle:
        t = handle.lower()
    for uid, m in mgrs.items():                      # exact handle / alias
        if t == (m["name"] or "").lower() or any(t == a.lower() for a in m["aliases"]):
            return uid
    for uid, m in mgrs.items():                      # partial handle
        if any(t in a.lower() for a in list(m["aliases"]) + [m["name"] or ""] if a):
            return uid
    for uid, m in mgrs.items():                      # team name, last resort
        if any(t in tm.lower() for tm in m["teams"]):
            return uid
    return None


def name_of(uid: str) -> str:
    return (managers().get(uid) or {}).get("name") or uid


@lru_cache(maxsize=1)
def roster_owner() -> dict[tuple, str]:
    """(season, roster_id) -> user_id."""
    with _c() as c:
        return {(r["season"], r["roster_id"]): r["owner_id"]
                for r in c.execute("SELECT season, roster_id, owner_id FROM rosters")}


def _owner(season: str, roster_id: int) -> str:
    return roster_owner().get((season, roster_id), "")


# ------------------------------------------------------------------ results

@lru_cache(maxsize=1)
def season_results() -> dict[str, dict]:
    """season -> {champion, runner_up, third, last} as user_ids."""
    out = {}
    with _c() as c:
        for r in c.execute("SELECT season, kind, place, winner, loser FROM brackets WHERE place IS NOT NULL"):
            s = out.setdefault(r["season"], {})
            if r["kind"] == "winners" and r["place"] == 1:
                s["champion"], s["runner_up"] = _owner(r["season"], r["winner"]), _owner(r["season"], r["loser"])
            elif r["kind"] == "winners" and r["place"] == 3:
                s["third"] = _owner(r["season"], r["winner"])
            elif r["kind"] == "losers" and r["place"] == 5:
                # losers bracket p=5 is the bottom game; loser finishes last overall
                s["last"] = _owner(r["season"], r["loser"])
    return out


def champions() -> str:
    """All-time championship roll."""
    res, tally = season_results(), {}
    lines = []
    for season in sorted(res):
        s = res[season]
        if not s.get("champion"):
            continue
        tally[s["champion"]] = tally.get(s["champion"], 0) + 1
        lines.append(f"{season}: {name_of(s['champion'])} beat {name_of(s.get('runner_up',''))}"
                     + (f" (3rd: {name_of(s['third'])})" if s.get("third") else ""))
    ranked = sorted(tally.items(), key=lambda kv: -kv[1])
    roll = ", ".join(f"{name_of(u)} {n}" for u, n in ranked)
    return "\n".join(lines) + f"\n\nTitles: {roll}"


def season_summary(season: str) -> str:
    """Final standings and playoff result for one season."""
    season = str(season)
    with _c() as c:
        rows = c.execute("""SELECT roster_id, owner_id, wins, losses, ties, fpts, fpts_against
                            FROM rosters WHERE season=? ORDER BY wins DESC, fpts DESC""", (season,)).fetchall()
    if not rows:
        return f"No data for {season}."
    res = season_results().get(season, {})
    out = [f"{season} season:"]
    if res.get("champion"):
        out.append(f"  CHAMPION: {name_of(res['champion'])}"
                   f" | runner-up: {name_of(res.get('runner_up',''))}"
                   + (f" | 3rd: {name_of(res['third'])}" if res.get("third") else ""))
    for i, r in enumerate(rows, 1):
        out.append(f"  {i:>2}. {name_of(r['owner_id']):<16} {r['wins']}-{r['losses']}"
                   f"{'-'+str(r['ties']) if r['ties'] else ''}  {r['fpts']:.1f} pts")
    return "\n".join(out)


def head_to_head(manager_a: str, manager_b: str) -> str:
    """All-time regular season + playoff record between two managers."""
    ua, ub = resolve_manager(manager_a), resolve_manager(manager_b)
    if not ua or not ub:
        return f"Couldn't identify {'both' if not ua and not ub else (manager_a if not ua else manager_b)}."
    if ua == ub:
        return "That's the same manager."
    wins = {ua: 0, ub: 0}
    games, margin = [], 0.0
    with _c() as c:
        rows = c.execute("""SELECT season, week, roster_id, matchup_id, points FROM matchups
                            WHERE matchup_id IS NOT NULL AND points IS NOT NULL""").fetchall()
    by_game = {}
    for r in rows:
        by_game.setdefault((r["season"], r["week"], r["matchup_id"]), []).append(r)
    for (season, week, _), pair in by_game.items():
        if len(pair) != 2:
            continue
        owners = [_owner(season, p["roster_id"]) for p in pair]
        if set(owners) != {ua, ub}:
            continue
        hi, lo = (pair[0], pair[1]) if pair[0]["points"] >= pair[1]["points"] else (pair[1], pair[0])
        w = _owner(season, hi["roster_id"])
        wins[w] += 1
        margin += hi["points"] - lo["points"] if w == ua else -(hi["points"] - lo["points"])
        games.append(f"{season} wk{week}: {name_of(w)} {hi['points']:.1f}-{lo['points']:.1f}")
    if not games:
        return f"{name_of(ua)} and {name_of(ub)} have never played."
    lead = name_of(ua) if wins[ua] >= wins[ub] else name_of(ub)
    return (f"{name_of(ua)} vs {name_of(ub)}: {wins[ua]}-{wins[ub]} all-time ({lead} leads). "
            f"Avg margin {margin/len(games):+.1f} for {name_of(ua)}.\n"
            + "\n".join(games[-8:]))


def record_book() -> str:
    """Extremes: best and worst single weeks, biggest blowouts, closest games."""
    with _c() as c:
        rows = c.execute("""SELECT season, week, roster_id, matchup_id, points FROM matchups
                            WHERE points IS NOT NULL AND points > 0""").fetchall()
    scores = sorted(rows, key=lambda r: -r["points"])
    out = ["Highest single weeks:"]
    for r in scores[:5]:
        out.append(f"  {r['points']:.1f} — {name_of(_owner(r['season'], r['roster_id']))} ({r['season']} wk{r['week']})")
    out.append("Lowest single weeks:")
    for r in scores[-5:][::-1]:
        out.append(f"  {r['points']:.1f} — {name_of(_owner(r['season'], r['roster_id']))} ({r['season']} wk{r['week']})")
    by_game = {}
    for r in rows:
        if r["matchup_id"]:
            by_game.setdefault((r["season"], r["week"], r["matchup_id"]), []).append(r)
    diffs = []
    for (season, week, _), pair in by_game.items():
        if len(pair) == 2:
            d = abs(pair[0]["points"] - pair[1]["points"])
            hi, lo = sorted(pair, key=lambda p: -p["points"])
            diffs.append((d, season, week, hi, lo))
    diffs.sort(key=lambda x: -x[0])
    out.append("Biggest blowouts:")
    for d, season, week, hi, lo in diffs[:5]:
        out.append(f"  {name_of(_owner(season, hi['roster_id']))} beat "
                   f"{name_of(_owner(season, lo['roster_id']))} by {d:.1f} ({season} wk{week})")
    out.append("Closest games:")
    for d, season, week, hi, lo in [x for x in diffs if x[0] > 0][-4:][::-1]:
        out.append(f"  {name_of(_owner(season, hi['roster_id']))} over "
                   f"{name_of(_owner(season, lo['roster_id']))} by {d:.2f} ({season} wk{week})")
    return "\n".join(out)


def manager_profile(manager: str) -> str:
    """All-time record, titles, and season-by-season for one owner."""
    uid = resolve_manager(manager)
    if not uid:
        return f"No manager matching '{manager}'."
    m = managers()[uid]
    with _c() as c:
        rows = c.execute("""SELECT season, wins, losses, ties, fpts FROM rosters
                            WHERE owner_id=? ORDER BY season""", (uid,)).fetchall()
    res = season_results()
    titles = [s for s, v in res.items() if v.get("champion") == uid]
    seconds = [s for s, v in res.items() if v.get("runner_up") == uid]
    W = sum(r["wins"] or 0 for r in rows)
    L = sum(r["losses"] or 0 for r in rows)
    out = [f"{m['name']} — {W}-{L} all-time over {len(rows)} seasons."]
    if m["aliases"] - {m["name"]}:
        out[0] += f" (formerly {', '.join(sorted(m['aliases'] - {m['name']}))})"
    out.append(f"  Titles: {', '.join(titles) if titles else 'none'}"
               + (f" | runner-up: {', '.join(seconds)}" if seconds else ""))
    for r in rows:
        tag = ""
        v = res.get(r["season"], {})
        if v.get("champion") == uid:
            tag = "  CHAMPION"
        elif v.get("runner_up") == uid:
            tag = "  (lost final)"
        out.append(f"  {r['season']}: {r['wins']}-{r['losses']} {r['fpts']:.1f} pts{tag}")
    return "\n".join(out)


def draft_history(manager: str, season: str = "") -> str:
    """What a manager drafted — their picks, keepers flagged."""
    uid = resolve_manager(manager)
    if not uid:
        return f"No manager matching '{manager}'."
    q = """SELECT p.season, p.round, p.pick_no, p.player_name, p.pos, p.is_keeper
           FROM picks p JOIN rosters r ON r.season=p.season AND r.roster_id=p.roster_id
           WHERE r.owner_id=?"""
    args = [uid]
    if season:
        q += " AND p.season=?"
        args.append(str(season))
    q += " ORDER BY p.season DESC, p.pick_no LIMIT 40"
    with _c() as c:
        rows = c.execute(q, args).fetchall()
    if not rows:
        return f"No draft picks found for {name_of(uid)}{' in ' + season if season else ''}."
    out = [f"{name_of(uid)} draft picks{' (' + season + ')' if season else ''}:"]
    for r in rows:
        k = " [KEEPER]" if r["is_keeper"] else ""
        out.append(f"  {r['season']} R{r['round']:>2} (#{r['pick_no']:>3}) {r['player_name']} {r['pos'] or ''}{k}")
    return "\n".join(out)


def keeper_history(term: str = "") -> str:
    """Who has been kept, by whom, and when."""
    q = """SELECT p.season, p.round, p.player_name, p.pos, r.owner_id
           FROM picks p JOIN rosters r ON r.season=p.season AND r.roster_id=p.roster_id
           WHERE p.is_keeper=1"""
    args = []
    uid = resolve_manager(term) if term else None
    if uid:
        q += " AND r.owner_id=?"
        args.append(uid)
    elif term:
        q += " AND lower(p.player_name) LIKE ?"
        args.append(f"%{term.lower()}%")
    q += " ORDER BY p.season DESC, p.round LIMIT 40"
    with _c() as c:
        rows = c.execute(q, args).fetchall()
    if not rows:
        return f"No keeper records for '{term}'." if term else "No keeper records."
    return "\n".join(f"  {r['season']} R{r['round']:>2} {r['player_name']:<22} kept by {name_of(r['owner_id'])}"
                     for r in rows)


def biggest_faab() -> str:
    """The most anyone has ever spent on a waiver claim."""
    with _c() as c:
        rows = c.execute("""SELECT season, week, roster_ids, adds, waiver_bid FROM transactions
                            WHERE waiver_bid IS NOT NULL AND waiver_bid > 0 AND status='complete'
                            ORDER BY waiver_bid DESC LIMIT 12""").fetchall()
    if not rows:
        return "No FAAB history recorded."
    from robo import sleeper_read as api
    players = api.players()
    out = ["Biggest FAAB bids:"]
    for r in rows:
        rid = (json.loads(r["roster_ids"]) or [None])[0]
        adds = json.loads(r["adds"] or "{}")
        who = ", ".join((players.get(p, {}) or {}).get("full_name", p) for p in adds) or "?"
        out.append(f"  ${r['waiver_bid']} — {name_of(_owner(r['season'], rid))} on {who} ({r['season']} wk{r['week']})")
    return "\n".join(out)


def franchise_history(roster_id: int = 4) -> str:
    """Every owner who has held one franchise (roster slot) and how they did.

    Robowner inherited roster 4, which has now burned through two owners.
    """
    with _c() as c:
        rows = c.execute("""SELECT season, owner_id, wins, losses, fpts FROM rosters
                            WHERE roster_id=? ORDER BY season""", (int(roster_id),)).fetchall()
    if not rows:
        return f"No history for franchise {roster_id}."
    res = season_results()
    out = [f"Franchise (roster {roster_id}) — owner by owner:"]
    for r in rows:
        tag = ""
        v = res.get(r["season"], {})
        if v.get("champion") == r["owner_id"]:
            tag = "  CHAMPION"
        elif v.get("last") == r["owner_id"]:
            tag = "  (last place)"
        out.append(f"  {r['season']}  {name_of(r['owner_id']):<16} {r['wins']}-{r['losses']}"
                   f"  {r['fpts']:.1f}{tag}")
    return "\n".join(out)


FUNCS = {f.__name__: f for f in (champions, season_summary, head_to_head, record_book,
                                 manager_profile, draft_history, keeper_history, biggest_faab,
                                 franchise_history)}

if __name__ == "__main__":
    fn = sys.argv[1] if len(sys.argv) > 1 else "champions"
    print(FUNCS[fn](*sys.argv[2:]))
