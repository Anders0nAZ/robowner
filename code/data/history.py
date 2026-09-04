"""Harvest the league's full Sleeper history into data/history.db.

Walks previous_league_id back from 2026 and pulls every season's rosters,
weekly matchups, transactions, drafts, and playoff brackets. All of this is on
the public REST API — no credentials needed. (League CHAT is the exception: it
requires membership, so 2020-2025 chat needs Nate's token, not Robowner's.)

python -m robo.history harvest      # full pull (idempotent, safe to re-run)
python -m robo.history stats        # what's in the DB
"""

import json
import sqlite3
import sys
import time

from robo import DATA, LEAGUE_ID_2026
from robo import sleeper_read as api

DB = DATA / "history.db"
MAX_WEEK = 18

SCHEMA = """
CREATE TABLE IF NOT EXISTS seasons (
    season TEXT PRIMARY KEY, league_id TEXT, name TEXT, status TEXT,
    settings TEXT, scoring TEXT, roster_positions TEXT, metadata TEXT);
CREATE TABLE IF NOT EXISTS managers (
    season TEXT, user_id TEXT, display_name TEXT, team_name TEXT,
    PRIMARY KEY (season, user_id));
CREATE TABLE IF NOT EXISTS rosters (
    season TEXT, roster_id INT, owner_id TEXT, wins INT, losses INT, ties INT,
    fpts REAL, fpts_against REAL, players TEXT, keepers TEXT,
    PRIMARY KEY (season, roster_id));
CREATE TABLE IF NOT EXISTS matchups (
    season TEXT, week INT, roster_id INT, matchup_id INT, points REAL,
    starters TEXT, starters_points TEXT,
    PRIMARY KEY (season, week, roster_id));
CREATE TABLE IF NOT EXISTS transactions (
    season TEXT, transaction_id TEXT PRIMARY KEY, week INT, type TEXT,
    status TEXT, roster_ids TEXT, adds TEXT, drops TEXT, waiver_bid INT,
    notes TEXT,
    created INT);
CREATE TABLE IF NOT EXISTS picks (
    season TEXT, draft_id TEXT, round INT, pick_no INT, roster_id INT,
    player_id TEXT, player_name TEXT, pos TEXT, is_keeper INT, picked_by TEXT,
    PRIMARY KEY (season, pick_no));
CREATE TABLE IF NOT EXISTS brackets (
    season TEXT, kind TEXT, round INT, match_id INT, t1 INT, t2 INT,
    winner INT, loser INT, place INT,
    PRIMARY KEY (season, kind, match_id));
"""


def conn(db=None) -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(db or DB)
    c.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS will not add a column to a table that already
    # exists, so a new one needs its own migration. `notes` carries Sleeper's
    # own stated reason a transaction failed, which is the ONLY thing that
    # distinguishes a claim that lost on price from one that bounced off a full
    # roster -- and reading that distinction out of the `drops` column instead
    # gets it exactly backwards, because a failed claim never executes its drop
    # and so never records one.
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(transactions)")}
        if "notes" not in cols:
            c.execute("ALTER TABLE transactions ADD COLUMN notes TEXT")
            c.commit()
    except sqlite3.Error:
        pass
    return c


def league_chain(start: str = LEAGUE_ID_2026) -> list[dict]:
    """Walk previous_league_id back to the beginning.

    Stops on a 404: a chain can point at a league that has since been deleted,
    which is the end of the recoverable history, not an error.
    """
    out, lid = [], start
    while lid and len(out) < 25:
        try:
            L = api.league(lid)
        except Exception:
            break
        if not L:
            break
        out.append(L)
        lid = L.get("previous_league_id")
    return out


def harvest(verbose: bool = True, start: str = LEAGUE_ID_2026, db=None) -> None:
    players = api.players()
    c = conn(db)
    for L in league_chain(start):
        season, lid = L["season"], L["league_id"]
        if verbose:
            print(f"— {season} ({lid})")
        c.execute("INSERT OR REPLACE INTO seasons VALUES (?,?,?,?,?,?,?,?)", (
            season, lid, L["name"], L["status"], json.dumps(L["settings"]),
            json.dumps(L["scoring_settings"]), json.dumps(L["roster_positions"]),
            json.dumps(L.get("metadata") or {})))

        for u in api.users(lid):
            c.execute("INSERT OR REPLACE INTO managers VALUES (?,?,?,?)", (
                season, u["user_id"], u.get("display_name"),
                (u.get("metadata") or {}).get("team_name")))

        for r in api.rosters(lid):
            s = r["settings"]
            c.execute("INSERT OR REPLACE INTO rosters VALUES (?,?,?,?,?,?,?,?,?,?)", (
                season, r["roster_id"], r.get("owner_id"), s.get("wins"), s.get("losses"),
                s.get("ties"), s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100,
                s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100,
                json.dumps(r.get("players") or []), json.dumps(r.get("keepers") or [])))

        weeks = 0
        for wk in range(1, MAX_WEEK + 1):
            try:
                ms = api.matchups(lid, wk)
            except Exception:
                continue
            if not ms:
                continue
            weeks += 1
            for m in ms:
                c.execute("INSERT OR REPLACE INTO matchups VALUES (?,?,?,?,?,?,?)", (
                    season, wk, m["roster_id"], m.get("matchup_id"), m.get("points"),
                    json.dumps(m.get("starters") or []),
                    json.dumps(m.get("starters_points") or [])))
            try:
                for t in api.transactions(lid, wk):
                    st = t.get("settings") or {}
                    c.execute("INSERT OR REPLACE INTO transactions VALUES "
                              "(?,?,?,?,?,?,?,?,?,?,?)", (
                        season, t["transaction_id"], t.get("leg", wk), t.get("type"),
                        t.get("status"), json.dumps(t.get("roster_ids") or []),
                        json.dumps(t.get("adds") or {}), json.dumps(t.get("drops") or {}),
                        st.get("waiver_bid"), t.get("created"),
                        ((t.get("metadata") or {}).get("notes") or "").strip()))
            except Exception:
                pass

        npicks = 0
        for d in api.drafts(lid):
            # Sleeper's own is_keeper flag is NOT reliable -- 3 of the 24 keeper
            # assignments in the 2026 draft came back null, so trusting it alone
            # publishes three kept players as though they had been drafted. The
            # sound test is "was this pick on the board before the draft opened",
            # which league_keepers freezes while the draft still reads pre_draft.
            # Only that draft has a snapshot; every other season falls back to
            # the flag, which is all there is for them.
            frozen = set()
            try:
                from robo.league_keepers import board_keepers
                frozen = {k["pick_no"] for k in board_keepers(d["draft_id"])}
            except Exception:
                pass
            for p in api.draft_picks(d["draft_id"]):
                meta = p.get("metadata") or {}
                pid = p.get("player_id")
                name = (f"{meta.get('first_name','')} {meta.get('last_name','')}".strip()
                        or (players.get(pid, {}) or {}).get("full_name") or pid)
                keeper = bool(p.get("is_keeper")) or p.get("pick_no") in frozen
                c.execute("INSERT OR REPLACE INTO picks VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    season, d["draft_id"], p.get("round"), p.get("pick_no"),
                    p.get("roster_id"), pid, name, meta.get("position"),
                    1 if keeper else 0, p.get("picked_by")))
                npicks += 1

        nb = 0
        for kind, key in (("winners", "winners_bracket"), ("losers", "losers_bracket")):
            try:
                for b in api.get(f"league/{lid}/{key}"):
                    c.execute("INSERT OR REPLACE INTO brackets VALUES (?,?,?,?,?,?,?,?,?)", (
                        season, kind, b.get("r"), b.get("m"),
                        b.get("t1") if isinstance(b.get("t1"), int) else None,
                        b.get("t2") if isinstance(b.get("t2"), int) else None,
                        b.get("w"), b.get("l"), b.get("p")))
                    nb += 1
            except Exception:
                pass

        if verbose:
            print(f"   {weeks} weeks, {npicks} picks, {nb} bracket rows")
        c.commit()
        time.sleep(0.2)
    c.close()


def stats(db=None) -> None:
    c = conn(db)
    for t in ("seasons", "managers", "rosters", "matchups", "transactions", "picks", "brackets"):
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:<14} {n:>7,}")
    print("\nseasons:", [r[0] for r in c.execute("SELECT season FROM seasons ORDER BY season")])
    c.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "harvest"
    if cmd == "harvest":
        harvest()
        stats()
    else:
        stats()
