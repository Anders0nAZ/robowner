"""Shout across every channel at once, for the things a human must not miss.

Built for draft day, where the operator's attention is on his own team and the
bot has to be able to say "I am not picking for you" loudly enough to cut
through that. Three destinations, because on a Sunday afternoon nobody is
watching all three: the Sleeper DRAFT ROOM (where his eyes actually are during
a draft), the Sleeper league chat, and GroupMe.

Every send is independently guarded. A dead GroupMe token must not stop the
draft-room alert, and no alert may ever raise into the draft loop -- an alert
about a missed pick that itself crashes the agent would be the joke of the
season. Failures are counted and returned so the caller can see what landed.

Alerts are rate-limited per key: draft-day failure modes repeat every few
seconds, and eleven consecutive missed picks should read as one alarm, not
eleven identical messages in the league chat.
"""

import time

from robo import DRAFT_ID_2026, LEAGUE_ID_2026

# Same alert key won't re-fire inside this window.
COOLDOWN_SECS = 300
_last: dict[str, float] = {}


def _groupme(text: str) -> None:
    from robo import groupme
    groupme.post(text)


def _sleeper_league(text: str) -> None:
    from robo import sleeper_chat
    sleeper_chat.post(text, league_id=LEAGUE_ID_2026)


def _sleeper_draft(text: str, draft_id: str) -> None:
    """Post into the draft room itself.

    parent_type "draft" with the draft id, confirmed against a throwaway mock
    on 28 Aug 2026 ("draft_chat" and "league_draft" both 500).
    """
    from robo.sleeper_write import gql
    q = ('mutation create_message($text: String!) { create_message('
         'parent_type: "draft", parent_id: "%s", text: $text) { message_id } }'
         % draft_id)
    gql("create_message", q, {"text": text})


ALL_CHANNELS = ("draft", "sleeper", "groupme")
# In-season there is no draft room worth writing to -- the draft is complete and
# the room is a graveyard. GroupMe is also excluded by default off-season,
# because its posts CANNOT be deleted (403 with the user token) and an
# operational nit does not deserve a permanent entry in the league's chat.
# Anything that genuinely needs a human still reaches Nate on the status page.
INSEASON_CHANNELS = ("sleeper",)


def blast(text: str, key: str = "", draft_id: str = DRAFT_ID_2026,
          cooldown: int = COOLDOWN_SECS, live: bool | None = None,
          channels: tuple = ALL_CHANNELS) -> dict:
    """Send to draft room, league chat and GroupMe. Never raises.

    `key` groups repeats: the same key inside the cooldown is dropped, so a
    failure that recurs every poll produces one alarm rather than a flood.
    Returns {channel: True/error-string, "skipped": bool}.

    `channels` narrows the destinations. Draft day wants all three; in-season
    callers should pass INSEASON_CHANNELS, which drops the finished draft room
    and the undeletable GroupMe.

    SAFETY: the league chat and GroupMe destinations are hardcoded to the real
    league, so passing a mock draft_id does NOT make this call a test -- two of
    the three channels still post publicly. On 28 Aug 2026 a cooldown check with
    a mock draft_id put a bare "x" in the league GroupMe and Sleeper chat for
    exactly this reason. A non-real draft_id therefore now means "not live":
    the message is printed and only the given draft room is written to. Pass
    live=True to override deliberately.
    """
    if live is None:
        live = draft_id == DRAFT_ID_2026
    if not live:
        print(f"[alerts: not live, would send] {text}")
        return {"skipped": False, "live": False}
    k = key or text[:40]
    now = time.time()
    if now - _last.get(k, 0) < cooldown:
        return {"skipped": True}
    _last[k] = now

    out: dict = {"skipped": False}
    for name, fn in (("draft", lambda: _sleeper_draft(text, draft_id)),
                     ("sleeper", lambda: _sleeper_league(text)),
                     ("groupme", lambda: _groupme(text))):
        if name not in channels:
            continue
        try:
            fn()
            out[name] = True
        except Exception as e:  # one dead channel must not silence the others
            out[name] = f"{type(e).__name__}: {e}"[:160]
    return out


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) or "Roboner alert test — ignore."
    print(blast(msg, key="manual-test", cooldown=0))
