"""Roster construction: one check that runs after anything reshapes the roster.

Two different rules have to hold at once, and they live in different modules:

  * SLEEPER-legal -- no reserve man without an IR-eligible designation, no more
    than ROSTER_MAX active (`ir.legality`, repaired by `ir.unblock`). Sleeper
    refuses every roster and lineup write until this holds.
  * BOT-legal -- every starting slot filled by a man who can play this week
    (`lineup.run`'s `holes` and `illegal`, repaired by `moves` patch and then a
    lineup write).

A roster can pass the first and fail the second. On 23 Sep 2026 waivers settled
at 00:07 with our new defence on the bench and nothing to start it; when Nico's
designation expired, the unblock cut that defence as the cheapest body, and a
starter would have been protected. Only the morning cascade ran both repairs in
order, so every other path that reshaped the roster -- the news pulse, the
Wednesday moves run, the IR sweep, a waiver settling -- stopped halfway.

`ensure()` is the one repair. `session()` wraps every entry point that writes to
the roster; the writers in `sleeper_write` call `mark_dirty()`, and the
OUTERMOST session runs `ensure()` once on the way out. A nested session, and
anything `ensure()` itself calls, adds nothing -- it is the writers' caller, so
re-entering it would repair its own repair.

    python -m robo.construction            # dry run: what would be repaired
    python -m robo.construction --apply
"""

import argparse
import contextlib
import json

from robo import DATA, LEAGUE_ID_2026, season

STATE = DATA / "construction.json"

# Each round is unblock -> patch -> lineup against a freshly read roster. Three
# covers an unblock that opens a slot and a patch that changes which slot the
# lineup can fill; beyond that a rejected transaction would cycle all pulse.
MAX_ROUNDS = 3
RETRIES = 1
RETRY_WAIT_S = 5.0

_state = {"depth": 0, "dirty": False, "repairing": False, "stack": []}


def mark_dirty() -> None:
    """A roster write just landed (or may have). Called by the writers."""
    if not _state["repairing"]:
        _state["dirty"] = True


def path() -> list[str]:
    """Which sessions are open right now, outermost first.

    Every roster-writing entry point opens one, so at the moment a writer fires
    this is the route that instructed it -- "news pulse > construction > ir
    unblock" -- which the transaction journal records against the write.
    """
    return list(_state["stack"])


@contextlib.contextmanager
def session(trigger: str, *, apply: bool = True, force: bool = False,
            run: bool = True, week: int | None = None,
            league_id: str = LEAGUE_ID_2026, statuses: dict | None = None):
    """Run ensure() once after the outermost session, if the roster changed.

    `force` runs it whether or not this process wrote anything -- the news
    pulse needs that, because a waiver settling or a designation expiring is
    Sleeper's change, not ours. `run=False` only collects: the caller will call
    ensure() itself at a point it chooses (the cascade logs it as a step). The
    yielded dict is filled with ensure()'s result on exit.
    """
    outer = _state["depth"] == 0 and not _state["repairing"]
    if outer:
        _state["dirty"] = False
    _state["depth"] += 1
    _state["stack"].append(trigger)
    result: dict = {}
    failed = False
    try:
        yield result
    except BaseException:
        failed = True
        raise
    finally:
        _state["depth"] -= 1
        try:
            if outer:
                dirty, _state["dirty"] = _state["dirty"], False
                # A caller that meant to run ensure() itself and died first
                # still gets it: a half-finished write sequence is exactly the
                # roster most likely to need repair.
                if apply and (dirty or force) and (run or failed):
                    result.update(_guarded(trigger, week, league_id, statuses))
        finally:
            # Popped after the repair, so its writes are attributed to the
            # session that caused them.
            _state["stack"].pop()


def deferred(trigger: str, *, apply: bool = True):
    return session(trigger, apply=apply, run=False)


def _guarded(trigger, week, league_id, statuses) -> dict:
    try:
        return ensure(week=week, league_id=league_id, apply=True,
                      trigger=trigger, statuses=statuses)
    except Exception as e:
        out = {"status": "failed", "trigger": trigger,
               "error": f"{type(e).__name__}: {e}"}
        _save(out)
        return out


def ensure(week: int | None = None, league_id: str = LEAGUE_ID_2026,
           apply: bool = True, trigger: str = "",
           statuses: dict | None = None) -> dict:
    """Take the roster to Sleeper-legal AND bot-legal, re-reading after each write.

    `statuses` is {player_id: designation} from a source fresher than the
    cached player dump (the pulse's weekly feed). Where it disagrees with the
    dump about one of our men, the dump is refreshed so every downstream reader
    -- lineup's NEVER_START, the sweep's plan -- sees the same designation the
    legality check does.

    ONE RETRY on a network error. The first live pulse lost the whole check to a
    single reset connection (07:13, 23 Sep 2026), which is twenty minutes of an
    unrepaired roster for nothing. Retrying is safe because every round re-reads
    the roster before it writes.
    """
    import time
    import requests
    # A caller with no session of its own (the pulse, the cascade's step) still
    # names itself in the path its repairs are journalled under.
    named = trigger and not _state["stack"]
    if named:
        _state["stack"].append(trigger)
    _state["stack"].append("construction")
    try:
        for attempt in range(RETRIES + 1):
            try:
                return _ensure(week, league_id, apply, trigger, statuses)
            except requests.exceptions.RequestException:
                if attempt == RETRIES:
                    raise
                time.sleep(RETRY_WAIT_S)
    finally:
        _state["stack"].pop()
        if named:
            _state["stack"].pop()


def _ensure(week, league_id, apply, trigger, statuses) -> dict:
    from robo import ir, lineup, moves
    from robo import sleeper_read as api

    week = week or season.current_week()
    _state["repairing"] = True
    try:
        try:
            refresh_statuses(statuses, league_id)
        except Exception:
            # Best effort: the legality check below reads the overlay itself.
            pass
        steps = []

        def snapshot():
            season.invalidate_live()
            roster = season.mine(league_id)
            players = ir.with_statuses(
                api.players(max_age_h=api.FRESH_STATUS_MAX_AGE_H), statuses)
            legal = ir.legality(roster, players, season.ir_statuses(league_id))
            plan = lineup.run(week=week, league_id=league_id,
                              apply=False, verbose=False)
            signature = (tuple(roster.get("players") or []),
                         tuple(roster.get("reserve") or []),
                         tuple(roster.get("starters") or []))
            return legal, plan, signature

        def issues_of(legal, plan):
            return {"frozen": not legal["legal"],
                    "holes": plan.get("holes") or [],
                    "illegal": plan.get("illegal") or []}

        for _ in range(MAX_ROUNDS):
            legal, plan, before = snapshot()
            issues = issues_of(legal, plan)
            if not any(issues.values()):
                return _finish({"status": "repaired" if steps else "clear",
                                "issues": issues, "steps": steps},
                               trigger, week, apply)
            if not apply:
                return _finish({"status": "would_repair", "issues": issues,
                                "steps": steps}, trigger, week, apply)

            if not legal["legal"]:
                unblocked = ir.unblock(apply=True, league_id=league_id,
                                       verbose=False, statuses=statuses)
                steps.append({"kind": "unblock", "legal": unblocked["legal"],
                              "steps": [s.get("text") for s in
                                        unblocked.get("steps") or []]})
                if not unblocked["legal"]:
                    break
            # Unblocking can itself change which slot is empty, so solve again.
            _, plan, _ = snapshot()
            if plan.get("holes"):
                patched = moves.run("free", apply=True, mode="patch",
                                    league_id=league_id, verbose=False)
                steps.append({"kind": "patch",
                              "submitted": patched.get("submitted") or [],
                              "gated": patched.get("gated"),
                              "control_block": patched.get("control_block")})
            season.invalidate_live()
            set_lineup = lineup.run(week=week, league_id=league_id,
                                    apply=True, verbose=False)
            steps.append({"kind": "lineup", "applied": set_lineup.get("applied"),
                          "blocked": set_lineup.get("write_blocked")})
            _, _, after = snapshot()
            if after == before:
                break

        legal, plan, _ = snapshot()
        issues = issues_of(legal, plan)
        status = "repaired" if steps and not any(issues.values()) else "unresolved"
        return _finish({"status": status, "steps": steps, "issues": issues},
                       trigger, week, apply)
    finally:
        _state["repairing"] = False
        if apply:
            # Whatever was pending has just been checked; a session still open
            # around an explicit ensure() must not run it a second time.
            _state["dirty"] = False


def refresh_statuses(statuses: dict, league_id: str = LEAGUE_ID_2026) -> None:
    """Re-pull the player dump if it disagrees with `statuses` about our men.

    Every consumer that reads designations from the dump -- the IR sweep, the
    unblock, lineup's NEVER_START -- then agrees with the fresher source.
    """
    from robo import sleeper_read as api
    if not statuses:
        return
    roster = season.mine(league_id)
    ours = {str(p) for p in (roster.get("players") or [])}
    dump = api.players(max_age_h=api.FRESH_STATUS_MAX_AGE_H)
    if any((dump.get(pid) or {}).get("injury_status") != statuses[pid]
           for pid in ours if pid in statuses):
        api.players(refresh=True)


def _finish(out: dict, trigger: str, week: int, apply: bool) -> dict:
    out["trigger"] = trigger
    out["week"] = week
    if apply:
        _publish_unresolved(out)
        _save(out)
    return out


def _save(out: dict) -> None:
    import time
    out.setdefault("published", last().get("published"))
    try:
        STATE.write_text(json.dumps({**out, "at": time.time()}, default=str,
                                    indent=1), encoding="utf-8")
    except Exception:
        pass


def _publish_unresolved(out: dict) -> None:
    """One public entry per distinct unfixable state, not one per pulse.

    The key is cleared once the roster is sound, so the same hole coming back
    next week is published again.
    """
    if out["status"] != "unresolved":
        out["published"] = None
        return
    key = json.dumps([out["week"], out["issues"]], sort_keys=True)
    out["published"] = key
    if key == last().get("published"):
        return
    from robo.decisions import record
    bits = []
    if out["issues"]["frozen"]:
        bits.append("Sleeper is refusing roster changes")
    if out["issues"]["holes"]:
        bits.append("no one on the roster can fill "
                    + ", ".join(out["issues"]["holes"]))
    if out["issues"]["illegal"] and not out["issues"]["holes"]:
        bits.append("the lineup still starts " + "; ".join(out["issues"]["illegal"]))
    record("lineup", f"Week {out['week']} lineup incomplete",
           "Could not field a complete lineup: " + "; ".join(bits) + ".",
           "Checked after " + (out["trigger"] or "a roster change")
           + ". Every repair available was tried: clearing the reserve, "
             "signing a free agent into the empty slot, and re-setting the "
             "lineup.",
           data={"week": out["week"], "issues": out["issues"],
                 "trigger": out["trigger"]})


def last() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(json.dumps(ensure(apply=a.apply, trigger="manual"),
                     default=str, indent=1))


if __name__ == "__main__":
    main()
