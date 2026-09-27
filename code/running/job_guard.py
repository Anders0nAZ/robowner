"""Reconcile scheduled jobs and retry missed work while its window is open.

Runs every five minutes and at sign-in (RobonerJobGuard). Three rules keep it
from becoming a second, unaccountable scheduler:

  * A MISSING task is restored from its checked-in XML. A DISABLED one is left
    alone and reported: `Disable-ScheduledTask` is the documented kill switch,
    and a guard that re-enabled it would make the bot impossible to stop.
  * The schedule is read from the same XML the task is installed from, so the
    guard's idea of when a job was due cannot drift from Task Scheduler's.
  * Each missed or failed run is retried at most MAX_ATTEMPTS times, then the
    guard stops and says so. A retry reruns the whole job, and for the daily
    refresh that means republishing and a responder restart.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from robo import DATA, ROOT
from robo import job_run, ops_alerts
from robo.runlock import pid_alive

STATE = DATA / "job_guard_state.json"
TASK_XML = ROOT / "ops" / "inseason-tasks"
TASKS = (
    "RobonerAutostart", "RobonerWatchdog", "RobonerJobGuard",
    "RobonerRefresh", "RobonerNewsWatch", "RobonerRoster",
    "RobonerLineup", "RobonerMoves", "RobonerWaivers",
    "RobonerPreKickDaily", "RobonerModelCaptureDaily", "RobonerScorecard",
)
JOB_TASK = {
    "model-daily": "RobonerModelCaptureDaily", "prekick-plan": "RobonerPreKickDaily",
    "refresh": "RobonerRefresh", "roster": "RobonerRoster",
    "lineup": "RobonerLineup", "newswatch": "RobonerNewsWatch",
    "moves": "RobonerMoves", "waivers": "RobonerWaivers",
    "scorecard": "RobonerScorecard",
}
# The last moment a late run is still the run that was meant. After it, a
# replay would act on a wire the league has already moved past: Wednesday's
# moves compete with everyone else's post-waiver adds, and Tuesday's slate
# must be in before Sleeper processes waivers just after midnight.
CUTOFF = {"moves": (12, 0), "waivers": (23, 30)}
# The pulse repeats every 20 minutes; it counts as missed after 50.
NEWSWATCH_GRACE_MIN = 50
RETRY_MINUTES = (5, 15, 30)
MAX_ATTEMPTS = 3
SCHEDULER_MISSES_ALERT = 3
PS_TASKS = (
    "Get-ScheduledTask | Where-Object { $_.TaskName -like 'Roboner*' } | "
    "ForEach-Object { [pscustomobject]@{name=$_.TaskName; state=[string]$_.State} } | "
    "ConvertTo-Json -Compress"
)
_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _read() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(doc: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def _tasks() -> dict[str, str]:
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", PS_TASKS],
                            capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError("could not read Windows Task Scheduler")
    rows = json.loads(result.stdout.strip() or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    return {row["name"]: row["state"] for row in rows}


def _install(name: str) -> bool:
    path = ROOT / "ops" / "InstallRobonerTasks.ps1"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy",
                             "Bypass", "-File", str(path), "-Name", name],
                            capture_output=True, text=True, timeout=60)
    return result.returncode == 0


def _start(name: str) -> bool:
    result = subprocess.run(["schtasks.exe", "/Run", "/TN", name],
                            capture_output=True, text=True, timeout=20)
    return result.returncode == 0


def schedule(task: str, xml_dir: Path | None = None) -> list[tuple[int, int, frozenset]]:
    """(hour, minute, weekdays) for each calendar trigger in the task's XML.

    Weekdays are Python's (Monday=0); an empty set means every day. Only the
    time of day is read from StartBoundary -- its date is when the trigger was
    first registered, not when it next fires.
    """
    root = ET.parse((xml_dir or TASK_XML) / f"{task}.xml").getroot()
    out = []
    for trig in root.iterfind("t:Triggers/t:CalendarTrigger", _NS):
        start = trig.findtext("t:StartBoundary", default="", namespaces=_NS)
        hh, mm = start.split("T", 1)[1][:5].split(":")
        days = trig.find("t:ScheduleByWeek/t:DaysOfWeek", _NS)
        weekdays = frozenset(
            _DAYS.index(el.tag.split("}", 1)[1]) for el in days) if days is not None \
            else frozenset()
        out.append((int(hh), int(mm), weekdays))
    return out


def _due(job: str, now: datetime) -> tuple[float | None, float | None]:
    """(scheduled epoch, last safe start epoch) of the run due most recently
    today. (None, None) when nothing has come due yet today."""
    if job == "newswatch":
        return (now - timedelta(minutes=NEWSWATCH_GRACE_MIN)).timestamp(), None
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fired = [today.replace(hour=h, minute=m)
             for h, m, days in schedule(JOB_TASK[job])
             if not days or now.weekday() in days]
    fired = [t for t in fired if t <= now]
    if not fired:
        return None, None
    due = max(fired)
    cutoff = None
    if job in CUTOFF:
        h, m = CUTOFF[job]
        cutoff = due.replace(hour=h, minute=m).timestamp()
    return due.timestamp(), cutoff


def _complete(job: str, due: float) -> bool:
    rec = job_run.receipt(job)
    if rec.get("status") != "success" or float(rec.get("started") or 0) < due:
        return False
    if job == "newswatch":
        try:
            state = json.loads((DATA / "news_watch.json").read_text(encoding="utf-8"))
            return float(state.get("last_poll") or state.get("last_attempt") or 0) >= due
        except (OSError, ValueError):
            return False
    if job == "refresh":
        from robo.cascade import refresh_completed_today
        return refresh_completed_today()
    return True


def _alert(key: str, message: str, tier: str = ops_alerts.ACTION) -> None:
    """ACTION reaches Side Chat; INFO is logged only (see ops_alerts)."""
    try:
        ops_alerts.send(key, message, tier)
    except Exception as exc:
        print(f"Side Chat alert failed: {type(exc).__name__}: {exc}", flush=True)


def tick(*, now: datetime | None = None, repair: bool = True) -> dict:
    now = now or datetime.now()
    state = _read()
    state.setdefault("failures", {})
    state["checked_at"] = time.time()
    held = job_run.paused()
    if held:
        state.update(status="paused", paused=held, actions=[])
        _write(state)
        return state
    state.pop("paused", None)
    actions = []
    try:
        installed = _tasks()
    except Exception as exc:
        misses = int(state.get("scheduler_misses") or 0) + 1
        state.update(status="scheduler_unreadable", error=str(exc), actions=[],
                     scheduler_misses=misses)
        _write(state)
        # One failed read is usually a slow PowerShell start; three in a row
        # (fifteen minutes) means recovery itself is down.
        _alert("scheduler-unreadable",
               "Roboner cannot read Windows Task Scheduler; scheduled recovery is not running.",
               ops_alerts.ACTION if misses >= SCHEDULER_MISSES_ALERT else ops_alerts.INFO)
        return state
    state.pop("error", None)
    state["scheduler_misses"] = 0
    disabled = set()
    for name in TASKS:
        task_state = installed.get(name)
        if task_state is None:
            actions.append(f"{name}: missing")
            if repair and _install(name):
                actions.append(f"{name}: restored")
                _alert(f"restored-{name}", f"{name} was missing and was restored.",
                       ops_alerts.INFO)
            elif repair:
                _alert(f"task-{name}", f"Roboner could not restore the {name} scheduled task.")
        elif task_state.lower() == "disabled":
            disabled.add(name)
            actions.append(f"{name}: disabled, left alone")
    # Once per task being switched off, not every tick it stays off: a disable
    # is usually deliberate, and saying so again every six hours is noise.
    for name in sorted(disabled - set(state.get("disabled") or [])):
        _alert(f"disabled-{name}",
               f"{name} is disabled, so it will not run. Re-enable it if that was not "
               f"intended; StopRoboner.bat is the way to pause everything.")
    state["disabled"] = sorted(disabled)
    for job, task in JOB_TASK.items():
        if task in disabled:
            continue
        due, cutoff = _due(job, now)
        if due is None:
            continue
        failure = state["failures"].get(job) or {}
        if failure and failure.get("due") != due and job != "newswatch":
            failure = {}
        if _complete(job, due):
            if state["failures"].pop(job, None):
                actions.append(f"{job}: recovered")
                _alert(f"recovered-{job}-{int(due)}",
                       f"Roboner {job} recovered and completed after a retry.",
                       ops_alerts.INFO)
            continue
        if cutoff is not None and now.timestamp() >= cutoff:
            actions.append(f"{job}: missed safe window")
            _alert(f"missed-{job}-{int(due)}",
                   f"Roboner missed the {job} window today; no late action was submitted.")
            continue
        rec = job_run.receipt(job)
        if rec.get("status") == "running" and pid_alive(rec.get("pid") or 0):
            actions.append(f"{job}: waiting for running producer")
            continue
        count = int(failure.get("count") or 0)
        if count >= MAX_ATTEMPTS:
            actions.append(f"{job}: gave up after {count} retries")
            _alert(f"gave-up-{job}-{int(due)}",
                   f"Roboner retried {job} {count} times without a validated result "
                   f"and has stopped retrying it; it needs a look.")
            state["failures"][job] = failure
            continue
        if now.timestamp() < float(failure.get("next_retry") or 0):
            continue
        if not repair:
            actions.append(f"{job}: would retrigger")
            continue
        # A start that fails spends an attempt like a run that fails, so the
        # give-up alert covers it rather than a message per refusal.
        started = _start(task)
        count += 1
        state["failures"][job] = {
            "due": due, "count": count,
            "next_retry": now.timestamp() + 60 * RETRY_MINUTES[min(count - 1, 2)],
            "last_attempt": now.timestamp(),
        }
        if started:
            actions.append(f"{job}: retriggered ({count}/{MAX_ATTEMPTS})")
        else:
            actions.append(f"{job}: could not start {task} ({count}/{MAX_ATTEMPTS})")
        _alert(f"retry-{job}-{int(due)}-{count}",
               f"{job}: {'retriggered' if started else 'could not start'} "
               f"{task}, attempt {count}/{MAX_ATTEMPTS}.", ops_alerts.INFO)
    state.update(status="repairing" if actions else "healthy", actions=actions)
    _write(state)
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = tick(repair=not args.dry_run)
    for action in result.get("actions") or []:
        print(action)
    if result["status"] == "scheduler_unreadable":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
