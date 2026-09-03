"""Sleeper league chat — read and post as Robowner.

Sleeper's chat rides on the same GraphQL endpoint as the write API. The league
chat is a message thread whose parent is the league itself (parent_type
"league", parent_id = league_id).

Mirrors robo.groupme's interface so the chat responder can drive both.

python -m robo.sleeper_chat check        # show recent messages
python -m robo.sleeper_chat post "msg"   # post as Robowner
"""

import json
import sys

from robo import chat_cursor, DATA, LEAGUE_ID_2026, ROBOWNER_USER_ID as ROBOWNER
from robo.sleeper_write import gql

STATE = DATA / "sleeper_chat_last_seen.json"
NAME = "sleeper"


def messages(limit: int = 25, league_id: str = LEAGUE_ID_2026) -> list[dict]:
    """Recent league-chat messages, newest first."""
    q = f"""query msgs {{
        messages(parent_id: "{league_id}") {{
            message_id text author_display_name author_id created author_is_bot
        }}
    }}"""
    rows = gql("msgs", q)["messages"][:limit]
    return [{
        "id": m["message_id"],
        "name": m.get("author_display_name"),
        "author_id": m.get("author_id"),
        "text": m.get("text") or "",
        "created": m.get("created"),
        "is_bot": m.get("author_is_bot"),
    } for m in rows]


def history(days: float = 7.0, max_msgs: int = 150,
            league_id: str = LEAGUE_ID_2026) -> list[dict]:
    """Recent conversation, oldest first. Same contract as groupme.history."""
    import time
    cutoff = time.time() - days * 86400
    rows = messages(limit=max_msgs * 2, league_id=league_id)
    keep = [m for m in rows
            if m.get("text") and (m.get("created") or 0) >= cutoff]
    return list(reversed(keep[:max_msgs]))


def _is_gif(url: str) -> bool:
    """Is this a GIF? Sleeper renders on the declared attachment type, so calling
    a GIF an image posts a still frame.

    Not endswith(".gif"): GroupMe's CDN puts the extension in the MIDDLE of the
    path -- i.groupme.com/480x267.gif.a41451ab... -- so every GIF in the archive
    pool was being typed as an image and posted frozen.
    """
    import re
    return bool(re.search(r"\.gif(?:[./?]|$)", url.lower()))


def post(text: str, reply_to: str | None = None, image_url: str | None = None,
         league_id: str = LEAGUE_ID_2026, parent_type: str = "league") -> str:
    """Post to a Sleeper channel as Robowner.

    `parent_type` is "league" for the league chat or "draft" for a draft room,
    with league_id carrying the draft id in that case -- same mutation, same
    shape, so one function serves both.

    Images and GIFs attach for real: Sleeper takes an external URL through
    attachment_type + k/v_attachment_data and renders it inline, so the GroupMe
    media pool works here unchanged. The URL belongs in the attachment ONLY --
    putting it in the text as well gets it rendered twice.
    """
    # The URL goes ONLY in the attachment, never in the text. Appending it too
    # made Sleeper render both -- a link preview (a still frame) beside the real
    # attachment, so one message showed the same image twice.
    body = text
    # Text goes in as a VARIABLE, not interpolated into the query. Inlining it
    # meant json.dumps escaped any non-ASCII to a backslash-u escape, and
    # Sleeper's GraphQL parser rejects those -- a single em dash returned
    # "unknown error during parsing" and the message was silently lost. The
    # local model writes em dashes constantly, so this was eating real
    # replies, not just alerts.
    attach = ""
    variables = {"text": body}
    if image_url:
        kind = "gif" if _is_gif(image_url) else "image"
        attach = f'attachment_type: "{kind}", k_attachment_data: $k, v_attachment_data: $v, '
        variables["k"] = ["url"]
        variables["v"] = [image_url]
    decl = "$text: String!" + (", $k: [String], $v: [String]" if image_url else "")
    q = """mutation create_message(%s) {
        create_message(parent_type: "%s", parent_id: "%s", %stext: $text) {
            message_id text author_display_name
        }
    }""" % (decl, parent_type, league_id, attach)
    return gql("create_message", q, variables)["create_message"]["message_id"]


def new_messages(league_id: str = LEAGUE_ID_2026,
                 commit: bool = True) -> list[dict]:
    """Messages since the last poll (oldest first), excluding our own and
    Sleeper's system messages.

    commit=False stages the cursor instead of advancing it; see
    groupme.new_messages() for why the responder wants that.
    """
    state = chat_cursor.read(STATE)
    last = state.get("last_id")
    msgs = messages(limit=50, league_id=league_id)
    if not msgs:
        return []
    newest = msgs[0]["id"]
    if last is None:
        # No usable cursor -- adopt the present and answer nothing.
        # See robo/chat_cursor.py: replaying the backlog would
        # burst-reply to every recent message instead of recovering.
        chat_cursor.adopt(STATE, newest)
        return []
    STATE.write_text(json.dumps({"last_id": newest} if commit
                                else {"last_id": last, "pending_id": newest}))
    fresh = []
    for m in msgs:
        if last and int(m["id"]) <= int(last):
            break
        # skip our own posts and Sleeper's automated "sys" notices
        if m["author_id"] == ROBOWNER or m["name"] in (None, "sys"):
            continue
        fresh.append(m)
    return list(reversed(fresh))


def commit_seen() -> None:
    """Promote a staged cursor — call once a batch is fully handled."""
    chat_cursor.commit(STATE)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        for m in reversed(messages(limit=10)):
            print(f"  [{m['name']}] {m['text'][:80]}")
    elif cmd == "post":
        print("posted:", post(sys.argv[2]))
