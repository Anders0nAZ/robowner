"""Immutable records of ordinary ROS waiver evaluations."""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from robo import DATA

AUDIT_DIR = DATA / "waiver_events"
NEWS_STATE = DATA / "news_watch.json"
SCHEMA = 3


def _jsonable(value):
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(type(value).__name__)


def record(ctx: dict, plans: list[dict], result: dict) -> Path | None:
    core = {
        "week": ctx.get("week"), "mode": ctx.get("mode"),
        "league_id": ctx.get("league_id"),
        "valuation_computed": ((ctx.get("_expected_table") or {}).get("computed")),
        "roster": sorted(str(p) for p in (ctx.get("roster", {}).get("players") or [])),
        "claims": [(c.get("add") or {}).get("player_id")
                   for s in plans for c in s.get("claims") or []],
    }
    fingerprint_input = {"audit_schema": SCHEMA, **core}
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_input, sort_keys=True).encode()).hexdigest()[:16]
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    existing = list(AUDIT_DIR.glob(f"*_{fingerprint}.json"))
    if existing:
        return existing[-1]
    market = {}
    try:
        state = json.loads(NEWS_STATE.read_text(encoding="utf-8"))
        market = {"captured_at": state.get("last_poll") or state.get("polled_at"),
                  "trending_top": state.get("trending_top") or [],
                  "pricing_status": "archived only; not calibrated for bid pricing"}
    except (OSError, ValueError, TypeError):
        market = {"pricing_status": "snapshot unavailable; not used for bid pricing"}
    doc = {"schema": SCHEMA, "at": time.time(), "fingerprint": fingerprint,
           **core, "faab_left": ctx.get("faab"),
           "worst_case_faab": ctx.get("_claim_exposure", 0),
           "defence_group_error": ctx.get("_defence_group_error"),
           "hours_to_kickoff": ctx.get("hours_to_kickoff"),
           "sequence_basis": ctx.get("_sequence_basis"),
           "free_plans": ctx.get("_sequence_free_plans") or [],
           "free_audit": ctx.get("_sequence_free_audit") or {},
           "claims_audit": result.get("decision_audit") or {},
           "plans": plans, "gated": result.get("gated"),
           "blackout": result.get("blackout"),
           "control_block": result.get("control_block"),
           "roster_control_checks": result.get("roster_control_checks") or [],
           "market_snapshot": market}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = AUDIT_DIR / f"{stamp}_{fingerprint}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=_jsonable), encoding="utf-8")
    tmp.replace(path)
    _prune()
    return path


# These are ~800KB each and nothing was deleting them. That was survivable
# while the only writer was the Tuesday slate and the Wednesday move pass --
# about two a week. The news channel now rebuilds the full ROS portfolio on
# every pulse that carries an event, so the rate went to one per pulse: 72 a
# day, 56MB a day, into a directory git is tracking. Retention belongs here
# either way; a record that grows without bound is not a record, it is a leak.
KEEP_EVENTS = 200


def _prune(keep: int = KEEP_EVENTS) -> int:
    """Drop the oldest evaluations beyond `keep`. Never raises."""
    try:
        paths = sorted(AUDIT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return 0
    dropped = 0
    for path in paths[:-keep] if len(paths) > keep else []:
        try:
            path.unlink()
            dropped += 1
        except OSError:
            continue
    return dropped


def events(limit: int = 100) -> list[dict]:
    out = []
    for path in AUDIT_DIR.glob("*.json") if AUDIT_DIR.exists() else []:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(row, dict) and row.get("fingerprint"):
            row["_path"] = str(path)
            out.append(row)
    out.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
    return out[:limit]
