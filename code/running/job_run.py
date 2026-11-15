"""One exit-code-preserving entry point for scheduled Roboner jobs.

Task Scheduler only sees the final process. These receipts keep each command's
outcome and prevent a later successful command from hiding an earlier failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from robo import DATA, ROOT

RECEIPTS = DATA / "job_runs"
PAUSE = DATA / "automation_paused.json"

# NO PAUSE IS OPEN-ENDED (Nate, 2026-11-15: "there is no circumstance where I would
# want the bot paused indefinitely"). A pause given no end time ends after
# PAUSE_DEFAULT_H; no pause, however requested, outlasts PAUSE_MAX_H from when it
# was written. An open-ended StopRoboner.bat marker left by a hub restart ran no
# lineup, moves or newswatch from 11 to 15 Nov 2026 and emptied the DEF slot.
# RobonerWatchdog.ps1 (Test-Paused) applies the same two numbers.
PAUSE_DEFAULT_H = 2.0
PAUSE_MAX_H = 24.0

COMMANDS = {
    "refresh": [("refresh", "robo.refresh")],
    "newswatch": [("pulse", "robo.newswatch")],
    "roster": [("cascade", "robo.cascade", "--apply")],
    "lineup": [("cascade", "robo.cascade", "--apply")],
    "prekick": [("cascade", "robo.cascade", "--apply", "--pregame")],
    "moves": [("ir", "robo.ir", "--apply"),
              ("ros", "robo.moves", "--free", "--mode", "ros", "--apply")],
    "waivers": [("ir", "robo.ir", "--apply"),
                ("sequence", "robo.moves", "--sequence", "--mode", "ros", "--apply")],
    "prekick-plan": [("plan", "robo.prekick", "--plan-day", "--install")],
    "scorecard": [("score", "robo.scorecard", "--latest")],
    "model-daily": [
        ("plan", "nflmodel.ingest.archive_projections", "--plan-day", "--install"),
        ("capture-horizon", "nflmodel.ingest.archive_projections", "--horizon"),
        ("nflverse", "nflmodel.ingest.nflverse"),
        ("export-week", "nflmodel.export", "--league", "rurffl"),
        ("export-horizon", "nflmodel.export", "--league", "rurffl", "--horizon")],
    "model-prekick": [
        ("capture", "nflmodel.ingest.archive_projections"),
        ("export", "nflmodel.export", "--league", "rurffl")],
}


def receipt(job: str) -> dict:
    try:
        return json.loads((RECEIPTS / f"{job}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(job: str, doc: dict) -> None:
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    path = RECEIPTS / f"{job}.json"
    tmp = RECEIPTS / f"{job}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(path)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) \
        and v == v and v not in (float("inf"), float("-inf")) else None


def expiry(doc: dict | None, written: float) -> float:
    """When a marker ends: its `until`, else PAUSE_DEFAULT_H after it was written,
    and never later than PAUSE_MAX_H after that. `written` is the marker's `at`,
    or the file's mtime when the marker cannot say."""
    doc = doc if isinstance(doc, dict) else {}
    start = _num(doc.get("at")) or written
    until = _num(doc.get("until"))
    if until is None:
        until = start + PAUSE_DEFAULT_H * 3600
    return min(until, start + PAUSE_MAX_H * 3600)


def paused() -> dict | None:
    """The pause marker StopRoboner.bat / PauseRoboner.bat write, or None when
    automation runs. EVERY marker expires: the first reader past its expiry()
    removes it, so a pause needs no second task to end it and still ends if the
    PC was asleep at the time. An unreadable marker still counts as a stop, but
    only for PAUSE_DEFAULT_H from when it was written. The returned doc carries
    the effective `until`. RobonerWatchdog.ps1 reads the same file by the same rule."""
    try:
        raw = PAUSE.read_text(encoding="utf-8")
        written = PAUSE.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError:
        return {"by": "unknown", "until": time.time() + PAUSE_DEFAULT_H * 3600}
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        doc = {"by": "unknown"}
    ends = expiry(doc, written)
    if time.time() >= ends:
        PAUSE.unlink(missing_ok=True)
        return None
    return {**doc, "until": ends,
            "until_iso": datetime.fromtimestamp(ends).strftime("%Y-%m-%d %H:%M")}


def parse_until(text: str, now: datetime | None = None) -> datetime:
    """'1am', '1:30pm', '01:00' or '2026-09-29 01:00'. A bare time means its
    next occurrence, so '1am' typed in the evening is tonight."""
    now = now or datetime.now()
    raw = text.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        pass
    compact = raw.lower().replace(" ", "")
    for fmt in ("%I%p", "%I:%M%p", "%H:%M"):
        try:
            t = datetime.strptime(compact, fmt).time()
        except ValueError:
            continue
        at = datetime.combine(now.date(), t)
        return at if at > now else at + timedelta(days=1)
    raise ValueError(f"cannot read {text!r} as a time (try 1am, 13:30 or 2026-09-29 01:00)")


def pause(by: str, until: datetime | None = None) -> datetime:
    """Write a pause that ends on its own; return when it ends (see expiry)."""
    now = time.time()
    ends = expiry({"until": until.timestamp() if until is not None else None}, now)
    end_dt = datetime.fromtimestamp(ends)
    doc = {"by": by, "at": now, "at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
           "until": ends, "until_iso": end_dt.strftime("%Y-%m-%d %H:%M")}
    PAUSE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE.write_text(json.dumps(doc), encoding="utf-8")
    return end_dt


def resume() -> None:
    PAUSE.unlink(missing_ok=True)


STOP_MARKER_BY = "StopRoboner.bat"


def resume_stop() -> dict | None:
    """Clear the marker ONLY if StopRoboner.bat wrote it; return what was cleared.

    THE HUB'S START. The hub stops Roboner with StopRoboner.bat, which pauses every
    scheduled job, and its restart is that stop plus a start -- so a start that did
    not undo it left the responder up and every job exiting "automation paused".
    That happened three times in a week (4, 6 and 11 Nov 2026); the last one ran no
    lineup, moves or newswatch for four days and left the DEF slot empty at Sunday
    kickoff. A PauseRoboner.bat pause (timed or not) is a different decision and
    survives a hub start; so does an unreadable marker. The logon autostart does not
    call this, so a deliberate StopRoboner.bat still survives a reboot.
    """
    try:
        doc = json.loads(PAUSE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("by") != STOP_MARKER_BY:
        return None
    PAUSE.unlink(missing_ok=True)
    return doc


def run(job: str) -> int:
    """Run every step of a job, even after one fails, and exit with the first
    failure's code. Each step is an independent command the old wrappers also
    ran unconditionally: a failed IR sweep must not cost Tuesday's FAAB slate,
    and a failed one-shot registration must not cost the day's capture and
    export."""
    if job not in COMMANDS:
        raise ValueError(job)
    if paused():
        print(f"{job}: automation paused")
        return 0
    started = time.time()
    doc = {"job": job, "started": started, "status": "running", "pid": os.getpid(),
           "steps": []}
    _write(job, doc)
    first_bad = 0
    try:
        for row in COMMANDS[job]:
            name, module, *args = row
            print(f"{job}: {name} starting", flush=True)
            began = time.time()
            result = subprocess.run([sys.executable, "-u", "-m", module, *args],
                                    cwd=str(ROOT))
            doc["steps"].append({"name": name, "exit_code": result.returncode,
                                 "started": began, "finished": time.time()})
            _write(job, doc)
            if result.returncode and not first_bad:
                first_bad = result.returncode
        doc["status"] = "failed" if first_bad else "success"
        doc["exit_code"] = first_bad
    except Exception as exc:
        doc.update(status="failed", exit_code=1,
                   error=f"{type(exc).__name__}: {exc}"[:240])
    doc["finished"] = time.time()
    _write(job, doc)
    failed = [s["name"] for s in doc["steps"] if s["exit_code"]]
    print(f"{job}: {doc['status']} ({len(doc['steps']) - len(failed)}/"
          f"{len(COMMANDS[job])} steps OK"
          + (f"; failed: {', '.join(failed)}" if failed else "") + ")", flush=True)
    return int(doc["exit_code"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", nargs="?", choices=sorted(COMMANDS))
    ap.add_argument("--pause", metavar="BY",
                    help="stop every scheduled job and the job guard for "
                         f"{PAUSE_DEFAULT_H:g} h (or --until, at most {PAUSE_MAX_H:g} h)")
    ap.add_argument("--until", metavar="WHEN",
                    help="with --pause: resume on its own at this time (1am, 13:30, "
                         "2026-09-29 01:00)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--resume-stop", action="store_true",
                    help="clear only a StopRoboner.bat pause (the hub's start)")
    args = ap.parse_args()
    if args.resume_stop:
        cleared = resume_stop()
        print(f"Roboner automation resumed (cleared {STOP_MARKER_BY} pause from "
              f"{cleared.get('at_iso')})." if cleared
              else "No StopRoboner.bat pause to clear; any other pause is left in place.")
        return
    if args.until and not args.pause:
        ap.error("--until needs --pause")
    if args.pause:
        try:
            until = parse_until(args.until) if args.until else None
        except ValueError as exc:
            ap.error(str(exc))
        ends = pause(args.pause, until)
        capped = until is not None and ends < until
        print(f"Roboner automation paused until {ends:%a %d %b %H:%M}"
              + (f" (capped at {PAUSE_MAX_H:g} h)." if capped else
                 "." if until is not None else f" ({PAUSE_DEFAULT_H:g} h default).")
              + " It resumes on its own; ResumeRoboner.bat ends it sooner.")
        return
    if args.resume:
        resume()
        print("Roboner automation resumed.")
        return
    if not args.job:
        ap.error("a job name, --pause or --resume is required")
    raise SystemExit(run(args.job))


if __name__ == "__main__":
    main()
