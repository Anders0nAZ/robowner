"""Validated handoffs between data producers and unattended decision jobs.

The receipt records the inputs a valuation actually followed. A successful task
exit, or a freshly copied model file, is insufficient evidence that the roster
numbers were built from the current inputs.

INPUTS ARE COMPARED BY CONTENT, NOT BY FILE TIME. Several producers rewrite a
file whose substance has not moved -- the scout queue republishes the whole
verdict store with a new `written` stamp and a new `judged_at` for a re-judged
man who kept the same verdict, and the lines artifact carries a fetch time on
every row. Comparing mtimes called every one of those "inputs changed" and sent
the Tuesday and Wednesday jobs off to rebuild a valuation that was already
current. The fingerprint drops the bookkeeping stamps and hashes the rest.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from robo import DATA, MODEL_DATA, RAW, ROOT, season

STATE = DATA / "dependency_state.json"
MAX_VALUE_AGE_S = 6 * 3600
OUTPUTS = {"expected": DATA / "expected.json", "ros": DATA / "ros.json"}

EXPECTED_INPUTS = {
    "projections": RAW / "projections_2026.json",
    "injuries": DATA / "injuries_espn.json",
    "model_week": DATA / "model_week.json",
    "model_horizon": DATA / "model_horizon.json",
    "lines": MODEL_DATA / "lines" / "current.json",
    "schedules": MODEL_DATA / "parquet" / "schedules.parquet",
    "news": DATA / "news_verdicts.json",
}
ROS_INPUTS = {**EXPECTED_INPUTS,
              "board": DATA / "board_2026.csv",
              "playoff_odds": DATA / "playoff_odds.json"}

# When a file was written, not what it says. Stripped at any depth before
# hashing, so a republish of identical content keeps its fingerprint.
VOLATILE_KEYS = frozenset({
    "written", "written_iso", "judged_at", "judged_now", "reused",
    "generated_utc", "simulated_utc", "updated_utc", "fetched_utc", "computed",
})

_memo: dict[tuple, str] = {}


def _strip(node):
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in VOLATILE_KEYS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def fingerprint(path: Path) -> str | None:
    """Content hash of one input, bookkeeping stamps removed. None if absent."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key in _memo:
        return _memo[key]
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    body = raw
    if path.suffix == ".json":
        try:
            body = json.dumps(_strip(json.loads(raw)), sort_keys=True).encode("utf-8")
        except ValueError:
            pass
    digest = hashlib.sha256(body).hexdigest()[:20]
    _memo[key] = digest
    return digest


def _file_stamp(path: Path) -> str | None:
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return None


def _inputs(paths: dict[str, Path]) -> dict[str, str | None]:
    return {name: fingerprint(path) for name, path in paths.items()}


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(doc: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def record(kind: str) -> None:
    """Record a completed expected/ROS build immediately after its output lands."""
    if kind not in OUTPUTS:
        raise ValueError(kind)
    target = OUTPUTS[kind]
    value = _read(target)
    if not value or not value.get("players"):
        raise ValueError(f"{target.name} is missing or empty")
    doc = _read(STATE)
    doc[kind] = {"at": time.time(), "week": int(value.get("week") or 0),
                 "output": _file_stamp(target),
                 "inputs": _inputs(EXPECTED_INPUTS if kind == "expected" else ROS_INPUTS)}
    _write(doc)


def model_ready(week: int | None = None) -> tuple[bool, str]:
    """Both model artifacts validate for this season and week."""
    from robo import LEAGUE_ID_2026, model_proj
    week = season.current_week() if week is None else week
    weekly, weekly_why = model_proj.week_projections(week, season.SEASON, LEAGUE_ID_2026)
    if not weekly:
        return False, f"model week unavailable: {weekly_why}"
    horizon, horizon_why = model_proj.horizon_projections(season.SEASON, LEAGUE_ID_2026)
    if not horizon:
        return False, f"model horizon unavailable: {horizon_why}"
    return True, "ready"


def check(kind: str, max_age_s: int = MAX_VALUE_AGE_S,
          week: int | None = None) -> tuple[bool, str]:
    """Check the output and all sources against its completed build receipt."""
    if kind not in OUTPUTS:
        raise ValueError(kind)
    target = OUTPUTS[kind]
    value = _read(target)
    if not value or not isinstance(value.get("players"), dict) or not value["players"]:
        return False, f"{kind} output missing or unreadable"
    week = season.current_week() if week is None else week
    if int(value.get("week") or 0) != int(week):
        return False, f"{kind} output is for another week"
    if str(value.get("season")) != str(season.SEASON):
        return False, f"{kind} output is for another season"
    ok, why = model_ready(week)
    if not ok:
        return False, why
    entry = _read(STATE).get(kind) or {}
    if not entry or entry.get("output") != _file_stamp(target):
        return False, f"{kind} has no matching completed build receipt"
    age = time.time() - float(entry.get("at") or 0)
    if age < -60 or age > max_age_s:
        return False, f"{kind} build is {age / 3600:.1f}h old"
    paths = EXPECTED_INPUTS if kind == "expected" else ROS_INPUTS
    current = _inputs(paths)
    if entry.get("inputs") != current:
        changed = [name for name in paths if (entry.get("inputs") or {}).get(name) != current[name]]
        return False, f"{kind} inputs changed: {', '.join(changed)}"
    return True, "ready"


def hold_reason(week: int | None = None) -> str:
    """Empty when a valuation-priced write may go ahead, else why it is held."""
    bad = [why for ok, why in (check(k, week=week) for k in OUTPUTS) if not ok]
    return "; ".join(bad)


def rebuild_values(league_id: str | None = None) -> str:
    """Rebuild expected + ros from the inputs on disk, and receipt both.

    The smallest repair: when the model artifacts already validate, a stale or
    missing valuation needs only these two builds, not a re-pull, capture and
    export. The caller holds DecisionRun.
    """
    from robo import LEAGUE_ID_2026, expected, marginal, ros
    league_id = league_id or LEAGUE_ID_2026
    d = expected.build(league_id=league_id)
    expected.save(d)
    record("expected")
    r = ros.build(league_id=league_id)
    ros.CACHE.write_text(json.dumps(r), encoding="utf-8")
    record("ros")
    marginal.board.cache_clear()
    return f"{len(d['players'])} expected, {len(r['players'])} ros"


def ensure_values(*, wait_s: int = 300, repair_timeout_s: int = 900) -> tuple[bool, str]:
    """Wait for a running producer, repair the smallest chain, then revalidate.

    Call before acquiring DecisionRun. Repair order: if the model artifacts
    validate, rebuild the valuation in-process under the lock; otherwise run a
    dry cascade, which re-pulls, captures, exports and rebuilds without writing
    to Sleeper. The caller revalidates under its own lock before any write.
    """
    from robo.runlock import LOCK, DecisionRun, RunBusy, pid_alive

    reason = hold_reason()
    if not reason:
        return True, "ready"
    until = time.monotonic() + max(0, wait_s)
    while time.monotonic() < until:
        owner = _read(LOCK)
        if not (owner.get("pid") and pid_alive(owner["pid"])):
            break
        time.sleep(min(5, max(0, until - time.monotonic())))
        reason = hold_reason()
        if not reason:
            return True, "upstream completed while waiting"

    if model_ready()[0]:
        try:
            with DecisionRun("dependency repair", wait_s=wait_s):
                reason = hold_reason()
                if not reason:
                    return True, "upstream completed while waiting"
                rebuild_values()
        except RunBusy as exc:
            return False, f"repair could not take the decision lock: {exc}"
        except Exception as exc:
            return False, f"valuation rebuild failed: {type(exc).__name__}: {exc}"[:240]
        reason = hold_reason()
        if not reason:
            return True, "valuation rebuilt"

    try:
        proc = subprocess.run([sys.executable, "-m", "robo.cascade"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=repair_timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"upstream repair could not finish: {exc}"
    reason = hold_reason()
    if not reason:
        return True, "upstream repaired"
    return False, f"upstream repair exited {proc.returncode}; {reason}"
