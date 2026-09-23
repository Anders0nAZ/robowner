"""The in-season chain, in order, on one snapshot.

WHAT THIS IS FOR. A man in an active slot gets hurt on a Thursday. What should
happen is a sequence: bench him, move him to injured reserve, re-optimise the
lineup around the gap, fill the roster spot that just opened, re-optimise again
because the man we added might be startable today, and note who is sitting on
waivers for Tuesday. Every one of those steps existed. The SEQUENCE did not.

WHAT WAS ACTUALLY HAPPENING. Five gaps, all found by reading the live schedule
rather than the code:

  * `RobonerMoves` was never registered, so `--mode ros` had never run at all
    -- the .vbs existed, the scheduled task did not;
  * `--mode patch` returns nothing unless a STARTING slot is unfillable, so a
    freed bench spot filled nothing and no code path anywhere asked "the roster
    is under 17, should we go and get somebody";
  * the player dump caches for 24 hours and is refreshed once at 06:30, so a
    designation landing at noon was invisible until the following morning --
    and `injury_status` is what lineup.startable(), ir.py and expected.py's
    fallback all read;
  * nothing re-optimised after a roster change, so a man added at 07:00 could
    not start until the 09:00 lineup run happened to pick him up. The chain's
    correctness was a property of the schedule, not of the code;
  * the weekly number was recomputed live at 09:00 and 16:00 while the
    rest-of-season number was frozen at the 06:30 build, so the two halves of a
    roster decision could be reading different days.

ORDER IS LOAD-BEARING IN TWO PLACES, and both are easy to get backwards.

  CAPTURE BEFORE EXPORT, and this was got wrong first. The plan claimed that
  re-pulling our own player dump would invalidate the model's artifact, because
  nflmodel/simulate.py reads robo.sleeper_read.players(). It does read it -- and
  viewer_cache._input_files() does NOT fingerprint it, so re-pulling our dump
  changes nothing and the export returns a cache hit. What the key DOES cover is
  the model's own capture directory, RAW/projection_archive, so the thing that
  makes a fresh weekly number is nflmodel.ingest.archive_projections, not
  anything on our side. Export without capturing first and the run looks fresh
  and is wrong -- the exact failure the ordering was supposed to prevent, just
  one step further upstream than it looked.

  That is also the right place for it to live. The anchor is a pre-kickoff
  projection snapshot, and Sleeper's projection for a man who has just been
  ruled out drops on its own; the injury reaches the model through the capture,
  not through a designation field. Measured cost, week 1: 0.5-1.2s for a cache
  hit, 4.0s for a full resimulation.

  LINEUP BEFORE IR. ir.py refuses to reserve anyone Sleeper still has in a
  starting slot, so the optimiser has to bench him first. This one was already
  documented; it is restated here because the new re-optimise steps sit either
  side of it and the reason for the first call is not the reason for the others.

THE MODEL IS A SUBPROCESS, NEVER AN IMPORT. model_proj.py's rule is that the
artifact is the interface, and its concern is that a simulation stall must
never become a lineup that never gets set. A subprocess with a timeout
honours that; an import defeats it. Every fallback already exists -- a failed
export leaves yesterday's artifact, and model_proj refuses one too old and drops
honours that; an import defeats it. Every fallback already exists -- a failed
export leaves yesterday's artifact, and model_proj refuses one too old and drops
to Sleeper's live weekly feed, which is 23 of 57 scoring keys but current.

    python -m robo.cascade              # the whole chain, dry
    python -m robo.cascade --apply      # ... and act on it
"""

import argparse
from datetime import datetime, time as dtime
import json
import os
import subprocess
import sys
import time

from robo import DATA, LEAGUE_ID_2026, ROOT, season, settings

# How long the weekly-projection export may take before we stop waiting and use
# whatever artifact we already hold. Measured at 4.0s for a full resimulation
# and about 1s when nothing has changed, so this is roughly thirty times the
# worst observed run -- generous on purpose, because the cost of waiting is a
# slower job and the cost of cutting it short is a stale number.
EXPORT_TIMEOUT_S = 120

# How long RobonerRoster waits for an actively running RobonerRefresh before proceeding.
REFRESH_WAIT_S = 300

# Maximum seconds a retriggered recovery refresh may run before timing out.
REFRESH_RECOVERY_TIMEOUT_S = 600

# Steps whose failure must NOT stop the chain. A stale weekly projection is
# survivable and model_proj says so out loud; a lineup that never gets set is
# not. Anything outside this set aborts the run.
# A failed odds recompute is soft: playoffs.py already reads the cached file
# under its own MAX_AGE_H, so ros falls back to the last good odds rather than
# to nothing, and losing the whole cascade over it would cost the lineup.
SOFT_STEPS = ("scout", "capture", "export", "stream", "waivers", "odds")

settings.apply(__name__, globals())


# --------------------------------------------------------------------- the pull

def _snapshot() -> dict:
    """What we currently believe, in projarchive's shape, for diffing."""
    from robo import projarchive
    try:
        return projarchive.season_block()
    except Exception:
        return {}


def pull(record: dict | None = None) -> dict:
    """Re-pull every input that can go stale between daily refreshes.

    THE DELTA IS THE POINT, not the freshness. Anything can be re-fetched; what
    a decision log needs is WHICH player changed and in which direction, and
    projarchive already classifies exactly that -- a move is `volume` (his
    projection fell), `roster` (he was designated or traded) or `both`. Reusing
    it means the intra-day diff and the daily one cannot drift apart.

    Rosters and weekly projections are deliberately absent: season.py already
    reads those live through short in-process memos, so they are current on
    every run without anything being done to them.

    SCHEDULES ARE PULLED HERE TOO, and they are not a Sleeper input. The betting
    lines set every defence's value through streaming.expected, and the kickoff
    times decide whether a roster move is inside the blackout at all -- so an
    old schedule does not merely age a number, it mis-states the rule that
    governs whether the run may act. It used to arrive once a day at 05:00 on a
    12-hour TTL, which put an eight-hour-old line under a game-day decision;
    measured on 9 Sep 2026, three of sixteen week-1 games had moved in that
    window, one of them the game our own streaming target was playing in.
    """
    from robo import injuries, projarchive, refresh
    from robo import sleeper_read as api

    before = _snapshot()
    out = {"players": 0, "projections": 0, "injuries": 0}

    out["schedules"] = _pull_schedules()
    # After the schedules: they supply the fixture list the lines attach to.
    out["lines"] = _pull_lines()
    out["players"] = len(api.players(refresh=True))
    try:
        out["projections"] = refresh.pull_projections()
    except Exception as e:
        out["projections_error"] = str(e)[:120]
    d, why = injuries.fetch()
    out["injuries"] = len(d.get("players") or {}) if d else 0
    if not d:
        out["injuries_error"] = why

    after = _snapshot()
    moves = projarchive.diff_blocks(before, after) if before and after else []
    out["changed"] = moves
    if record is not None:
        record.update(out)
    return out


SCHEDULES_TIMEOUT_S = 60


def _pull_schedules() -> str:
    """Re-pull nflverse schedules, then forget what we already read from them.

    A SUBPROCESS, NOT AN IMPORT -- the same rule the rest of the model wears
    here: the artifact is the interface, so a simulation stall cannot become a
    lineup that never gets set. store.cached() writes through a .tmp and keeps
    the existing file when a fetch fails, so the worst case is the snapshot we
    already had, whose age the freshness check then reports.

    CLEARING THE MEMOS IS HALF THE JOB. vegas._schedule and vegas._kickoffs are
    lru_cached and the cascade is one process, so anything that read a line
    before this ran would hold it for the rest of the run and the new file would
    change nothing. Same move as marginal.board.cache_clear() below.

    THE EXIT CODE IS NOT THE ANSWER, THE FILE IS. store.cached() catches a fetch
    failure, keeps the snapshot it already had, prints a warning and returns it
    -- so the process exits 0 having refreshed nothing. Verified against a dead
    upstream on 9 Sep 2026: exit 0, "keeping existing snapshot", file untouched.
    Trusting that would print "refreshed" over a stale line, which is the exact
    failure this step exists to end, so freshness is read off the file's own
    mtime and an unmoved file reports its real age.
    """
    from robo import vegas

    def age_h() -> float | None:
        try:
            return (time.time() - vegas.PARQUET.stat().st_mtime) / 3600.0
        except OSError:
            return None

    before = age_h()
    ok, how = _model_cmd(["nflmodel.ingest.nflverse", "--refresh",
                          "--only", "schedules"], SCHEDULES_TIMEOUT_S)
    vegas._schedule.cache_clear()
    vegas._kickoffs.cache_clear()
    after = age_h()

    if after is None:
        return f"MISSING ({how})" if not ok else "MISSING"
    if before is None or after < before:
        return "refreshed"
    why = how if not ok else "upstream unreachable, kept the snapshot we had"
    return f"KEPT OLD {after:.1f}h ({why})"


LINES_TIMEOUT_S = 90


def _pull_lines() -> str:
    """Refresh ESPN's lines for the whole remaining season -- the look-ahead the
    twenty-minute pulse (four weeks) does not reach.

    Judged by the artifact, not the exit code, for the same reason as the
    schedules: a fetch that fails per week still exits 0 with the nflverse
    fallback written, and "refreshed" over a failed ESPN read is the one
    reading that must not happen.
    """
    from robo import season, vegas
    ok, how = _model_cmd(["nflmodel.ingest.lines", "--season-rest"], LINES_TIMEOUT_S)
    meta = ((vegas._artifact().get("weeks") or {})
            .get(str(season.current_week())) or {})
    if not meta:
        return f"MISSING ({how})"
    if meta.get("status") != "ok":
        return f"ESPN FAILED, nflverse fallback ({meta.get('error') or how})"
    if meta.get("fallbacks"):
        return f"refreshed, {meta['fallbacks']} game(s) on fallback"
    return "refreshed"


def _model_cmd(args: list[str], timeout: int) -> tuple[bool, str]:
    """Run one Roboner NFL model command as a subprocess. Never raises."""
    if not (ROOT / "nflmodel" / "__init__.py").exists():
        return False, f"no Roboner NFL model package under {ROOT}"
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-m"] + args, cwd=str(ROOT),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{args[0]} exceeded {timeout}s; using what we hold"
    except Exception as e:
        return False, f"{args[0]} could not be launched: {str(e)[:100]}"
    if r.returncode != 0:
        return False, f"{args[0]} exited {r.returncode}: {(r.stderr or '')[-160:]}"
    return True, f"{time.time() - t0:.1f}s"


def capture_week(week: int, timeout: int = EXPORT_TIMEOUT_S) -> tuple[bool, str]:
    """Take a fresh pre-kickoff projection snapshot in the Roboner NFL model.

    THIS IS THE STEP THAT MAKES THE EXPORT REGENERATE. The model's artifact key
    fingerprints its own capture directory; nothing on our side is in it. Without
    this the export is a cache hit and the weekly number remains anchored on the
    most recent local capture.

    Captures are additive and never overwritten, so an extra one costs a file.
    On a Sunday the model takes several of its own anyway.
    """
    ok, how = _model_cmd(["nflmodel.ingest.archive_projections",
                          "--week", str(week)], timeout)
    return ok, (f"captured in {how}" if ok else how)


def export_week(week: int, timeout: int = EXPORT_TIMEOUT_S) -> tuple[bool, str]:
    """Regenerate this week's projection, AFTER the pull. Never raises.

    Run as a subprocess from the Roboner root, so nothing in this process imports
    polars, nflreadpy or a decade of play-by-play. See the header for why that
    distinction is not cosmetic.
    """
    ok, how = _model_cmd(["nflmodel.export", "--week", str(week),
                          "--league", "rurffl"], timeout)
    return ok, (f"regenerated in {how}" if ok else how)


def monday_roster_guard(apply: bool = False,
                        league_id: str = LEAGUE_ID_2026,
                        week: int | None = None) -> dict:
    """Protect Monday starters and fill only spots opened by this IR sweep."""
    from robo import ir, lineup, moves
    week = week or season.current_week()
    out = {"week": week, "mode": "monday_guard"}
    out["lineup_before"] = lineup.run(week=week, league_id=league_id,
                                       apply=apply, verbose=False)
    season.invalidate_live()
    out["ir_first"] = ir.run(apply=apply, league_id=league_id, verbose=False)
    season.invalidate_live()
    out["lineup_after_ir"] = lineup.run(week=week, league_id=league_id,
                                         apply=apply, verbose=False)
    out["patch"] = moves.run("free", apply=apply, league_id=league_id,
                              mode="patch", verbose=False)
    season.invalidate_live()
    out["ir_second"] = ir.run(apply=apply, league_id=league_id, verbose=False)

    ir_runs = (out["ir_first"], out["ir_second"])
    parked = {str(m["player_id"]): m for result in ir_runs
              for m in (result.get("reserve") or [])}
    activated = {str(m["player_id"]) for result in ir_runs
                 for m in (result.get("activate") or [])}
    created = [m for pid, m in parked.items() if pid not in activated]
    out["ir_opened"] = len(created)
    out["ir_fills"] = moves.run_ir_fills(len(created), created, apply=apply,
                                          league_id=league_id, verbose=False)
    season.invalidate_live()
    out["lineup_final"] = lineup.run(week=week, league_id=league_id,
                                      apply=apply, verbose=False)
    return out


def _monday_guard_summary(out: dict) -> str:
    starters = out.get("lineup_before") or {}
    patch = out.get("patch") or {}
    fills = out.get("ir_fills") or {}
    bits = [f"starter watch {'changed' if starters.get('changed') else 'clear'}"]
    if patch.get("plans"):
        bits.append(f"{len(patch['plans'])} emergency patch proposal(s)")
    bits.append(f"{out.get('ir_opened', 0)} spot(s) opened by IR")
    if fills.get("plans"):
        tag = "submitted" if fills.get("submitted") else "proposed"
        bits.append(f"{len(fills['plans'])} IR fill(s) {tag}")
    else:
        bits.append("no IR fill")
    bits.append("ordinary moves and claims suppressed")
    return "; ".join(bits)


# -------------------------------------------------------------------- the chain

def run(apply: bool = False, league_id: str = LEAGUE_ID_2026,
        verbose: bool = True, pregame: bool = False) -> dict:
    """The whole sequence. Returns what happened at each step.

    `pregame` is the run that fires ten minutes before a kickoff slot, and it
    skips scout. That is not a cost saving, it is the only step whose output
    cannot reach any decision this run makes: the lineup does not read scout at
    all, and `patch` -- the one roster move still permitted this close to a
    kickoff -- explicitly does not weigh a return date, because an empty slot
    scores zero and anyone startable beats it. The men scout would re-read are
    exactly those whose designation just moved, who by definition have no
    reporting yet. Meanwhile it is the only UNBOUNDED step in the chain, at
    about seventeen seconds a player against a six-hundred-second budget, and
    everything it delays -- including a surprise inactive's patch and the free
    agent that fills the hole -- is the reason the run exists.

    The daily and roster runs keep it, because that is where a date lands in
    the valuation it was written for.
    """
    from robo import construction
    # The roster steps below are each construction sessions of their own; one
    # outer session keeps them from each running the check, and the run ends
    # with it as a named step instead.
    with construction.deferred("cascade", apply=apply):
        return _run(apply, league_id, verbose, pregame)


def _run(apply: bool, league_id: str, verbose: bool, pregame: bool) -> dict:
    from robo import construction, expected, ir, lineup, model_proj, moves, refresh, ros

    log: list = []

    def step(name: str, fn):
        t0 = time.time()
        try:
            detail = fn()
            ok = True
        except Exception as e:
            detail, ok = f"FAILED: {str(e)[:160]}", False
        log.append({"step": name, "ok": ok, "detail": detail,
                    "secs": round(time.time() - t0, 1)})
        if verbose:
            print(f"  {name:<10} {'ok ' if ok else 'FAIL'} {detail} "
                  f"({time.time() - t0:.1f}s)", flush=True)
        if not ok and name not in SOFT_STEPS:
            raise RuntimeError(f"{name} failed, and it is not a soft step")
        return detail

    wk = season.current_week()
    if verbose:
        kind = "PREGAME, " if pregame else ""
        print(f"CASCADE - week {wk}, {kind}"
              f"{'APPLYING' if apply else 'dry run'}\n")

    prec: dict = {}
    step("pull", lambda: _fmt_pull(pull(record=prec)))
    # Named and logged rather than silently absent: a step that vanishes from
    # the output looks identical to one that never ran, and this is the log
    # somebody reads when a lineup went wrong.
    step("scout", (lambda: "skipped: pregame run, no decision here reads it")
         if pregame else (lambda: _scout(prec)))
    step("capture", lambda: capture_week(wk)[1])
    step("export", lambda: export_week(wk)[1])
    step("model", lambda: refresh.pull_model())

    # BEFORE rebuild, because ros.py reads these odds to weight weeks 15-17 and
    # marginal.py reads them as p_playoffs when it prices a move. Recomputed
    # every run rather than once a day: the odds move on results and on the
    # lineups every team can now field, and a decision taken on this morning's
    # odds is taken on last night's league. It is the most expensive step here
    # -- 6.9s at week 1, of which strength() is 2.6s optimising twelve lineups
    # across fourteen remaining weeks -- and it gets cheaper every week as the
    # schedule drains, roughly 0.18s per remaining regular week.
    step("odds", lambda: refresh.build_playoff_odds())

    # Rebuilt here so every decision below reads ONE vintage. Cheap enough that
    # there is no reason not to: expected.build() measures about two seconds.
    def _rebuild():
        d = expected.build(league_id=league_id)
        expected.save(d)
        r = ros.build(league_id=league_id)
        ros.CACHE.write_text(json.dumps(r), encoding="utf-8")
        # The simulator caches a Board per process and it was built from the
        # PREVIOUS artifacts; dropping it here is what stops the roster steps
        # below pricing against the numbers we just replaced.
        from robo import marginal
        marginal.board.cache_clear()
        return f"{len(d['players'])} expected, {len(r['players'])} ros"
    step("rebuild", _rebuild)

    # BEFORE ANY ROSTER OR LINEUP WRITE. A man left on reserve without an
    # IR-eligible designation makes Sleeper refuse everything -- the lineup
    # included -- so with lineup first the run died at its first write and never
    # reached the step that could fix it. If it cannot be fixed, every write
    # step below is skipped by name rather than failing one after another.
    ub: dict = {}

    def _unblock():
        ub.update(ir.unblock(apply=apply, league_id=league_id, verbose=False))
        return _fmt_unblock(ub, apply)
    step("unblock", _unblock)
    stop = "" if ub.get("legal", True) else (ub.get("reason") or "roster frozen")

    if stop:
        for name in ("lineup", "ir", "patch", "fill", "stream"):
            step(name, lambda: f"skipped: roster frozen -- {stop}")
    elif season.monday_guard_active():
        step("monday", lambda: _monday_guard_summary(
            monday_roster_guard(apply=apply, league_id=league_id, week=wk)))
        step("fill", lambda: "suppressed: Monday guard permits only IR-created fills")
        step("stream", lambda: "suppressed: Monday guard")
    else:
        step("lineup", lambda: _lineup(lineup, wk, apply))
        step("ir", lambda: _ir(ir, apply))
        step("lineup2", lambda: _lineup(lineup, wk, apply))
        step("patch", lambda: _moves(moves, "patch", apply))
        step("ir2", lambda: _ir(ir, apply))
        step("fill", lambda: _moves(moves, "fill", apply))
        step("stream", lambda: _stream(moves, wk, apply, league_id))
    # LAST roster step on every branch, frozen and Monday included: whatever
    # the steps above did or could not do, the run ends Sleeper-legal with
    # every starting slot filled, or says why not.
    step("construction", lambda: _construction(
        construction.ensure(week=wk, league_id=league_id, apply=apply,
                            trigger="cascade")))
    step("waivers", lambda: _waiver_watch(league_id))

    prov = model_proj.week_projections(wk)[1]
    return {"week": wk, "applied": bool(apply), "steps": log,
            "pull": prec, "weekly_projection": prov}


def _scout(pulled: dict) -> str:
    """Re-read the reporting for anyone whose DESIGNATION just moved.

    THE FLOOR IS A RULE AND THE DATE IS A JUDGEMENT, and before this step the
    cascade refreshed one and not the other. ESPN gives the earliest week the
    rules allow a man back, and the pull makes that current within the run --
    but expected.py reads the DATED estimate that overrides it out of the
    verdicts file, which only the 06:30 refresh ever wrote. A Thursday IR
    placement therefore got a fresh floor of week 5 and kept yesterday's silence
    about the reporting that says week 7, until the following morning.

    GATED ON A ROSTER-KIND CHANGE, because that is the only thing that can
    produce a new date. projarchive classifies a move as `volume` (his
    projection fell) or `roster` (he was designated or traded); a projection
    drifting a point is not new reporting and does not deserve a model. On a
    quiet run this costs one dictionary lookup.

    Narrowed to those men, because the full pool is about a second a player and
    a minute is too much to spend three times a day on a wire that has not
    moved. needs_judging still decides who is actually re-read -- the
    fingerprint covers the designation, so a new placement forces it.

    EVERY OTHER VERDICT IS CARRIED FORWARD EXPLICITLY. write_verdicts persists
    exactly what it is handed, so a narrow run that passed only its own reuse
    would silently truncate the file to the handful it looked at.
    """
    from robo import scout, scout_queue
    moved = [m["player_id"] for m in (pulled.get("changed") or [])
             if m.get("kind") in ("roster", "both")]
    if not moved:
        drained = scout_queue.drain(verbose=False)
        return (f"no designation moved; queue {drained.get('status')} "
                f"({drained.get('queued', 0)} pending)")
    b = scout.gather(only=moved)
    if not b:
        drained = scout_queue.drain(verbose=False)
        return (f"{len(moved)} designation(s) moved, none in the decision pool; "
                f"queue {drained.get('status')}")
    todo, reuse = scout.needs_judging(b)
    if not todo:
        drained = scout_queue.drain(verbose=False)
        return (f"{len(b)} in scope, all fingerprints unchanged; "
                f"queue {drained.get('status')} ({drained.get('queued', 0)} pending)")
    roster = season.mine()
    ours = {str(pid) for pid in (roster.get("players") or [])}
    starters = {str(pid) for pid in (roster.get("starters") or [])}
    states = season.transaction_states([x["player_id"] for x in todo])
    categories = {}
    for bundle in todo:
        pid = str(bundle["player_id"])
        if season.monday_guard_active() and pid in starters:
            categories[pid] = "monday_starter"
        elif pid in ours:
            categories[pid] = "emergency"
        elif states[pid]["acquisition"] in {"weekly_waiver", "drop_waiver"}:
            categories[pid] = "waiver_candidate"
        else:
            categories[pid] = "background"
    queued = scout_queue.enqueue(todo, categories=categories)
    drained = scout_queue.drain(verbose=False)
    return (f"{len(todo)} queued, {len(reuse)} reused, "
            f"{queued['filtered_recaps']} recap(s) excluded; "
            f"batch {drained.get('status')} ({len(drained.get('completed') or [])} "
            f"completed, {drained.get('queued', 0)} pending)")


def _fmt_pull(p: dict) -> str:
    ch = p.get("changed") or []
    bits = [f"{p['players']} players", f"{p['projections']} projections",
            f"{p['injuries']} injury rows"]
    if p.get("schedules") and p["schedules"] != "refreshed":
        bits.append(f"schedules {p['schedules']}")
    if p.get("lines") and p["lines"] != "refreshed":
        bits.append(f"lines {p['lines']}")
    if p.get("projections_error"):
        bits.append(f"projections KEPT OLD ({p['projections_error']})")
    if p.get("injuries_error"):
        bits.append(f"injuries KEPT OLD ({p['injuries_error']})")
    bits.append(f"{len(ch)} changed" if ch else "nothing changed")
    return ", ".join(bits)


def _lineup(lineup, wk: int, apply: bool) -> str:
    out = lineup.run(week=wk, apply=apply, verbose=False)
    bad = out.get("illegal") or []
    holes = out.get("holes") or []
    if not out.get("changed"):
        return f"optimal at {out.get('total', 0):.1f}" + (f", ILLEGAL: {bad}" if bad else "")
    tag = "applied" if out.get("applied") else "would change"
    return (f"{tag} {out.get('gain', 0):+.1f} to {out.get('total', 0):.1f}"
            + (f", holes {holes}" if holes else "")
            + (f", ILLEGAL: {bad}" if bad else ""))


def _construction(out: dict) -> str:
    issues = out.get("issues") or {}
    open_ = [k for k in ("frozen", "holes", "illegal") if issues.get(k)]
    done = ", ".join(s["kind"] for s in out.get("steps") or [])
    text = out["status"]
    if done:
        text += f" ({done})"
    if open_:
        text += " -- " + "; ".join(
            f"{k}: {issues[k]}" if k != "frozen" else "roster frozen"
            for k in open_)
    return text


def _fmt_unblock(out: dict, apply: bool) -> str:
    if out.get("was_legal", True):
        return "roster legal"
    tag = "done" if apply else "plan"
    steps = "; ".join(s["text"] for s in out.get("steps") or []
                      if not apply or s.get("landed"))
    head = f"was frozen ({out.get('before')}); {tag}: {steps or 'nothing'}"
    return head + ("" if out.get("legal") else f"; STILL FROZEN: {out.get('reason')}")


def _ir(ir, apply: bool) -> str:
    """BLOCKED IS REPORTED FIRST, because `changed` is False when it happens.

    A man whose designation cleared cannot be activated into a full roster, so
    he stays in `target`, so `changed` comes back False -- and the old summary
    read that as "nothing to move" while Sleeper was refusing every transaction
    on the account. The one roster state that freezes everything was the one
    the cascade described as quiet.
    """
    out = ir.run(apply=apply, verbose=False)
    res, act = out.get("reserve") or [], out.get("activate") or []
    stuck = [b for b in (out.get("blocked") or [])
             if b.get("player_id") in set(out.get("current") or [])]
    parts = []
    if stuck:
        parts.append("ROSTER BLOCKED by " + ", ".join(
            f"{b['name']} ({b['status']})" for b in stuck)
            + " -- no move will be accepted until unblock resolves it")
    if out.get("changed"):
        tag = "applied" if out.get("applied") else "would move"
        parts.append(f"{tag}: {len(res)} to reserve, {len(act)} to activate")
    elif not stuck:
        parts.append(f"nothing to move ({out.get('slots', {}).get('ir_used', 0)} on IR)")
    return "; ".join(parts)


def _moves(moves, mode: str, apply: bool) -> str:
    """Through moves.run, never plan_free directly.

    run() is where the gate, the blackout and the decision-log record live. A
    cascade that called the planner and submitted for itself would be a second
    copy of all three, and the one most likely to forget the gate.
    """
    if mode == "fill" and moves.season.slots()["open"] <= 0:
        return "roster full, nothing to fill"
    out = moves.run("free", apply=apply, mode=mode, verbose=False)
    plans = out.get("plans") or []
    if out.get("blackout"):
        return f"blacked out: {out['blackout']}"
    if not plans:
        return "no candidate clears the bar"
    who = ", ".join(p["add"]["name"] for p in plans)
    if out.get("gated"):
        return f"{len(plans)} add(s) WOULD be made ({who}) -- gate shut"
    return f"{len(plans)} add(s): {who}" + ("" if out.get("applied") else " (not submitted)")


def _stream(moves, wk: int, apply: bool, league_id: str) -> str:
    """Swap the defence when this week's lines say somebody free is better.

    The decision lives in moves.stream_defence(), shared with the news pulse's
    line-move repricing, so the cascade and the pulse cannot answer the same
    question two ways. See there for why defences only, why it is exempt from
    the kickoff blackout, and why the board is intersected with what is free.
    """
    return moves.stream_defence(wk, apply=apply, league_id=league_id)["text"]


def _waiver_watch(league_id: str) -> str:
    """Who is sitting on waivers, for Tuesday. Recorded, never acted on here.

    A player another team dropped is not addable until he clears, so this step
    exists to make the next cycle's target set visible in today's run rather
    than to do anything about it. RobonerWaivers is what acts.
    """
    onw = season.on_waivers(league_id)
    if not onw:
        return "nobody on waivers"
    from robo import sleeper_read as api
    players = api.players()
    names = [api.player_name(players, p) for p in list(onw)[:6]]
    return f"{len(onw)} on waivers for Tuesday: " + ", ".join(names)


def is_refresh_running(lock_path=None) -> tuple[bool, str]:
    """Whether RobonerRefresh is actively running right now."""
    from robo.runlock import LOCK, pid_alive
    lp = lock_path or LOCK
    if lp.exists():
        try:
            doc = json.loads(lp.read_text(encoding="utf-8"))
            owner = str(doc.get("owner") or "")
            pid = int(doc.get("pid") or 0)
            if "refresh" in owner.lower():
                if pid and pid_alive(pid):
                    return True, f"locked by {owner} (pid {pid})"
        except Exception:
            pass
    try:
        import psutil
        for p in psutil.process_iter(["pid", "cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if "robo.refresh" in cmd and p.pid != os.getpid():
                return True, f"process running (pid {p.pid})"
    except Exception:
        pass
    return False, ""


def refresh_completed_today(today_str: str | None = None, log_path=None) -> bool:
    """Whether RobonerRefresh has logged a completed run for today."""
    target_date = today_str or datetime.now().strftime("%Y-%m-%d")
    lp = log_path or (ROOT / "refresh.log")
    if not lp.exists():
        return False
    try:
        with lp.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in reversed(lines[-200:]):
            if f"[{target_date} " in line and "=== refresh done:" in line:
                return True
    except Exception:
        pass
    return False


def preflight_refresh(max_wait_s: int = REFRESH_WAIT_S,
                      pregame: bool = False,
                      verbose: bool = True,
                      lock_path=None,
                      log_path=None,
                      now_fn=None) -> dict:
    """Preflight check for RobonerRoster: ensure today's 06:30 refresh completed.

    1. If pregame is True, skips immediately (never delay before kickoff).
    2. If before 06:30 local time, skips immediately (today's refresh not yet due).
    3. If RobonerRefresh is actively running, delays and polls every 5s up to max_wait_s.
    4. If today's refresh did not complete (crashed, failed, or missing), retriggers
       a recovery run via `python -m robo.refresh --no-restart`.
    5. Fails soft: if recovery fails or times out, logs a warning and allows the
       cascade to continue with existing data so lineups are never stranded.
    """
    if pregame:
        return {"status": "skipped", "detail": "pregame run"}

    now = now_fn() if now_fn else datetime.now()
    refresh_due_time = dtime(6, 30)
    if now.time() < refresh_due_time:
        return {"status": "skipped", "detail": "before 06:30 scheduled refresh"}

    # 1. Delay while refresh is currently active
    t0 = time.time()
    deadline = t0 + max_wait_s
    waited = False
    running = False
    why = ""
    while time.time() < deadline:
        running, why = is_refresh_running(lock_path=lock_path)
        if not running:
            break
        waited = True
        if verbose:
            print(f"  preflight  waiting for RobonerRefresh ({why})...", flush=True)
        time.sleep(5)

    if running:
        msg = f"RobonerRefresh still running after {int(time.time() - t0)}s; proceeding with available data"
        if verbose:
            print(f"  preflight  WARN: {msg}", flush=True)
        return {"status": "timed_out", "detail": msg}

    if waited and verbose:
        print(f"  preflight  ok  RobonerRefresh finished after {time.time() - t0:.1f}s", flush=True)

    # 2. Check if today's refresh succeeded
    if refresh_completed_today(today_str=now.strftime("%Y-%m-%d"), log_path=log_path):
        if verbose and not waited:
            print("  preflight  ok  today's refresh completed", flush=True)
        return {"status": "ok", "detail": "today's refresh completed"}

    # 3. Retrigger recovery refresh
    if verbose:
        print("  preflight  WARN: today's 06:30 refresh not completed; retriggering...", flush=True)
    try:
        t_rec = time.time()
        r = subprocess.run([sys.executable, "-m", "robo.refresh", "--no-restart"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           timeout=REFRESH_RECOVERY_TIMEOUT_S)
        ok = (r.returncode == 0) and refresh_completed_today(today_str=now.strftime("%Y-%m-%d"), log_path=log_path)
        detail = (f"recovery refresh completed in {time.time() - t_rec:.1f}s" if ok
                  else f"recovery refresh exited {r.returncode} ({time.time() - t_rec:.1f}s)")
        if verbose:
            print(f"  preflight  {'ok ' if ok else 'FAIL'} {detail}", flush=True)
        return {"status": "retriggered" if ok else "retrigger_failed", "detail": detail}
    except subprocess.TimeoutExpired:
        msg = f"recovery refresh timed out after {REFRESH_RECOVERY_TIMEOUT_S}s; proceeding with available data"
        if verbose:
            print(f"  preflight  WARN: {msg}", flush=True)
        return {"status": "retrigger_timeout", "detail": msg}
    except Exception as e:
        msg = f"could not retrigger refresh: {str(e)[:120]}; proceeding with available data"
        if verbose:
            print(f"  preflight  WARN: {msg}", flush=True)
        return {"status": "retrigger_error", "detail": msg}


def main():
    ap = argparse.ArgumentParser(description="the in-season chain, in order")
    ap.add_argument("--apply", action="store_true",
                    help="actually set the lineup and submit roster moves")
    ap.add_argument("--pregame", action="store_true",
                    help="minutes before kickoff: skip scout (see run.__doc__)")
    a = ap.parse_args()

    # Before acquiring the cascade lock, ensure the morning refresh is not
    # running and has completed. Done outside DecisionRun so that a retriggered
    # refresh can acquire its own lock and not deadlock.
    preflight_refresh(pregame=a.pregame)

    from robo.runlock import DecisionRun
    # Scheduled cascades own the shared writer lock. A news pulse never waits
    # ahead of this path, and the offset watcher schedule avoids start races.
    with DecisionRun("pregame cascade" if a.pregame else "full cascade",
                     wait_s=15 * 60):
        d = run(apply=a.apply, pregame=a.pregame)
    print(f"\nweekly projection in use: {d['weekly_projection']}")
    bad = [s for s in d["steps"] if not s["ok"]]
    if bad:
        print(f"{len(bad)} soft step(s) failed: "
              + ", ".join(s["step"] for s in bad))


if __name__ == "__main__":
    main()
