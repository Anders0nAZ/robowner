"""One writer at a time for projection and roster-decision cascades.

The full cascade and the injury pulse both replace the same model/valuation
artifacts. Task Scheduler prevents two copies of one task, but it knows nothing
about the other task, so this small cross-process lock is shared by both entry
points.
"""

import json
import os
import time
import uuid
from pathlib import Path

from robo import DATA

LOCK = DATA / "decision_run.lock"
STALE_S = 30 * 60


class RunBusy(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    """Whether a lock owner still exists; permission errors mean it does."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


class DecisionRun:
    """Acquire the shared decision lock, optionally waiting for its owner."""

    def __init__(self, owner: str, wait_s: float = 0, path: Path = LOCK):
        self.owner = owner
        self.wait_s = max(0.0, float(wait_s))
        self.path = path
        self.held = False
        self.token = uuid.uuid4().hex

    def _existing_owner(self) -> str:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            return str(doc.get("owner") or "another decision run")
        except Exception:
            return "another decision run"

    def _reap_abandoned(self) -> None:
        if not self.path.exists():
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(doc.get("pid") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            pid = 0
        unknown_and_stale = (not pid and
                             time.time() - self.path.stat().st_mtime > STALE_S)
        if (pid and not pid_alive(pid)) or unknown_and_stale:
            self.path.unlink()

    def __enter__(self):
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                self._reap_abandoned()
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = json.dumps({"owner": self.owner, "pid": os.getpid(),
                                      "at": time.time(),
                                      "token": self.token}).encode("utf-8")
                os.write(fd, payload)
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RunBusy(f"{self._existing_owner()} is running")
                time.sleep(0.25)

    def __exit__(self, *_):
        if not self.held:
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if doc.get("token") == self.token:
                self.path.unlink()
        except (OSError, ValueError, TypeError):
            pass
        self.held = False
