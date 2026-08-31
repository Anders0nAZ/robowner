"""Injured-reserve moves: the one roster decision that needs no valuation.

Whether a player may sit on reserve is written in the league settings, not in a
model. Ours accepts Out, Suspended, COV (plus anyone already tagged IR or PUP)
and refuses Doubtful, NA and DNR. That makes this the only part of in-season
roster management that can run live today, while what a player is WORTH is
still an open question (see robo/value.py).

IT NEVER FILLS THE SLOT IT FREES. Reserving an injured player and then signing
somebody with the space are two different decisions: the first is bookkeeping,
the second needs a valuation the bot does not have yet. So this opens the slot,
records why, and stops. An open slot showing on the status page is the point,
not an oversight.

Going the other way is asymmetric on purpose. Reserving is always safe -- the
player could not have played anyway. Activating is not: if the roster is full,
somebody has to be cut to make room, and choosing that person is a valuation.
So a recovered player is activated when there is space and ALERTED when there
is not, rather than quietly costing us a body.

python -m robo.ir              # what it would do
python -m robo.ir --apply      # do it
"""

import argparse

from robo import LEAGUE_ID_2026, season, settings
from robo import sleeper_read as api

# Master switch. Off means the module still reports what it would do and
# changes nothing, which is the state to leave it in if the league ever
# disputes an IR move.
IR_ENABLED = True

settings.apply(__name__, globals())


def plan(league_id: str = LEAGUE_ID_2026) -> dict:
    """What should move, in or out of reserve, and why. Reads only."""
    players = api.players()
    r = season.mine(league_id)
    roster = list(r.get("players") or [])
    reserve = list(r.get("reserve") or [])
    starters = set(r.get("starters") or [])
    sl = season.slots(league_id)
    ok = season.ir_statuses(league_id)

    to_reserve, to_activate, blocked = [], [], []

    room = sl["ir_open"]
    for pid in roster:
        if pid in reserve:
            continue
        st = (players.get(pid) or {}).get("injury_status") or ""
        if st not in ok:
            continue
        name = api.player_name(players, pid)
        if pid in starters:
            # Reserving somebody Sleeper still has in a starting slot would
            # leave the lineup pointing at a player who is not on the active
            # roster. The daily task runs lineup first for exactly this reason;
            # by the next pass he is on the bench and eligible to move.
            blocked.append({"player_id": pid, "name": name, "status": st,
                            "why": "still in the starting lineup; "
                                   "lineup runs first and will bench him"})
            continue
        if room <= 0:
            blocked.append({"player_id": pid, "name": name, "status": st,
                            "why": f"all {sl['ir_slots']} IR slots are full"})
            continue
        room -= 1
        to_reserve.append({"player_id": pid, "name": name, "status": st,
                           "why": f"designated {st}, which this league allows on "
                                  f"reserve; parking him frees an active slot"})

    space = sl["open"] + len(to_reserve)
    for pid in reserve:
        st = (players.get(pid) or {}).get("injury_status") or ""
        if st in ok:
            continue
        name = api.player_name(players, pid)
        if space <= 0:
            blocked.append({"player_id": pid, "name": name, "status": st or "healthy",
                            "why": "no longer IR-eligible but the active roster is "
                                   "full; activating him needs a cut, and choosing "
                                   "who to cut is a valuation this bot does not "
                                   "have yet"})
            continue
        space -= 1
        to_activate.append({"player_id": pid, "name": name, "status": st or "healthy",
                            "why": f"no longer carries an IR-eligible designation "
                                   f"({st or 'healthy'}), so he cannot stay on "
                                   f"reserve"})

    target = [p for p in reserve if p not in {m["player_id"] for m in to_activate}]
    target += [m["player_id"] for m in to_reserve]
    return {"reserve": to_reserve, "activate": to_activate, "blocked": blocked,
            "current": reserve, "target": target, "slots": sl,
            "changed": sorted(target) != sorted(reserve)}


def render(p: dict) -> str:
    sl = p["slots"]
    L = [f"IR: {sl['ir_used']}/{sl['ir_slots']} used, "
         f"active roster {sl['active']}/{sl['roster_max']} ({sl['open']} open)"]
    if not IR_ENABLED:
        L.append("  IR_ENABLED is off - reporting only, nothing will move")
    for m in p["reserve"]:
        L.append(f"  -> RESERVE   {m['name']} [{m['status']}] - {m['why']}")
    for m in p["activate"]:
        L.append(f"  -> ACTIVATE  {m['name']} [{m['status']}] - {m['why']}")
    for m in p["blocked"]:
        L.append(f"     blocked   {m['name']} [{m['status']}] - {m['why']}")
    if not (p["reserve"] or p["activate"] or p["blocked"]):
        L.append("  nothing to move")
    return "\n".join(L)


def run(apply: bool = False, league_id: str = LEAGUE_ID_2026,
        verbose: bool = True) -> dict:
    p = plan(league_id)
    if verbose:
        print(render(p))
    p["applied"] = False
    if not apply or not IR_ENABLED or not p["changed"]:
        return p

    from robo.decisions import record
    from robo.sleeper_write import set_reserve
    r = season.mine(league_id)
    set_reserve(r["roster_id"], p["target"], league_id)
    p["applied"] = True

    moved = [f"{m['name']} to injured reserve" for m in p["reserve"]]
    moved += [f"{m['name']} back to the active roster" for m in p["activate"]]
    why = "; ".join(m["why"] for m in p["reserve"] + p["activate"])
    freed = len(p["reserve"]) - len(p["activate"])
    if freed > 0:
        why += (f". That leaves {freed} active roster spot(s) open. Nothing is "
                f"being signed to fill them: what a free agent is worth has not "
                f"been modelled yet, so the space is left open rather than spent")
    record("ir", "Injured reserve updated", "Moved " + ", ".join(moved) + ".",
           why + ".", data={"reserve": p["target"], "previous": p["current"]})

    if verbose:
        print("applied.")
    return p


def warnings(league_id: str = LEAGUE_ID_2026) -> list[str]:
    """Blocked moves worth a human's attention, for the status page.

    Deliberately NOT a chat alert. Neither of these is urgent enough to earn a
    permanent GroupMe post, and Nate reads the status page; blasting an
    operational nit into the league chat is the kind of noise that gets a bot
    muted. The genuinely time-critical case -- a lineup that cannot be made
    legal before kickoff -- is alerted from lineup.py, not here.
    """
    out = []
    for m in plan(league_id)["blocked"]:
        if "IR slots are full" in m["why"]:
            out.append(f"{m['name']} is {m['status']} and IR is full - an active "
                       f"roster spot is held by a player who cannot play")
        elif "active roster is full" in m["why"]:
            out.append(f"{m['name']} is no longer IR-eligible and the roster is "
                       f"full - activating him needs a cut")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--league", default=LEAGUE_ID_2026)
    args = ap.parse_args()
    run(apply=args.apply, league_id=args.league)


if __name__ == "__main__":
    main()
