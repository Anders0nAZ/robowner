"""Reconstruct value-table vintages from evidence that predates the archive.

WHY THIS IS NEEDED. `value_history` only began archiving `expected.json` on
15 Sep 2026, so a week-over-week comparison has nothing to compare against
until the 22nd -- on a bot that had been running for weeks. Two other archives
reach further back and, between them, say most of what a missing snapshot
would have said.

WHY IT IS TWO NUMBERS AND NEVER ONE. The archives cover DIFFERENT HALVES of a
value change, and a blended figure would be confidently wrong.

  * `data/raw/proj_archive/` (daily since 4 Sep) holds Sleeper's weekly
    projections. That is the `provider` term, and substituting it reconstructs
    PROJECTION DRIFT exactly for every player.
  * It reconstructs nothing else, because Sleeper does not propagate an
    absence into future weeks. Sam Darnold was ruled out 4-6 weeks on 13 Sep
    and his weeks 3-6 moved 23.0->21.0, 15.3->16.2 across that bracket: noise,
    in both directions. `expected.py`'s availability ramp is the only thing
    that applies the absence, and availability was never archived.
  * `data/news_events/` (since 12 Sep) holds the missing half. Every recorded
    cascade froze `pre_ros`, `post_ros` and a per-week `by_week` delta for
    each affected player, so NEWS-DRIVEN CHANGE can be replayed backwards
    exactly -- for the players an event touched, which is precisely the set
    that moved for that reason.

So a backfilled comparison reports projection drift and news-driven change as
separate columns. Their sum is the best available estimate of the total, and
it is presented as a sum of two measured things rather than as one measurement.
A single blended number would rank the wrong movers and look authoritative
doing it.

THE ARITHMETIC IS EXACT, NOT FITTED. `expected.py` computes
`final = a * max(provider, s1 + miss * s2)` and stores all five terms per week.
Verified against the live table: `pts == final` in all 12,106 rows. So
substituting an archived `provider` and re-running the same expression
reproduces what that week's value WOULD have been, holding everything the
archives do not carry at today's value.

Reconstructed snapshots are written into `data/value_history/` alongside the
observed ones, carrying `reconstructed: true` and the `method` that built
them. `value_history.baseline()` prefers an observed snapshot whenever one
exists at the age asked for, so these are a floor under the comparison rather
than a replacement for it.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from robo import DATA, expected, news_audit, value_history

PROJ_ARCHIVE = DATA / "raw" / "proj_archive"

# `s1` is the HEALTHY baseline and `provider` is the weekly feed. They are the
# same quantity for a healthy man -- equal in 12,032 of 12,106 live rows -- and
# deliberately decoupled for a sidelined one, whose baseline is what he would
# be worth if he played. So the ratio is carried across only where they agree;
# where they disagree the baseline is held, because it is not what the weekly
# feed was measuring.
SAME_TERM_TOL = 1e-6


def captures() -> list[dict]:
    """Projection captures, oldest first, each with its path."""
    out = []
    for path in sorted(PROJ_ARCHIVE.glob("proj_*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("weeks"):
            doc["_path"] = str(path)
            out.append(doc)
    return sorted(out, key=lambda d: float(d.get("captured") or 0))


def _week_final(block: dict, provider_old: float | None) -> float:
    """Re-run expected.py's own expression with one term swapped."""
    a = float(block.get("a") or 0.0)
    s1 = float(block.get("s1") or 0.0)
    s2 = float(block.get("s2") or 0.0)
    miss = float(block.get("miss") or 0.0)
    provider = float(block.get("provider") or 0.0)
    if provider_old is None:
        return float(block.get("final") or 0.0)
    s1_old = provider_old if abs(s1 - provider) <= SAME_TERM_TOL else s1
    return a * max(provider_old, s1_old + miss * s2)


def projection_vintage(current: dict, capture: dict) -> dict:
    """Today's table with the archived weekly projections put back in."""
    weeks = {str(w): {str(p): v for p, v in (row or {}).items()}
             for w, row in (capture.get("weeks") or {}).items()}
    players, touched = {}, 0
    for pid, row in (current.get("players") or {}).items():
        series, changed = {}, False
        for w, block in (row.get("by_week") or {}).items():
            old = weeks.get(str(w), {}).get(str(pid))
            value = _week_final(block, None if old is None else float(old))
            series[str(w)] = round(value, 3)
            if old is not None and abs(value - float(block.get("final") or 0.0)) > 1e-6:
                changed = True
        touched += bool(changed)
        players[str(pid)] = {
            "name": row.get("name") or str(pid), "pos": row.get("pos"),
            "team": row.get("team"),
            "ros": round(sum(series.values()), 3),
            "by_week": series,
        }
    return _wrap(current, capture.get("captured"), players, "projection",
                 f"Sleeper weekly projections as captured "
                 f"{capture.get('captured_iso')}, substituted into today's "
                 f"availability and role terms. {touched} player(s) moved.")


def news_vintage(current: dict, cutoff: float) -> dict:
    """Today's table with every news-driven change since `cutoff` unwound.

    Walks the recorded cascades backwards subtracting the per-week deltas the
    rebuild measured at the time. Exact for the players an event touched and
    silent for everyone else, which is the honest shape: this archive knows
    about news and nothing else.
    """
    undo: dict[str, dict[str, float]] = {}
    replayed = 0
    for doc in news_audit.events(limit=None):
        if float(doc.get("at") or 0) <= cutoff:
            continue
        replayed += 1
        for pid, delta in ((doc.get("action") or {}).get("event_deltas") or {}).items():
            per_week = undo.setdefault(str(pid), {})
            for w, value in (delta.get("by_week") or {}).items():
                try:
                    per_week[str(w)] = per_week.get(str(w), 0.0) + float(value or 0.0)
                except (TypeError, ValueError):
                    continue

    players = {}
    for pid, row in (current.get("players") or {}).items():
        back = undo.get(str(pid)) or {}
        series = {}
        for w, block in (row.get("by_week") or {}).items():
            now = float(block.get("final") or 0.0)
            series[str(w)] = round(now - back.get(str(w), 0.0), 3)
        players[str(pid)] = {
            "name": row.get("name") or str(pid), "pos": row.get("pos"),
            "team": row.get("team"),
            "ros": round(sum(series.values()), 3),
            "by_week": series,
        }
    return _wrap(current, cutoff, players, "news",
                 f"{replayed} recorded cascade(s) since "
                 f"{news_audit.local_time(cutoff)} unwound from today's table. "
                 f"{len(undo)} player(s) affected.")


def _wrap(current: dict, stamp, players: dict, method: str, basis: str) -> dict:
    return {
        "schema": value_history.SNAPSHOT_SCHEMA,
        "computed": float(stamp or 0.0),
        "week": int(current.get("week") or 0),
        "season": str(current.get("season") or ""),
        "weights": {str(k): float(v) for k, v in (current.get("weights") or {}).items()},
        "players": players,
        "reconstructed": True,
        "method": method,
        "basis": basis,
    }


def _write(snap: dict, history: Path | None = None) -> Path:
    root = history or value_history.HISTORY
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(snap["computed"], timezone.utc)
    path = root / f"expected_{stamp:%Y%m%dT%H%M%SZ}_{snap['method']}.json.gz"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(snap, fh, separators=(",", ":"))
    tmp.replace(path)
    return path


def build(write: bool = False, history: Path | None = None) -> list[dict]:
    """One reconstructed vintage per projection capture, plus its news sibling."""
    current = expected.load()
    observed = [d for d in value_history.snapshots(history) if not d.get("reconstructed")]
    earliest_observed = min((float(d.get("computed") or 0) for d in observed), default=None)
    # A NEWS VINTAGE CANNOT PREDATE THE NEWS CORPUS. Unwinding "everything
    # since 3 Sep" from a store that begins on the 12th produces the 12th's
    # table wearing the 3rd's date -- a baseline that would be picked up by a
    # week-over-week comparison and silently credited with nine days it has no
    # evidence for. The projection half has no such floor; its archive really
    # does reach 4 Sep.
    news_rows = news_audit.events(limit=None)
    earliest_news = min((float(d.get("at") or 0) for d in news_rows), default=None)

    out = []
    for capture in captures():
        stamp = float(capture.get("captured") or 0)
        if not stamp or (earliest_observed and stamp >= earliest_observed):
            # An observed snapshot already covers this moment, and an
            # observation always beats a reconstruction of one.
            continue
        snaps = [projection_vintage(current, capture)]
        if earliest_news is not None and stamp >= earliest_news:
            snaps.append(news_vintage(current, stamp))
        for snap in snaps:
            if write:
                snap["_path"] = str(_write(snap, history))
            out.append(snap)
    return out


def _movers(current: dict, snap: dict, top: int = 10) -> list[tuple]:
    changes = value_history.compare(current, snap)
    rows = [(current["players"][pid]["name"], c["delta"])
            for pid, c in changes.items() if pid in current.get("players", {})]
    return sorted(rows, key=lambda r: -abs(r[1]))[:top]


def _main() -> None:
    ap = argparse.ArgumentParser(description="Reconstruct value-table vintages.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Reconstruct and report without writing anything.")
    ap.add_argument("--check", metavar="NAME",
                    help="Spot-check one player's split across the oldest vintage.")
    args = ap.parse_args()

    current = expected.load()
    snaps = build(write=not args.dry_run)
    if not snaps:
        print("Nothing to reconstruct: observed snapshots already cover every capture.")
        return

    by_method: dict[str, list[dict]] = {}
    for s in snaps:
        by_method.setdefault(s["method"], []).append(s)
    for method, rows in sorted(by_method.items()):
        rows.sort(key=lambda r: r["computed"])
        span = (news_audit.local_time(rows[0]["computed"]),
                news_audit.local_time(rows[-1]["computed"]))
        print(f"{method:<12}{len(rows):>3} vintage(s)   {span[0]}  ->  {span[1]}")
    print(("dry run, nothing written" if args.dry_run
           else f"wrote {len(snaps)} snapshot(s) to {value_history.HISTORY}"))

    oldest = {m: rows[0] for m, rows in by_method.items()}
    for method, snap in sorted(oldest.items()):
        print(f"\ntop movers vs the oldest {method} vintage "
              f"({news_audit.local_time(snap['computed'])}):")
        print(f"  {snap['basis']}")
        for name, delta in _movers(current, snap):
            print(f"    {name:<26}{delta:+8.2f}")

    if args.check:
        print(f"\nsplit for {args.check}:")
        target = next((pid for pid, r in current["players"].items()
                       if str(r.get("name", "")).lower() == args.check.lower()), None)
        if target is None:
            print("  not in the valuation")
            return
        for method, snap in sorted(oldest.items()):
            change = value_history.compare(current, snap).get(target)
            print(f"  {method:<12}{(change or {}).get('delta', 0.0):+8.2f}")


if __name__ == "__main__":
    _main()
