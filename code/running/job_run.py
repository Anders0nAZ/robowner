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


def paused() -> dict | None:
    """The pause marker StopRoboner.bat / PauseRoboner.bat write, or None when
    automation runs. A marker carrying `until` expires on its own: the first
    reader past that time removes it, so a timed pause needs no second task to
    end it and still ends if the PC was asleep at the time. RobonerWatchdog.ps1
    reads the same file by the same rule."""
    try:
        doc = json.loads(PAUSE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # Present but unreadable still means somebody asked for a stop.
        return {"by": "unknown"}
    if not isinstance(doc, dict):
        return {"by": "unknown"}
    until = doc.get("until")
    if isinstance(until, (int, float)) and time.time() >= until:
        PAUSE.unlink(missing_ok=True)
        return None
    return doc


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


def pause(by: str, until: datetime | None = None) -> None:
    doc = {"by": by, "at": time.time(), "at_iso": time.strftime("%Y-%m-%d %H:%M:%S")}
    if until is not None:
        doc.update(until=until.timestamp(), until_iso=until.strftime("%Y-%m-%d %H:%M"))
    PAUSE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE.write_text(json.dumps(doc), encoding="utf-8")


def resume() -> None:
    PAUSE.unlink(missing_ok=True)


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
                    help="stop every scheduled job and the job guard until --resume")
    ap.add_argument("--until", metavar="WHEN",
                    help="with --pause: resume on its own at this time (1am, 13:30, "
                         "2026-09-29 01:00)")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    if args.until and not args.pause:
        ap.error("--until needs --pause")
    if args.pause:
        try:
            until = parse_until(args.until) if args.until else None
        except ValueError as exc:
            ap.error(str(exc))
        pause(args.pause, until)
        print("Roboner automation paused"
              + (f" until {until:%a %d %b %H:%M}." if until else " until resumed."))
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
