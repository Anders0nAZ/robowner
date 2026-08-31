"""The draft room as a chat channel, so the bot can talk while it drafts.

Sleeper's draft room has its own message thread, separate from the league chat:
same `messages` query and `create_message` mutation, but parent_type "draft" and
the draft id as parent_id. That is the whole difference, so this module is a thin
re-binding of sleeper_chat rather than a second implementation.

Its own cursor file, because the draft room and the league chat are read
independently and a shared high-water mark would lose messages in one whenever
the other advanced.

python -m robo.draft_chat            # show recent draft-room messages
"""

import json

from robo import DATA, DRAFT_ID_2026, ROBOWNER_USER_ID as ROBOWNER
from robo import sleeper_chat
from robo.sleeper_write import gql

STATE = DATA / "draft_chat_last_seen.json"


def messages(limit: int = 25, draft_id: str = DRAFT_ID_2026) -> list[dict]:
    q = """query msgs {
        messages(parent_id: "%s") {
            message_id text author_display_name author_id created author_is_bot
        }
    }""" % draft_id
    rows = gql("msgs", q)["messages"][:limit]
    return [{"id": m["message_id"], "name": m.get("author_display_name"),
             "author_id": m.get("author_id"), "text": m.get("text") or "",
             "created": m.get("created"), "is_bot": m.get("author_is_bot")}
            for m in rows]


def post(text: str, reply_to: str | None = None, image_url: str | None = None,
         draft_id: str = DRAFT_ID_2026) -> str:
    return sleeper_chat.post(text, reply_to=reply_to, image_url=image_url,
                             league_id=draft_id, parent_type="draft")


# The room is worth listening to for a while either side of the draft itself --
# people gather early and keep talking after the last pick, and a bot that only
# speaks between pick 1 and pick 204 misses both ends of the actual event.
CUSHION_SECS = 30 * 60


def is_live(draft_id: str = DRAFT_ID_2026, _cache: dict = {}) -> bool:
    """Should the bot be listening in the draft room right now? Cached a minute.

    True while drafting, and for CUSHION_SECS either side: from half an hour
    before the scheduled start until half an hour after the final pick. Outside
    that it is False, because the room exists all year and the responder polls
    every 45s -- without the gate the bot would read an empty room for months
    and still be answering in it at Christmas.
    """
    import time
    from robo import sleeper_read as api
    now = time.time()
    if now - _cache.get("at", 0) > 60:
        try:
            d = api.draft(draft_id)
            status = d.get("status")
            now_ms = now * 1000
            cushion_ms = CUSHION_SECS * 1000
            if status == "drafting":
                live = True
            elif status == "pre_draft":
                st = d.get("start_time")
                # within the half hour BEFORE the scheduled start
                live = bool(st and 0 <= st - now_ms <= cushion_ms)
            elif status == "complete":
                lp = d.get("last_picked") or d.get("start_time")
                # within the half hour AFTER the last pick
                live = bool(lp and 0 <= now_ms - lp <= cushion_ms)
            else:
                live = False
            _cache["live"] = live
        except Exception:
            _cache["live"] = False
        _cache["at"] = now
    return _cache.get("live", False)


def history(days: float = 7.0, max_msgs: int = 150,
            draft_id: str = DRAFT_ID_2026) -> list[dict]:
    """Recent draft-room conversation, oldest first. A draft room only ever
    holds hours of talk, so the window rarely binds here."""
    import time
    cutoff = time.time() - days * 86400
    rows = messages(limit=max_msgs * 2, draft_id=draft_id)
    keep = [m for m in rows if m.get("text") and (m.get("created") or 0) >= cutoff]
    return list(reversed(keep[:max_msgs]))


def new_messages(draft_id: str = DRAFT_ID_2026, commit: bool = True) -> list[dict]:
    """Messages since the last poll, oldest first, excluding our own.

    Cursor is staged unless commit=True, matching the other channels: a failure
    mid-reply must replay the batch rather than bury it.
    """
    if not is_live(draft_id):
        return []
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    last = state.get("last_id")
    msgs = messages(limit=50, draft_id=draft_id)
    if msgs:
        newest = msgs[0]["id"]
        STATE.write_text(json.dumps({"last_id": newest} if commit
                                    else {"last_id": last, "pending_id": newest}))
    # Filter our OWN posts by author_id, not by is_bot. Robowner is an ordinary
    # Sleeper account, so author_is_bot comes back FALSE on everything it posts
    # -- verified against the live league chat. Filtering on is_bot alone let
    # the draft room see the bot's own messages, and TRIGGERS match its own
    # name, so any post of ours mentioning "Roboner" would have been answered
    # by us, then answered again. sleeper_chat.py already filters on author_id;
    # this channel was the one that did not.
    fresh = [m for m in reversed(msgs)
             if m["text"] and not m.get("is_bot")
             and m.get("author_id") != ROBOWNER]
    if last:
        fresh = [m for m in fresh if str(m["id"]) > str(last)]
    return fresh


def commit_seen() -> None:
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    if state.get("pending_id"):
        STATE.write_text(json.dumps({"last_id": state["pending_id"]}))


if __name__ == "__main__":
    for m in messages(limit=15):
        print(f"  {m['name']}: {m['text'][:80]}")
