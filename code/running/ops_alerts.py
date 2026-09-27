"""Private operational alerts to Nate's owner-only GroupMe Side Chat.

Two tiers, so the chat only ever carries something a person has to act on:

  ACTION  posted to Side Chat (throttled per key). Automatic recovery has been
          tried and has not worked, or a window has closed: a job was given up
          on, a deadline was missed, a task could not be restored, a task was
          disabled, or Task Scheduler stayed unreadable.
  INFO    written to data/ops_notices.jsonl only. Routine self-healing -- a
          retry, a recovery, a restored task -- which the guard handles without
          anyone, and which would train the reader to ignore the chat.
"""

from __future__ import annotations

import json
import os
import time

import requests

from robo import DATA, ROOT

STATE = DATA / "ops_alerts.json"
NOTICES = DATA / "ops_notices.jsonl"
COOLDOWN_S = 6 * 3600
ACTION, INFO = "action", "info"


def _bot_id() -> str:
    value = os.environ.get("GROUPME_OPS_BOT_ID", "")
    if value:
        return value
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("GROUPME_OPS_BOT_ID="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise RuntimeError("GROUPME_OPS_BOT_ID is not configured")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _note(key: str, message: str, tier: str) -> None:
    try:
        NOTICES.parent.mkdir(parents=True, exist_ok=True)
        with NOTICES.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "tier": tier, "key": key,
                                "message": message}) + "\n")
    except OSError:
        pass


def send(key: str, message: str, tier: str = ACTION, *,
         cooldown_s: int = COOLDOWN_S) -> bool:
    """Record every notice; post ACTION ones to Side Chat.

    Returns True when the notice needs nothing further: an INFO notice once
    logged, an ACTION one once Side Chat accepted it or while it is throttled.
    """
    if tier not in (ACTION, INFO):
        raise ValueError(tier)
    _note(key, message, tier)
    if tier == INFO:
        return True
    state = _state()
    if time.time() - float(state.get(key) or 0) < cooldown_s:
        return True
    body = {"bot_id": _bot_id(), "text": message[:990]}
    response = requests.post("https://api.groupme.com/v3/bots/post",
                             json=body, timeout=20)
    response.raise_for_status()
    state[key] = time.time()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE)
    return True
