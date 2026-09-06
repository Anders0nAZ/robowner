"""The in-season chain, in order, on one snapshot.

WHAT THIS IS FOR. A man in an active slot gets hurt on a Thursday. What should
happen is a sequence: bench him, move him to injured reserve, re-optimise the
lineup around the gap, fill the roster spot that just opened, re-optimise again
because the man we added might be startable today, and note who is sitting on
waivers for Tuesday. Every one of those steps existed. The SEQUENCE did not.

WHAT WAS ACTUALLY HAPPENING. Five gaps, all found by reading the live schedule
rather than the code:

  * `RobonerMoves` was never registered, so `--mode ros` and `--mode block` had
    never run at all -- the .vbs existed, the scheduled task did not;
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
artifact is the interface, and its concern is that a stall in the other repo
must never become a lineup that never gets set. A subprocess with a timeout
honours that; an import defeats it. Every fallback already exists -- a failed
export leaves yesterday's artifact, and model_proj refuses one too old and drops
to Sleeper's live weekly feed, which is 23 of 57 scoring keys but current.

    python -m robo.cascade              # the whole chain, dry
    python -m robo.cascade --apply      # ... and act on it
"""

import argparse
import json
import subprocess
import sys
import time

from robo import DATA, LEAGUE_ID_2026, MODEL_ROOT, ROOT, season, settings

# How long the weekly-projection export may take before we stop waiting and use
# whatever artifact we already hold. Measured at 4.0s for a full resimulation
# and about 1s when nothing has changed, so this is roughly thirty times the
# worst observed run -- generous on purpose, because the cost of waiting is a
# slower job and the cost of cutting it short is a stale number.
EXPORT_TIMEOUT_S = 120

# Steps whose failure must NOT stop the chain. A stale weekly projection is
# survivable and model_proj says so out loud; a lineup that never gets set is
# not. Anything outside this set aborts the run.
SOFT_STEPS = ("capture", "export", "waivers")

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
    """Re-pull every Sleeper input that can go stale between daily refreshes.

    THE DELTA IS THE POINT, not the freshness. Anything can be re-fetched; what
    a decision log needs is WHICH player changed and in which direction, and
    projarchive already classifies exactly that -- a move is `volume` (his
    projection fell), `roster` (he was designated or traded) or `both`. Reusing
    it means the intra-day diff and the daily one cannot drift apart.

    Rosters and weekly projections are deliberately absent: season.py already
    reads those live through short in-process memos, so they are current on
    every run without anything being done to them.
    """
    from robo import injuries, projarchive, refresh
    from robo import sleeper_read as api

    before = _snapshot()
    out = {"players": 0, "projections": 0, "injuries": 0}

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


def _model_cmd(args: list[str], timeout: int) -> tuple[bool, str]:
    """Run one NFL Model command as a subprocess. Never raises."""
    if not MODEL_ROOT.exists():
        return False, f"no NFL Model tree at {MODEL_ROOT}"
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-m"] + args, cwd=str(MODEL_ROOT),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{args[0]} exceeded {timeout}s; using what we hold"
    except Exception as e:
        return False, f"{args[0]} could not be launched: {str(e)[:100]}"
    if r.returncode != 0:
        return False, f"{args[0]} exited {r.returncode}: {(r.stderr or '')[-160:]}"
    return True, f"{time.time() - t0:.1f}s"


def capture_week(week: int, timeout: int = EXPORT_TIMEOUT_S) -> tuple[bool, str]:
    """Take a fresh pre-kickoff projection snapshot in the NFL Model.

    THIS IS THE STEP THAT MAKES THE EXPORT REGENERATE. The model's artifact key
    fingerprints its own capture directory; nothing on our side is in it. Without
    this the export is a cache hit and the weekly number is whatever the other
    repo's cron last produced -- which is precisely the schedule dependency the
    cascade exists to remove.

    Captures are additive and never overwritten, so an extra one costs a file.
    On a Sunday the model takes several of its own anyway.
    """
    ok, how = _model_cmd(["nflmodel.ingest.archive_projections",
                          "--week", str(week)], timeout)
    return ok, (f"captured in {how}" if ok else how)


def export_week(week: int, timeout: int = EXPORT_TIMEOUT_S) -> tuple[bool, str]:
    """Regenerate this week's projection, AFTER the pull. Never raises.

    Run as a subprocess against the other repo's own interpreter working
    directory, so nothing in this process imports polars, nflreadpy or a decade
    of play-by-play. See the header for why that distinction is not cosmetic.
    """
    ok, how = _model_cmd(["nflmodel.export", "--week", str(week),
                          "--league", "rurffl"], timeout)
    return ok, (f"regenerated in {how}" if ok else how)


# -------------------------------------------------------------------- the chain

def run(apply: bool = False, league_id: str = LEAGUE_ID_2026,
        verbose: bool = True) -> dict:
    """The whole sequence. Returns what happened at each step."""
    from robo import expected, ir, lineup, model_proj, moves, refresh, ros

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
        print(f"CASCADE - week {wk}, {'APPLYING' if apply else 'dry run'}\n")

    prec: dict = {}
    step("pull", lambda: _fmt_pull(pull(record=prec)))
    step("capture", lambda: capture_week(wk)[1])
    step("export", lambda: export_week(wk)[1])
    step("model", lambda: refresh.pull_model())

    # Rebuilt here so every decision below reads ONE vintage. Cheap enough that
    # there is no reason not to: expected.build() measures about two seconds.
    def _rebuild():
        d = expected.build(league_id=league_id)
        expected.CACHE.write_text(json.dumps(d), encoding="utf-8")
        r = ros.build(league_id=league_id)
        ros.CACHE.write_text(json.dumps(r), encoding="utf-8")
        # The simulator caches a Board per process and it was built from the
        # PREVIOUS artifacts; dropping it here is what stops the roster steps
        # below pricing against the numbers we just replaced.
        from robo import marginal
        marginal.board.cache_clear()
        return f"{len(d['players'])} expected, {len(r['players'])} ros"
    step("rebuild", _rebuild)

    step("lineup", lambda: _lineup(lineup, wk, apply))
    step("ir", lambda: _ir(ir, apply))
    step("lineup2", lambda: _lineup(lineup, wk, apply))
    step("fill", lambda: _moves(moves, "fill", apply))
    step("lineup3", lambda: _lineup(lineup, wk, apply))
    step("waivers", lambda: _waiver_watch(league_id))

    prov = model_proj.week_projections(wk)[1]
    return {"week": wk, "applied": bool(apply), "steps": log,
            "pull": prec, "weekly_projection": prov}


def _fmt_pull(p: dict) -> str:
    ch = p.get("changed") or []
    bits = [f"{p['players']} players", f"{p['projections']} projections",
            f"{p['injuries']} injury rows"]
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


def _ir(ir, apply: bool) -> str:
    out = ir.run(apply=apply, verbose=False)
    res, act = out.get("reserve") or [], out.get("activate") or []
    if not out.get("changed"):
        return f"nothing to move ({out.get('slots', {}).get('ir_used', 0)} on IR)"
    tag = "applied" if out.get("applied") else "would move"
    return f"{tag}: {len(res)} to reserve, {len(act)} to activate"


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


def main():
    ap = argparse.ArgumentParser(description="the in-season chain, in order")
    ap.add_argument("--apply", action="store_true",
                    help="actually set the lineup and submit roster moves")
    a = ap.parse_args()
    d = run(apply=a.apply)
    print(f"\nweekly projection in use: {d['weekly_projection']}")
    bad = [s for s in d["steps"] if not s["ok"]]
    if bad:
        print(f"{len(bad)} soft step(s) failed: "
              + ", ".join(s["step"] for s in bad))


if __name__ == "__main__":
    main()
