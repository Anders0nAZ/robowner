"""Every roster change, one row each: what happened, when, and who asked for it.

WHY THIS EXISTS. The audit app follows DECISION RUNS -- a news pulse or a
scheduled pass, screened and priced front to back. But not every roster change
belongs to a run. On 23 Sep 2026 the IR unblock cut our new defence at 04:23;
the only trace was a public decision-log line and a nested key in the raw JSON
of one news record, and a cut made by the cascade, `robo.ir --apply` or the
construction check would not have reached any run record at all. Nothing
anywhere said which path had instructed it.

TWO HALVES.

  * `journal()` is called by the Sleeper writers themselves, so every roster
    write the bot makes is recorded whatever path called it, together with
    `construction.path()` -- the sessions open at that moment, outermost first
    ("news pulse > construction > ir unblock") -- the process entry point and
    the caller's one-line reason. Append-only JSONL; it never raises, because a
    ledger that could fail a transaction would be worse than no ledger.
  * `ledger()` joins that journal with Sleeper's own record (the ground truth
    for what executed, and the only place a failed claim's reason lives), the
    waiver lifecycle journal, the public decision log and the decision runs.
    Anything on Sleeper for our roster that no bot record explains is labelled
    as not made by the bot.

Rows from before the journal existed are RECONSTRUCTED from the decision log
and the run records, and say so: an inferred path is labelled as inferred.

    python -m robo.transactions            # print the ledger
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from robo import DATA, LEAGUE_ID_2026, ROOT

JOURNAL = DATA / "transaction_journal.jsonl"
DECISIONS = ROOT / "decision-log" / "data" / "decisions.json"
NEWS_LOG = ROOT / "news-watch.log"
ROSTER_ID = 4

# How far apart a decision-log entry and the Sleeper transaction it describes
# may be. The entry is written right after the write lands, so seconds apart in
# practice; ten minutes only guards a slow read-back.
DECISION_MATCH_S = 600
# A run record is written when the run ENDS, so the write it made precedes the
# file's timestamp by up to one run's duration.
RUN_MATCH_S = 900

# The first element of a path, in the words the page filters on.
ORIGINS = {"news pulse": "news pulse", "cascade": "cascade"}

NOT_BOT = "not made by the bot"


# ------------------------------------------------------------------ journal

def _entry() -> str:
    task = os.environ.get("ROBONER_TASK")
    if task:
        return task
    argv = [a for a in sys.argv if a]
    if not argv:
        return "unknown"
    head = Path(argv[0]).stem
    if head in ("__main__", "-m", "-c") or argv[0].endswith("__main__.py"):
        head = "python"
    # `python -m robo.x --apply` arrives as argv[0] = ...\robo\x.py
    if Path(argv[0]).parent.name == "robo":
        head = "robo." + Path(argv[0]).stem
    return " ".join([head] + argv[1:])[:160]


def journal(kind: str, *, league_id: str = LEAGUE_ID_2026,
            adds: dict | None = None, drops: dict | None = None,
            bid: int | None = None, reserve: list | None = None,
            reserve_before: list | None = None, transaction_id: str | None = None,
            ok: bool = True, error: str | None = None,
            reason: str | None = None, path: list | None = None) -> None:
    """Record one roster write. Never raises."""
    try:
        if path is None:
            from robo import construction
            path = construction.path()
        row = {"at": time.time(), "kind": kind, "league_id": league_id,
               "adds": sorted(adds or {}), "drops": sorted(drops or {}),
               "bid": bid, "reserve": reserve, "reserve_before": reserve_before,
               "transaction_id": str(transaction_id) if transaction_id else None,
               "ok": ok, "error": (error or None) and str(error)[:300],
               "path": path, "entry": _entry(), "reason": reason}
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    except Exception:
        pass


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


# ------------------------------------------------------------------ sources

def _sleeper_rows(league_id: str, weeks) -> list[dict]:
    from robo import sleeper_read as api
    out = []
    for leg in weeks:
        try:
            rows = api.transactions(league_id, leg) or []
        except Exception:
            continue
        for t in rows:
            if ROSTER_ID in (t.get("roster_ids") or []):
                out.append({**t, "_leg": leg})
    return out


def _pending(league_id: str) -> list[dict]:
    try:
        from robo import sleeper_write as sw
        return sw.pending_waiver_claims(ROSTER_ID, league_id) or []
    except Exception:
        return []


def _decisions() -> list[dict]:
    try:
        d = json.loads(DECISIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    d = d if isinstance(d, list) else d.get("decisions") or []
    for e in d:
        try:
            e["_at"] = datetime.fromisoformat(e["ts"]).timestamp()
        except Exception:
            e["_at"] = None
    return [e for e in d if e.get("kind") in ("free-agent", "waiver", "ir")]


def _runs() -> list[dict]:
    try:
        from robo import decision_audit
        return decision_audit.events(limit=None)
    except Exception:
        return []


def _construction_times() -> list[float]:
    """When the pulse logged a construction repair, as epoch seconds.

    The only record of it before the journal existed: news-watch.log stamps
    local wall-clock time, and the pulse runs on this machine.
    """
    out = []
    try:
        text = NEWS_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for m in re.finditer(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] roster construction: repaired",
                         text, re.M):
        out.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
    return out


# ------------------------------------------------------------------ ledger

def _sleeper_type(t: dict) -> str:
    kind, status = t.get("type"), t.get("status")
    if kind == "waiver":
        return {"complete": "claim won", "failed": "claim lost",
                "cancelled": "claim cancelled"}.get(status, "claim pending")
    if kind == "free_agent":
        adds, drops = t.get("adds") or {}, t.get("drops") or {}
        return "add+drop" if adds and drops else ("add" if adds else "drop")
    return kind or "transaction"


def _ours(side: dict | None) -> list[str]:
    return sorted(str(p) for p, rid in (side or {}).items() if rid == ROSTER_ID)


def _nearest(items, t: float, window: float, key=lambda x: x["_at"], after_only=False):
    best, gap = None, window
    for it in items:
        at = key(it)
        if at is None:
            continue
        d = (at - t) if after_only else abs(at - t)
        if after_only and d < 0:
            continue
        if d <= gap:
            best, gap = it, d
    return best


def _run_path(run: dict | None, t: float, decision: dict | None,
              repairs: list[float]) -> list[str]:
    """What we can infer about who instructed a pre-journal write."""
    if run is None:
        return []
    raw = run.get("raw") or {}
    first = "news pulse" if run.get("kind") == "news" else f"scheduled {run.get('mode') or 'pass'}"
    path = [first]
    if any(abs(r - t) <= 120 for r in repairs):
        path.append("construction")
    kind = (decision or {}).get("kind")
    unblock = ((raw.get("action") or {}).get("roster_first") or {}).get("unblock") or {}
    if kind == "ir" and unblock.get("applied"):
        path.append("ir unblock")
    elif kind == "free-agent":
        mode = ((decision or {}).get("data") or {}).get("mode")
        path.append(f"moves free {mode}" if mode else "moves free")
    return path


def ledger(season: str = "2026", weeks=None, league_id: str = LEAGUE_ID_2026,
           pending: bool = True) -> list[dict]:
    """Every roster change for our team, newest first. Read-only."""
    from robo import season as _season
    from robo import sleeper_read as api

    if weeks is None:
        try:
            cur = int(_season.current_week())
        except Exception:
            cur = 18
        weeks = range(1, min(cur, 18) + 1)
    try:
        players = api.players()
    except Exception:
        players = {}

    def name(pid):
        return api.player_name(players, str(pid)) if players else str(pid)

    journal_rows = [r for r in _read_jsonl(JOURNAL)
                    if r.get("league_id", league_id) == league_id]
    by_txn = {}
    for r in journal_rows:
        if r.get("transaction_id"):
            by_txn.setdefault(r["transaction_id"], []).append(r)
    lifecycle = {}
    try:
        from robo import waiver_manager
        for r in _read_jsonl(waiver_manager.JOURNAL):
            if r.get("transaction_id"):
                lifecycle.setdefault(str(r["transaction_id"]), []).append(r)
    except Exception:
        pass
    decisions = _decisions()
    decision_by_txn = {}
    for e in decisions:
        for c in (e.get("data") or {}).get("claims") or []:
            if c.get("transaction_id"):
                decision_by_txn[str(c["transaction_id"])] = e
    runs = _runs()
    repairs = _construction_times()

    rows = []
    sleeper = _sleeper_rows(league_id, weeks)
    seen = {str(t.get("transaction_id")) for t in sleeper}
    if pending:
        for t in _pending(league_id):
            if str(t.get("transaction_id")) not in seen:
                sleeper.append({**t, "status": t.get("status") or "pending"})

    for t in sleeper:
        txn = str(t.get("transaction_id"))
        at = (t.get("created") or 0) / 1000 or None
        settled = (t.get("status_updated") or 0) / 1000 or None
        adds, drops = _ours(t.get("adds")), _ours(t.get("drops"))
        jr = (by_txn.get(txn) or [None])[0]
        lc = lifecycle.get(txn) or []
        submitted = next((r for r in lc if r.get("kind") == "submitted"), None)
        dec = decision_by_txn.get(txn)
        if dec is None and t.get("type") == "free_agent" and at:
            dec = _nearest([e for e in decisions if _matches(e, adds, drops)],
                           at, DECISION_MATCH_S)
        fp = (submitted or {}).get("fingerprint")
        run = next((r for r in runs if fp and r.get("fingerprint") == fp), None)
        if run is None and at and (jr or submitted or dec):
            run = _nearest(runs, at, RUN_MATCH_S,
                           key=lambda r: r.get("at"), after_only=True)

        if jr:
            path, prov, reason, entry = (jr.get("path") or [], "journal",
                                         jr.get("reason"), jr.get("entry"))
        elif submitted:
            src = submitted.get("source")
            path = ["news pulse" if src == "newswatch" else (src or "waivers")]
            prov, reason, entry = "lifecycle", _claim_reason(submitted), None
        elif dec:
            path = _run_path(run, at or dec["_at"], dec, repairs)
            prov, reason, entry = "reconstructed", None, None
        else:
            path, prov, reason, entry = [], "sleeper only", None, None

        bot = prov != "sleeper only"
        drop_names = [name(p) for p in drops]
        # A claim that did not execute never records its drop on Sleeper -- see
        # the metadata.notes gotcha -- so the one it named comes from our spec.
        intended = ((submitted or {}).get("spec") or {}).get("drop_id")
        if not drops and intended and t.get("type") == "waiver":
            drop_names = [f"{name(intended)} (not executed)"]
        rows.append({
            "when": at, "settled_at": settled if settled != at else None,
            "type": _sleeper_type(t), "week": t.get("_leg") or t.get("leg"),
            "adds": [name(p) for p in adds], "drops": drop_names,
            "add_ids": adds, "drop_ids": drops,
            "bid": (t.get("settings") or {}).get("waiver_bid"),
            "status": t.get("status"),
            "sleeper_note": (t.get("metadata") or {}).get("notes"),
            "initiated_by": " > ".join(path) if path else ("unrecorded" if bot else NOT_BOT),
            "origin": _origin(path, bot), "path": path, "entry": entry,
            "reason": reason, "provenance": prov,
            "decision_id": (dec or {}).get("id"),
            "decision": (dec or {}).get("decision"),
            "rationale": (dec or {}).get("rationale"),
            "run_fingerprint": (run or {}).get("fingerprint"),
            "transaction_id": txn, "sleeper": t, "journal": jr,
        })

    # IR moves are not Sleeper transactions; the journal is their only record.
    for r in journal_rows:
        if r.get("kind") != "reserve" or not r.get("ok", True):
            continue
        before = set(r.get("reserve_before") or [])
        after = set(r.get("reserve") or [])
        for pid, kind in ([(p, "ir park") for p in sorted(after - before)]
                          + [(p, "ir activate") for p in sorted(before - after)]):
            rows.append(_ir_row(r["at"], kind, pid, name(pid), r.get("path") or [],
                                "journal", r.get("reason"), r.get("entry"), r))
    # Before the journal: IR moves the decision log recorded.
    first_journal = min((r["at"] for r in journal_rows), default=float("inf"))
    for e in decisions:
        if e.get("kind") != "ir" or not e.get("_at") or e["_at"] >= first_journal:
            continue
        run = _nearest(runs, e["_at"], RUN_MATCH_S, key=lambda r: r.get("at"),
                       after_only=True)
        path = _run_path(run, e["_at"], e, repairs)
        data = e.get("data") or {}
        moves = [(p, "ir activate") for s in data.get("steps") or []
                 if s.get("landed") for p in s.get("activate") or []]
        if "reserve" in data and "previous" in data:
            moves += [(p, "ir park") for p in sorted(set(data["reserve"]) - set(data["previous"]))]
            moves += [(p, "ir activate") for p in sorted(set(data["previous"]) - set(data["reserve"]))]
        for pid, kind in moves:
            row = _ir_row(e["_at"], kind, pid, name(pid), path, "reconstructed",
                          None, None, None)
            row.update(decision_id=e.get("id"), decision=e.get("decision"),
                       rationale=e.get("rationale"),
                       run_fingerprint=(run or {}).get("fingerprint"))
            rows.append(row)

    # An IR move carries no Sleeper leg; it belongs to the week of whatever
    # Sleeper transaction sits nearest it in time.
    dated = [r for r in rows if r.get("week") and r.get("when")]
    for r in rows:
        if r.get("week") is None and r.get("when") and dated:
            r["week"] = min(dated, key=lambda d: abs(d["when"] - r["when"]))["week"]
    rows.sort(key=lambda r: r["when"] or 0, reverse=True)
    return rows


def _ir_row(at, kind, pid, pname, path, prov, reason, entry, jr) -> dict:
    return {"when": at, "settled_at": None, "type": kind, "week": None,
            "adds": [pname] if kind == "ir activate" else [],
            "drops": [pname] if kind == "ir park" else [],
            "add_ids": [pid] if kind == "ir activate" else [],
            "drop_ids": [pid] if kind == "ir park" else [],
            "bid": None, "status": "complete", "sleeper_note": None,
            "initiated_by": " > ".join(path) if path else "unrecorded",
            "origin": _origin(path, True), "path": path, "entry": entry,
            "reason": reason, "provenance": prov, "decision_id": None,
            "decision": None, "rationale": None, "run_fingerprint": None,
            "transaction_id": None, "sleeper": None, "journal": jr}


def _matches(e: dict, adds: list, drops: list) -> bool:
    data = e.get("data") or {}
    if e.get("kind") == "free-agent":
        return ([str(data["add"])] if data.get("add") else []) == adds and \
               ([str(data["drop"])] if data.get("drop") else []) == drops
    if e.get("kind") == "ir":
        cut = [str(s["drop"]) for s in data.get("steps") or []
               if s.get("action") == "drop" and s.get("landed")]
        return not adds and bool(drops) and set(drops) <= set(cut)
    return False


def _claim_reason(r: dict) -> str | None:
    spec = r.get("spec") or {}
    if not spec:
        return None
    bits = [f"claim ${spec.get('bid')}"] if spec.get("bid") is not None else []
    if spec.get("gain") is not None:
        bits.append(f"simulated gain {spec['gain']:+.1f}")
    if spec.get("group_id"):
        bits.append(f"ladder {spec['group_id']} rung {spec.get('priority', 0) + 1}")
    return ", ".join(bits) or None


def _origin(path: list, bot: bool) -> str:
    """The driver at the head of the path, in the page's filter words.

    A path that starts at a module's own session ("moves free ros", "ir
    sweep") had no driver above it: a scheduled task or a hand-run command
    called that module directly, and `entry` says which.
    """
    if not bot:
        return NOT_BOT
    if not path:
        return "unrecorded"
    head = path[0]
    if head.startswith("scheduled"):
        return "scheduled pass"
    return ORIGINS.get(head, "direct command")


def main():
    from robo import news_audit
    for r in ledger():
        print(f"{news_audit.local_time(r['when']):<20} {r['type']:<16} "
              f"+{','.join(r['adds']) or '-':<24} -{','.join(r['drops']) or '-':<24} "
              f"{r['initiated_by']}  [{r['provenance']}]")


if __name__ == "__main__":
    main()
