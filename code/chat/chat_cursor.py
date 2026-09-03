"""Where each chat channel got to -- the "already answered this" marker.

Shared by all three transports (groupme, sleeper_chat, draft_chat) because the
question is identical in each and so are the two ways it goes wrong.

A CURSOR THAT WILL NOT PARSE COUNTS AS ABSENT, NOT FATAL. The read used to be a
bare json.loads and Sleeper's cursor arrived as 34 bytes of NUL -- the signature
of a machine that lost power between NTFS allocating the file and flushing its
contents. Every poll from that moment raised, the responder logged "cycle error
(continuing)" and moved on, and the bot did not read the Sleeper league chat
again for three days. Nothing was down, nothing alerted: it just went deaf on
one channel while looking alive on the other two.

AN ABSENT CURSOR MEANS RE-BASELINE, NOT REPLAY. This is the half that makes the
recovery safe. Every transport asks for the last 50-100 messages and treats
"older than the cursor" as the stop condition, so falling back to no cursor
would make the entire recent backlog look new and the bot would answer all of
it in one burst -- on GroupMe, where posts cannot be deleted. Whatever was said
during the outage is already lost; the correct move is to adopt the present as
the new cursor and say nothing. Same rule on a genuinely first run, which had
the same latent flood in it and had simply never been exercised.
"""

import json


def read(path) -> dict:
    """The saved cursor, or {} when there is not a usable one."""
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def adopt(path, newest) -> None:
    """Take `newest` as the cursor without treating anything as unread."""
    path.write_text(json.dumps({"last_id": newest}))


def commit(path) -> None:
    """Promote a staged cursor -- call once a batch is fully handled."""
    state = read(path)
    if state.get("pending_id"):
        path.write_text(json.dumps({"last_id": state["pending_id"]}))
