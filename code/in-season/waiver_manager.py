"""Own, reconcile, and audit Robowner's pending waiver portfolio.

Valuation belongs to :mod:`robo.moves`.  This module owns the stateful half:
which exact transactions are pending, which ones the bot created, and how a
changed desired portfolio replaces the old one without touching a human claim.
No other module may call the waiver submit/cancel mutations directly.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from robo import DATA, LEAGUE_ID_2026
from robo import sleeper_read as api

STATE = DATA / "pending_waivers.json"
JOURNAL = DATA / "waiver_lifecycle.jsonl"
SCHEMA = 1
SPEC_KEYS = ("add_id", "drop_id", "bid", "group_id", "kind", "submit_order")


def _blank() -> dict:
    return {"schema": SCHEMA, "active": {}, "history": [],
            "last_fingerprint": None, "updated": None}


def load() -> dict:
    try:
        doc = json.loads(STATE.read_text(encoding="utf-8"))
        if doc.get("schema") == SCHEMA and isinstance(doc.get("active"), dict):
            return doc
    except (OSError, ValueError, TypeError):
        pass
    return _blank()


def _write(doc: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    doc["schema"], doc["updated"] = SCHEMA, time.time()
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE)


def _event(kind: str, **data) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": time.time(), "kind": kind, **data}
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def recent_events(limit: int = 25) -> list[dict]:
    """The tail of the lifecycle journal, newest first.

    The audit page shows what the planner WANTED and what is pending now; this
    is the half in between -- which claims were cancelled to make room, which
    were submitted, what a rollback did. A reconciliation that failed and
    restored itself leaves no trace in either of the other two views.
    """
    try:
        lines = JOURNAL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, int(limit)):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(out))


def _one_key(value) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    return str(next(iter(value)))


def pending_spec(row: dict, order: int = 0) -> dict:
    """Normalize Sleeper's transaction object to the planner's wire identity."""
    return {"add_id": _one_key(row.get("adds")),
            "drop_id": _one_key(row.get("drops")),
            "bid": int((row.get("settings") or {}).get("waiver_bid") or 0),
            "group_id": None, "kind": None, "submit_order": int(order)}


def normalize_spec(spec: dict, order: int = 0) -> dict:
    out = dict(spec)
    out["add_id"] = str(spec.get("add_id"))
    out["drop_id"] = (None if spec.get("drop_id") is None
                      else str(spec.get("drop_id")))
    out["bid"] = max(0, int(spec.get("bid") or 0))
    out["group_id"] = str(spec.get("group_id") or
                          ("open" if out["drop_id"] is None
                           else f"skill:{out['drop_id']}"))
    out["kind"] = str(spec.get("kind") or
                      ("open" if out["drop_id"] is None else "skill"))
    out["submit_order"] = int(spec.get("submit_order", order))
    return out


def _identity(spec: dict) -> tuple:
    return tuple(spec.get(k) for k in SPEC_KEYS)


def _wire_identity(spec: dict) -> tuple:
    return spec.get("add_id"), spec.get("drop_id"), int(spec.get("bid") or 0)


def _active_specs(doc: dict, pending_by_id: dict[str, dict]) -> list[dict]:
    out = []
    for txid, saved in doc.get("active", {}).items():
        if txid not in pending_by_id:
            continue
        spec = normalize_spec(saved.get("spec") or {})
        live = pending_spec(pending_by_id[txid], spec["submit_order"])
        if _wire_identity(live) != _wire_identity(spec):
            # A transaction id changing payload is not a safe ownership match.
            continue
        out.append(spec)
    return sorted(out, key=lambda s: s["submit_order"])


def _result(row: dict, source: str) -> dict:
    status = str(row.get("status") or "unknown")
    if status == "complete":
        status = "completed"
    return {"status": status, "type": row.get("type"),
            "notes": (row.get("metadata") or {}).get("notes"),
            "status_updated": row.get("status_updated"), "source": source}


def _settled_status(txid: str, league_id: str, roster_id: int,
                    week: int) -> dict:
    from robo import sleeper_write as sw
    try:
        for row in sw.waiver_claim_history(roster_id, league_id):
            if str(row.get("transaction_id")) == str(txid):
                return _result(row, "graphql")
    except Exception:
        pass
    for wk in {max(1, int(week) - 1), int(week)}:
        try:
            for row in api.transactions(league_id, wk) or []:
                if str(row.get("transaction_id")) == str(txid):
                    return _result(row, "rest")
        except Exception:
            continue
    return {"status": "unresolved"}


def _refresh_missing(doc: dict, pending_by_id: dict[str, dict],
                     league_id: str, roster_id: int, week: int) -> list[dict]:
    settled = []
    for txid in list(doc.get("active", {})):
        if txid in pending_by_id:
            continue
        saved = doc["active"].pop(txid)
        result = _settled_status(txid, league_id, roster_id, week)
        row = {**saved, "transaction_id": txid, "result": result,
               "left_pending_at": time.time()}
        doc.setdefault("history", []).append(row)
        settled.append(row)
    doc["history"] = (doc.get("history") or [])[-250:]
    return settled


def _valuation_vintage(ctx: dict) -> str | None:
    """The valuation this context is priced on.

    `moves._context` fills `_expected_table` LAZILY, so the same live facts
    fingerprint differently depending on whether pricing has run yet. The
    submitting caller has priced; the ten-minute maintenance check has not, so
    the two could never agree and the cheap "nothing changed" path -- the whole
    reason the check is cheap -- never fired. Resolve the vintage the same way
    from either side.
    """
    table = ctx.get("_expected_table")
    if table is None:
        from robo import expected
        table = expected.load()
    return (table or {}).get("computed")


def context_fingerprint(ctx: dict, relevant=()) -> str:
    ids = {str(pid) for pid in relevant if pid is not None}
    eligibility = ctx.get("eligibility") or {}
    body = {
        "league_id": ctx.get("league_id"), "week": ctx.get("week"),
        "players": sorted(str(x) for x in (ctx.get("roster", {}).get("players") or [])),
        "starters": sorted(str(x) for x in (ctx.get("roster", {}).get("starters") or [])),
        "reserve": sorted(str(x) for x in (ctx.get("roster", {}).get("reserve") or [])),
        "open": (ctx.get("slots") or {}).get("open"), "faab": ctx.get("faab"),
        "valuation": _valuation_vintage(ctx),
        "eligibility": {pid: eligibility.get(pid) for pid in sorted(ids)},
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def live_fingerprint(league_id: str = LEAGUE_ID_2026, relevant=()) -> str:
    from robo import expected, season
    ids = {str(pid) for pid in relevant if pid is not None}
    roster = season.mine(league_id)
    ctx = {"league_id": league_id, "week": season.current_week(),
           "roster": roster, "slots": season.slots(league_id),
           "faab": season.faab_left(league_id),
           "_expected_table": expected.load(),
           "eligibility": season.transaction_states(ids, league_id) if ids else {}}
    return context_fingerprint(ctx, ids)


def inspect(league_id: str, roster_id: int, week: int) -> dict:
    from robo import sleeper_write as sw
    pending = sw.pending_waiver_claims(roster_id, league_id)
    by_id = {str(row["transaction_id"]): row for row in pending}
    doc = load()
    settled = _refresh_missing(doc, by_id, league_id, roster_id, week)
    owned = _active_specs(doc, by_id)
    matched_ids = {
        str(txid) for txid, saved in (doc.get("active") or {}).items()
        if txid in by_id
        and _wire_identity(pending_spec(by_id[txid]))
        == _wire_identity(normalize_spec(saved.get("spec") or {}))
    }
    # An id recorded in the manifest with a different live payload is not
    # ownership. Treat it exactly like any other foreign/manual claim.
    foreign = [row for txid, row in by_id.items() if txid not in matched_ids]
    if settled:
        _write(doc)
        for row in settled:
            _event("settled", transaction_id=row["transaction_id"],
                   result=row.get("result"), spec=row.get("spec"))
    return {"state": doc, "pending": pending, "pending_by_id": by_id,
            "owned": owned, "foreign": foreign,
            "settled": settled}


def _alert(message: str, key: str) -> None:
    try:
        from robo import alerts
        alerts.blast(message, key=key)
    except Exception:
        pass


def _cancel(txid: str, saved: dict, doc: dict, league_id: str,
            roster_id: int, reason: str) -> dict:
    from robo import sleeper_write as sw
    row = sw.cancel_waiver_claim(txid, int(saved.get("leg") or 0), league_id)
    remaining = {str(x["transaction_id"])
                 for x in sw.pending_waiver_claims(roster_id, league_id)}
    if str(txid) in remaining:
        raise RuntimeError(f"cancelled claim {txid} remained pending")
    old = doc.get("active", {}).pop(txid, saved)
    doc.setdefault("history", []).append({**old, "transaction_id": txid,
                                           "result": {"status": "cancelled",
                                                      "reason": reason},
                                           "left_pending_at": time.time()})
    _write(doc)
    _event("cancelled", transaction_id=txid, reason=reason,
           spec=old.get("spec"), response=row)
    return row


def _submit(spec: dict, roster_id: int, league_id: str, source: str,
            fingerprint: str, doc: dict) -> dict:
    from robo import sleeper_write as sw
    before = {str(x["transaction_id"])
              for x in sw.pending_waiver_claims(roster_id, league_id)}
    drop = spec.get("drop_id")
    row = sw.submit_waiver_claim(
        {spec["add_id"]: roster_id}, {drop: roster_id} if drop else {},
        int(spec["bid"]), league_id)
    txid = str(row.get("transaction_id") or "")
    if not txid:
        raise RuntimeError("Sleeper returned no transaction_id for submitted claim")
    if txid in before:
        raise RuntimeError(f"Sleeper reused pre-existing transaction_id {txid}")
    saved = {"transaction_id": txid, "leg": int(row.get("leg") or 0),
             "source": source, "fingerprint": fingerprint,
             "submitted_at": time.time(), "spec": spec}
    doc.setdefault("active", {})[txid] = saved
    doc["last_fingerprint"] = fingerprint
    _write(doc)  # persist ownership before another write can happen
    live = {str(x["transaction_id"]): x
            for x in sw.pending_waiver_claims(roster_id, league_id)}
    if txid not in live or _wire_identity(pending_spec(live[txid])) != _wire_identity(spec):
        raise RuntimeError(f"pending read-back did not match transaction {txid}")
    _event("submitted", transaction_id=txid, source=source, spec=spec)
    return saved


def portfolio_summary(specs: list[dict]) -> dict:
    """Group capacities and true worst-case spend for UI/audit consumers."""
    groups = {}
    for raw in specs:
        spec = normalize_spec(raw)
        gid = spec["group_id"]
        group = groups.setdefault(gid, {"group_id": gid, "kind": spec["kind"],
                                        "capacity": max(1, int(spec.get("capacity") or 1)),
                                        "bids": [], "claims": 0})
        group["capacity"] = max(group["capacity"],
                                max(1, int(spec.get("capacity") or 1)))
        group["bids"].append(int(spec.get("bid") or 0))
        group["claims"] += 1
    rows = []
    for group in groups.values():
        group["exposure"] = sum(sorted(group.pop("bids"), reverse=True)
                                [:group["capacity"]])
        rows.append(group)
    rows.sort(key=lambda x: ({"open": 0, "skill": 1, "def": 2}
                             .get(x["kind"], 9), x["group_id"]))
    return {"groups": rows,
            "worst_case_exposure": sum(x["exposure"] for x in rows)}


def reconcile(desired: list[dict], *, league_id: str, roster_id: int,
              week: int, source: str, apply: bool,
              fingerprint: str | None = None) -> dict:
    """Make Sleeper's bot-owned pending queue equal ``desired``.

    This is transactional-style rather than falsely atomic: every mutation is
    verified, ownership is persisted after each submission, and a partial
    replacement restores the old portfolio only while the live roster
    fingerprint proves settlement or another roster write did not intervene.
    """
    desired = [normalize_spec(s, i) for i, s in enumerate(desired)]
    desired.sort(key=lambda s: s["submit_order"])
    add_ids = [s["add_id"] for s in desired]
    if any(pid in {"", "None"} for pid in add_ids):
        raise ValueError("every waiver claim requires an add_id")
    # UNIQUE ON THE PAIR, NOT ON THE ADD. One player claimed twice with
    # DIFFERENT drops is a fallback -- take him into the open slot if there is
    # one, otherwise take him by dropping the incumbent -- and refusing it
    # forced the planner to give each man to exactly one group, which handed
    # the later group the leftovers rather than the players it valued. Sleeper
    # settles at most one of them and charges FAAB only on the one that
    # executes. The same add with the same drop twice is still a bug.
    pairs = [(s["add_id"], s["drop_id"]) for s in desired]
    if len(pairs) != len(set(pairs)):
        raise ValueError("the desired waiver portfolio repeats an add/drop pair")
    relevant = {s.get("add_id") for s in desired} | {s.get("drop_id") for s in desired}
    snap = inspect(league_id, roster_id, week)
    doc, old = snap["state"], snap["owned"]
    result = {"changed": False, "applied": False, "blocked": None,
              "foreign": snap["foreign"], "pending_before": snap["pending"],
              "desired": desired, "cancelled": [], "submitted": [],
              "restored": [], "settled": snap["settled"]}
    if snap["foreign"]:
        result["blocked"] = "foreign pending waiver claim(s) require review"
        _event("blocked_foreign", count=len(snap["foreign"]), source=source)
        if apply:
            _alert("WAIVER AUTOMATION BLOCKED: an unowned pending claim is present.",
                   "waiver-foreign-claim")
        return result
    if [_identity(x) for x in old] == [_identity(x) for x in desired]:
        result["pending_after"] = snap["pending"]
        return result
    result["changed"] = True
    if not apply:
        return result
    from robo import value
    if not value.may_submit():
        result["blocked"] = value.GATE_MESSAGE
        return result

    start_fp = live_fingerprint(league_id, relevant)
    fingerprint = fingerprint or start_fp
    old_saved = [doc["active"][txid] for txid in list(doc.get("active", {}))
                 if txid in snap["pending_by_id"]]
    old_ids = {str(x["transaction_id"]) for x in old_saved}
    new_ids = []
    try:
        for saved in sorted(old_saved,
                            key=lambda x: (x.get("spec") or {}).get("submit_order", 0)):
            txid = str(saved["transaction_id"])
            _cancel(txid, saved, doc, league_id, roster_id,
                    "portfolio replaced")
            result["cancelled"].append(txid)
        for spec in desired:
            saved = _submit(spec, roster_id, league_id, source, fingerprint, doc)
            new_ids.append(saved["transaction_id"])
            result["submitted"].append(saved)
        result["applied"] = True
        result["pending_after"] = inspect(league_id, roster_id, week)["pending"]
        _event("reconciled", source=source, cancelled=result["cancelled"],
               submitted=new_ids, fingerprint=fingerprint,
               desired=desired, pending_before=snap["pending"],
               pending_after=result["pending_after"])
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        # Remove the partial replacement first.
        partial_ids = [str(txid) for txid in list(doc.get("active", {}))
                       if str(txid) not in old_ids]
        rollback_cancelled = []
        for txid in partial_ids:
            saved = doc.get("active", {}).get(str(txid))
            if not saved:
                continue
            try:
                _cancel(str(txid), saved, doc, league_id, roster_id,
                        "partial replacement rollback")
                rollback_cancelled.append(str(txid))
            except Exception as cancel_exc:
                result.setdefault("rollback_errors", []).append(str(cancel_exc))
        # Restore only when the same live facts still hold. A settlement race
        # demands a fresh plan; blindly restoring would act on a roster that no
        # longer exists.
        try:
            from robo import sleeper_write as sw
            live_rows = sw.pending_waiver_claims(roster_id, league_id)
            live_by_id = {str(x["transaction_id"]): x for x in live_rows}
            active_now = set(live_by_id)
            old_uncancelled = old_ids - set(result["cancelled"])
            new_unaccounted = set(partial_ids) - set(rollback_cancelled)
            settlement_started = bool((old_uncancelled - active_now)
                                      or (new_unaccounted - active_now))
            matching_old = {
                txid for txid in old_uncancelled & active_now
                if _wire_identity(pending_spec(live_by_id[txid]))
                == _wire_identity(normalize_spec(
                    next(x for x in old_saved
                         if str(x["transaction_id"]) == txid)["spec"]))
            }
            foreign_now = active_now - matching_old
            safe = (live_fingerprint(league_id, relevant) == start_fp
                    and not settlement_started
                    and not result.get("rollback_errors")
                    and not foreign_now)
        except Exception:
            safe, settlement_started, matching_old = False, True, set()
        result["settlement_started"] = settlement_started
        if safe:
            for saved in old_saved:
                if str(saved["transaction_id"]) in matching_old:
                    continue
                try:
                    restored = _submit(normalize_spec(saved["spec"]), roster_id,
                                       league_id, "rollback", start_fp, doc)
                    result["restored"].append(restored)
                except Exception as restore_exc:
                    result.setdefault("rollback_errors", []).append(str(restore_exc))
                    break
        _event("reconcile_failed", source=source, error=result["error"],
               restored=[x.get("transaction_id") for x in result["restored"]],
               rollback_errors=result.get("rollback_errors") or [])
        _alert("WAIVER PORTFOLIO RECONCILIATION FAILED; the slate needs review.",
               "waiver-reconcile-failed")
        return result


def cancel_owned(transaction_ids: list[str], *, league_id: str, roster_id: int,
                 week: int, reason: str, apply: bool) -> dict:
    snap = inspect(league_id, roster_id, week)
    wanted = {str(x) for x in transaction_ids}
    owned = snap["state"].get("active") or {}
    out = {"cancelled": [], "foreign": snap["foreign"], "applied": False}
    if snap["foreign"] or not apply:
        return out
    from robo import value
    if not value.may_submit():
        return out
    for txid in sorted(wanted):
        if txid not in owned:
            continue
        _cancel(txid, owned[txid], snap["state"], league_id, roster_id, reason)
        out["cancelled"].append(txid)
    out["applied"] = bool(out["cancelled"])
    return out


def status(league_id: str = LEAGUE_ID_2026, roster_id: int = 4,
           week: int | None = None) -> dict:
    from robo import season
    week = season.current_week() if week is None else int(week)
    snap = inspect(league_id, roster_id, week)
    summary = portfolio_summary(snap["owned"])
    return {"owned": snap["owned"], "foreign": snap["foreign"],
            "pending": snap["pending"], "settled": snap["settled"],
            "state": snap["state"], "events": recent_events(), **summary}
