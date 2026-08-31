"""GroupMe integration for the Roboner bot.

Post as the bot (GROUPME_BOT_ID); read the league chat by polling with
Nate's access token (GROUPME_TOKEN) — GroupMe bots can post but not read,
and we deliberately run without a public callback URL.

python -m robo.groupme check          # verify creds, show latest messages
python -m robo.groupme post "msg"     # post as the bot
python -m robo.groupme set-avatar     # point the bot avatar at the hosted art
"""

import json
import os
import sys
from pathlib import Path

import requests

from robo import DATA, ROOT, AVATAR_URL  # noqa: F401  (re-exported)

API = "https://api.groupme.com/v3"
STATE = DATA / "groupme_last_seen.json"


def _env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
    if not val:
        raise RuntimeError(f"{key} not set in .env")
    return val


def post(text: str, reply_to: str | None = None, image_url: str | None = None) -> None:
    """Post to the league chat as the Roboner bot.

    text      message body; chunked at 990 chars (GroupMe caps ~1000)
    reply_to  message_id to reply to — renders as a native GroupMe reply
    image_url a GroupMe CDN url from upload_image() (works for GIFs too)

    Attachments ride on the FIRST chunk only, so a long reply still threads
    correctly instead of attaching to a trailing fragment.
    """
    chunks = [text[i:i + 990] for i in range(0, len(text), 990)] or [""]
    for i, chunk in enumerate(chunks):
        body = {"bot_id": _env("GROUPME_BOT_ID"), "text": chunk}
        if i == 0:
            attachments = []
            if reply_to:
                attachments.append({"type": "reply", "reply_id": str(reply_to),
                                    "base_reply_id": str(reply_to)})
            if image_url:
                attachments.append({"type": "image", "url": image_url})
            if attachments:
                body["attachments"] = attachments
        r = requests.post(f"{API}/bots/post", json=body, timeout=20)
        r.raise_for_status()


def upload_image(source: str | bytes) -> str:
    """Upload an image (path, http url, or raw bytes) to GroupMe's CDN.

    Returns the i.groupme.com url to hand to post(image_url=...). Animated
    GIFs are preserved — GroupMe stores them as .gif and renders animation.
    """
    if isinstance(source, bytes):
        data = source
    elif str(source).startswith("http"):
        r = requests.get(source, timeout=30)
        r.raise_for_status()
        data = r.content
    else:
        data = Path(source).read_bytes()
    kind = "image/gif" if data[:6] in (b"GIF87a", b"GIF89a") else "image/png"
    up = requests.post("https://image.groupme.com/pictures", data=data,
                       headers={"Content-Type": kind,
                                "X-Access-Token": _env("GROUPME_TOKEN")},
                       timeout=90)
    up.raise_for_status()
    return up.json()["payload"]["url"]


def messages(limit: int = 20, since_id: str | None = None,
             before_id: str | None = None) -> list[dict]:
    params = {"token": _env("GROUPME_TOKEN"), "limit": limit}
    if since_id:
        params["since_id"] = since_id
    if before_id:
        params["before_id"] = before_id
    r = requests.get(f"{API}/groups/{_env('GROUPME_GROUP_ID')}/messages",
                     params=params, timeout=15)
    if r.status_code == 304:  # no new messages
        return []
    r.raise_for_status()
    return r.json()["response"]["messages"]


def history(days: float = 7.0, max_msgs: int = 150) -> list[dict]:
    """Recent conversation, oldest first: everything inside `days`, capped.

    Two bounds on purpose. Time keeps it relevant; the cap keeps one blow-up day
    from crowding out the week -- this group's busiest day was 153 messages, and
    a raw week window would be almost entirely that argument.
    """
    import time
    cutoff = time.time() - days * 86400
    out, before = [], None
    while len(out) < max_msgs:
        batch = messages(limit=min(100, max_msgs - len(out)), before_id=before)
        if not batch:
            break
        out += batch
        before = batch[-1]["id"]
        if batch[-1].get("created_at", 0) < cutoff:
            break
    out = [m for m in out if m.get("created_at", 0) >= cutoff and m.get("text")]
    return list(reversed(out[:max_msgs]))


_BOT_SENDER: dict = {}


def bot_sender_id(batch: list[dict] | None = None) -> str | None:
    """The bot's USER id as the group sees it -- NOT GROUPME_BOT_ID.

    Two different identifiers, and confusing them is why replies to the bot went
    unnoticed. GROUPME_BOT_ID is the posting credential, a long hex string.
    Everyone reading the group sees a short numeric sender_id instead, and a
    reply attachment names THAT one. Discovered from our own posts rather than
    hardcoded, so it survives the bot being recreated; GROUPME_BOT_SENDER_ID
    overrides if it ever needs pinning.
    """
    if _BOT_SENDER.get("id"):
        return _BOT_SENDER["id"]
    env = os.environ.get("GROUPME_BOT_SENDER_ID")
    if env:
        _BOT_SENDER["id"] = env
        return env
    for m in (batch or []) + (messages(limit=100) if batch is None else []):
        if m.get("sender_type") == "bot" and m.get("sender_id"):
            _BOT_SENDER["id"] = m["sender_id"]
            return m["sender_id"]
    return None


def replies_to_bot(msg: dict, bot_id: str | None) -> bool:
    """Did this message use GroupMe's reply function on one of OUR posts?

    The module has always claimed the bot answers a "name mention or reply to
    the bot", and only the name half was ever implemented. Measured over 600
    real messages: four people replied directly to the bot and three of them
    never typed its name, so three of four were silently ignored.
    """
    if not bot_id:
        return False
    for a in msg.get("attachments") or []:
        if (a or {}).get("type") == "reply" and a.get("user_id") == bot_id:
            return True
    return False


def new_messages(commit: bool = True) -> list[dict]:
    """Messages since last poll (oldest first), excluding our own bot posts.

    Each message is tagged `addressed` when it is a GroupMe reply to one of our
    posts, so the responder can treat that as being spoken to even when nobody
    typed the bot's name.

    With commit=False the cursor is only STAGED, not advanced; the batch counts
    as seen once commit_seen() runs. The responder uses that so a failure mid-
    reply (an Ollama timeout, say) retries the messages next cycle instead of
    silently burying them — advancing on fetch used to drop them for good.
    """
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    last = state.get("last_id")
    msgs = messages(limit=100, since_id=last)
    if msgs:
        newest = msgs[0]["id"]
        STATE.write_text(json.dumps({"last_id": newest} if commit
                                    else {"last_id": last, "pending_id": newest}))
    bot_id = bot_sender_id(msgs)
    out = [m for m in reversed(msgs)
           if m.get("sender_type") != "bot" and m.get("text")]
    for m in out:
        m["addressed"] = replies_to_bot(m, bot_id)
    return out


def commit_seen() -> None:
    """Promote a staged cursor — call once a batch is fully handled."""
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    if state.get("pending_id"):
        STATE.write_text(json.dumps({"last_id": state["pending_id"]}))


def set_avatar() -> None:
    """Update the bot's avatar to the hosted art (re-uploads via GroupMe CDN)."""
    img = requests.get(AVATAR_URL, timeout=30)
    img.raise_for_status()
    up = requests.post("https://image.groupme.com/pictures",
                       data=img.content,
                       headers={"Content-Type": "image/png",
                                "X-Access-Token": _env("GROUPME_TOKEN")},
                       timeout=60)
    up.raise_for_status()
    cdn_url = up.json()["payload"]["url"]
    print("GroupMe CDN url:", cdn_url)
    print("NOTE: bots API has no avatar-update endpoint; paste this URL into the "
          "bot's Avatar field at dev.groupme.com/bots (or it can be set at creation).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        msgs = messages(limit=5)
        print(f"OK — {len(msgs)} recent messages in group:")
        for m in reversed(msgs):
            print(f"  [{m.get('name')}] {(m.get('text') or '')[:80]}")
    elif cmd == "post":
        post(sys.argv[2])
        print("posted")
    elif cmd == "set-avatar":
        set_avatar()
