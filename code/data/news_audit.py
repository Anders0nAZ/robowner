"""Read and explain immutable news-pulse decision records.

The watcher owns the evidence and writes one JSON record whenever a pulse
actually triggers a re-evaluation.  This module is deliberately a reader: it
does not call Sleeper, rebuild projections, or make a transaction decision.
That keeps the audit UI faithful to what happened at the time instead of
silently recomputing an answer from today's roster or projections.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from robo import DATA

AUDIT_DIR = DATA / "news_events"
STATE = DATA / "news_watch.json"


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def events(limit: int | None = 100, audit_dir: Path = AUDIT_DIR) -> list[dict]:
    """Newest-first valid event records. A corrupt record cannot sink the UI."""
    out = []
    for path in audit_dir.glob("*.json") if audit_dir.exists() else []:
        doc = _read(path)
        if not isinstance(doc, dict) or not doc.get("fingerprint"):
            continue
        doc = dict(doc)
        doc["_path"] = str(path)
        doc["_report_path"] = str(path.with_suffix(".txt"))
        out.append(doc)
    out.sort(key=lambda d: float(d.get("at") or 0), reverse=True)
    return out if limit is None else out[:limit]


def state() -> dict:
    return _read(STATE, {}) or {}


def proposals(doc: dict) -> list[dict]:
    action = doc.get("action") or {}
    out = list(action.get("free_proposals") or [])
    out.extend(c for slate in action.get("claim_proposals") or []
               for c in slate.get("claims") or [])
    return out


def submitted(doc: dict) -> list[dict]:
    action = doc.get("action") or {}
    return list(action.get("free_submitted") or []) + list(action.get("claims_submitted") or [])


def player_index(doc: dict, fallback: dict | None = None) -> dict[str, dict]:
    """Names frozen in the event first; current data is a legacy fallback only."""
    out = dict(fallback or {})
    for e in doc.get("events") or []:
        out[str(e.get("player_id"))] = {
            "name": e.get("name") or str(e.get("player_id")),
            "pos": e.get("pos"), "team": e.get("team")}
    action = doc.get("action") or {}
    for pid, row in (action.get("event_deltas") or {}).items():
        if row.get("name"):
            out[str(pid)] = {"name": row.get("name"), "pos": row.get("pos"),
                             "team": row.get("team")}
    for row in action.get("candidate_checks") or action.get("rejections") or []:
        if row.get("name"):
            out[str(row.get("player_id"))] = {
                "name": row.get("name"), "pos": row.get("pos"), "team": row.get("team")}
    for row in action.get("drop_checks") or []:
        if row.get("drop_name"):
            out[str(row.get("drop_id"))] = {
                "name": row.get("drop_name"), "pos": row.get("drop_pos"),
                "team": row.get("drop_team")}
    for p in proposals(doc):
        for key in ("add", "drop"):
            row = p.get(key) or {}
            if row.get("player_id") is not None:
                out[str(row["player_id"])] = row
    return out


def label(pid, index: dict[str, dict]) -> str:
    row = index.get(str(pid)) or {}
    return row.get("name") or str(pid)


def model_review_count(timing: dict) -> int:
    """Count advisory reads, including schema-1 records that stored only prose."""
    if "model_reviewed" in timing:
        return len(timing.get("model_reviewed") or [])
    m = re.search(r"(\d+)\s*/\s*\d+\s+model-reviewed",
                  str(timing.get("summary") or ""))
    return int(m.group(1)) if m else len(timing.get("advisory") or [])


def local_time(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%b %-d, %-I:%M:%S %p")
    except ValueError:  # Windows strftime does not support '-' modifiers.
        try:
            d = datetime.fromtimestamp(float(ts)).astimezone()
            return d.strftime("%b %d, %I:%M:%S %p").replace(" 0", " ")
        except Exception:
            return "unknown time"
    except Exception:
        return "unknown time"


def outcome(doc: dict) -> str:
    action = doc.get("action") or {}
    if submitted(doc):
        return "Submitted"
    if doc.get("source_errors"):
        return "Held on source failure"
    if proposals(doc):
        return "Proposal only" if doc.get("dry_run") or action.get("free_gated") else "Not submitted"
    deltas = action.get("event_deltas") or {}
    if not any(float(d.get("delta_ros") or 0) > 0 and d.get("causal_edge")
               for d in deltas.values()):
        return "No causal value change"
    return "No move cleared"


def narrative(doc: dict) -> list[str]:
    """Deterministic plain-English cascade, derived only from recorded facts."""
    action = doc.get("action") or {}
    triggers = doc.get("events") or []
    changes = [c for e in triggers for c in e.get("changes") or []]
    fields = {c.get("field") for c in changes}
    affected = action.get("event_deltas") or {}
    available = sum(bool(d.get("acquisition_candidate")) for d in affected.values())
    changed = [d for d in affected.values() if abs(float(d.get("delta_ros") or 0)) >= .005]
    causal_up = [d for d in affected.values()
                 if float(d.get("delta_ros") or 0) > 0 and d.get("causal_edge")]
    timing = doc.get("timing") or {}
    candidates = action.get("candidate_checks") or action.get("rejections") or []
    props = proposals(doc)

    trigger_detail = ("Every trigger was only a Sleeper news timestamp refresh; no status or "
                      "weekly projection changed." if fields == {"news_updated"} else
                      "The changed fields were " + ", ".join(sorted(str(x) for x in fields if x)) + ".")
    out = [f"The pulse saw {len(triggers)} trigger player(s). {trigger_detail}"]
    out.append(f"Timing review: {timing.get('summary') or 'no timing review recorded'}. "
               "Only deterministic timing can change availability; local-model interpretations are advisory.")
    out.append(f"The rebuild expanded those triggers to {len(affected)} same-team, same-position players. "
               f"{len(changed)} changed rest-of-season value and {len(causal_up)} had both a positive change "
               "and a recorded causal edge to a trigger.")
    if any("acquisition_candidate" in d for d in affected.values()):
        out.append(f"{available} affected player(s) were actually available to acquire; the other "
                   f"{len(affected) - available} were ours or another manager's and were context only. "
                   f"The transaction screen recorded {len(candidates)} candidate verdict(s).")
    elif affected:
        out.append("This older record predates frozen ownership labels; candidate rejections below are still the "
                   "players that were available at evaluation time.")
    if props:
        out.append(f"{len(props)} move proposal(s) cleared the causal, coverage, and apples-to-apples ROS gates. "
                   f"{len(submitted(doc))} submission(s) were recorded.")
    else:
        out.append("No move reached submission. " +
                   ("The code submission gate was closed." if action.get("free_gated") or action.get("claims_gated")
                    else "No candidate cleared every decision gate."))
    if doc.get("source_errors"):
        out.append("Source failures suppressed live authority: " + "; ".join(doc["source_errors"]))
    return out
