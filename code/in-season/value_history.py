"""Historical snapshots and like-for-like deltas for expected player value.

The live value is a sum over the weeks still available to the franchise.  A
plain subtraction across a Tuesday rollover therefore calls every completed
game a loss of value.  Comparisons here instead put both vintages on the
CURRENT remaining-week horizon and apply the CURRENT playoff weights to both.
What remains is a change in the player's weekly outlook, not calendar runoff.

Snapshots are intentionally compact and local.  They retain the published
weekly ``final`` values needed to reconstruct a comparison, not the full audit
trace in data/expected.json.  One unchanged snapshot per UTC day is retained;
genuine intraday changes are retained as separate vintages.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from robo import DATA

HISTORY = DATA / "value_history"
SNAPSHOT_SCHEMA = 1


def _series(row: dict) -> dict[int, float]:
    out = {}
    for week, value in (row.get("by_week") or {}).items():
        try:
            final = value.get("final", 0.0) if isinstance(value, dict) else value
            out[int(week)] = float(final or 0.0)
        except (TypeError, ValueError):
            continue
    return out


def compact(table: dict) -> dict:
    """Keep exactly the fields required by the movers UI."""
    players = {}
    for pid, row in (table.get("players") or {}).items():
        series = _series(row)
        players[str(pid)] = {
            "name": row.get("name") or str(pid),
            "pos": row.get("pos"),
            "team": row.get("team"),
            "ros": round(float(row.get("ros") or 0.0), 3),
            "by_week": {str(w): round(v, 3) for w, v in sorted(series.items())},
        }
    return {
        "schema": SNAPSHOT_SCHEMA,
        "computed": float(table.get("computed") or 0.0),
        "week": int(table.get("week") or 0),
        "season": str(table.get("season") or ""),
        "weights": {str(k): float(v) for k, v in (table.get("weights") or {}).items()},
        "players": players,
    }


def _fingerprint(snapshot: dict) -> str:
    body = {k: v for k, v in snapshot.items() if k != "computed"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _read(path: Path) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if doc.get("schema") == SNAPSHOT_SCHEMA else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def snapshots(history: Path | None = None) -> list[dict]:
    """All readable snapshots, oldest first. Corrupt files are ignored."""
    root = history or HISTORY
    if not root.exists():
        return []
    docs = [doc for path in root.glob("expected_*.json.gz")
            if (doc := _read(path)) is not None]
    return sorted(docs, key=lambda d: float(d.get("computed") or 0.0))


def archive(table: dict, history: Path | None = None) -> Path | None:
    """Archive a successful build; suppress only same-day exact duplicates."""
    snap = compact(table)
    if not snap["computed"] or not snap["players"]:
        raise ValueError("cannot archive an empty or undated value table")
    root = history or HISTORY
    root.mkdir(parents=True, exist_ok=True)
    digest = _fingerprint(snap)
    stamp = datetime.fromtimestamp(snap["computed"], timezone.utc)

    # Preserve a daily heartbeat even when values do not move.  It lets "day
    # over day" mean about one day rather than silently reaching back a week.
    day_prefix = f"expected_{stamp:%Y%m%d}T"
    for path in root.glob(f"{day_prefix}*.json.gz"):
        old = _read(path)
        if old is not None and _fingerprint(old) == digest:
            return path

    path = root / f"expected_{stamp:%Y%m%dT%H%M%SZ}_{digest}.json.gz"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(snap, fh, separators=(",", ":"))
    tmp.replace(path)
    return path


def baseline(current: dict, days: int, history: Path | None = None,
             method: str | None = None) -> dict | None:
    """Latest snapshot at least ``days`` old and from the same season.

    An OBSERVED snapshot always beats a reconstructed one of the same age.
    `robo/value_backfill.py` writes reconstructions of vintages that predate
    this archive, and each knows only one half of a value change -- projection
    drift or news. They are a floor under the comparison, not a substitute for
    having been there, so they are only returned when nothing was observed at
    that age, or when a caller asks for one half by name.
    """
    target = float(current.get("computed") or 0.0) - days * 86400
    season = str(current.get("season") or "")
    eligible = [d for d in snapshots(history)
                if float(d.get("computed") or 0.0) <= target
                and str(d.get("season") or "") == season]
    if method is not None:
        eligible = [d for d in eligible if d.get("method") == method]
        return eligible[-1] if eligible else None
    observed = [d for d in eligible if not d.get("reconstructed")]
    return (observed or eligible)[-1] if eligible else None


def compare(current: dict, prior: dict | None) -> dict[str, dict]:
    """Player deltas on the common, current-weighted remaining-week horizon."""
    if not prior:
        return {}
    weights = {int(k): float(v) for k, v in (current.get("weights") or {}).items()}
    out = {}
    old_players = prior.get("players") or {}
    for pid, row in (current.get("players") or {}).items():
        old = old_players.get(str(pid))
        if not old:
            continue
        now_series, old_series = _series(row), _series(old)
        common = sorted(set(now_series) & set(old_series) & set(weights))
        if not common:
            continue
        now_value = sum(now_series[w] * weights[w] for w in common)
        old_value = sum(old_series[w] * weights[w] for w in common)
        delta = now_value - old_value
        out[str(pid)] = {
            "current": round(now_value, 2),
            "prior_adjusted": round(old_value, 2),
            "delta": round(delta, 2),
            "pct": round(100.0 * delta / old_value, 1) if abs(old_value) >= 0.01 else None,
            "weeks": common,
            "runoff_removed": round(float(old.get("ros") or 0.0) - old_value, 2),
        }
    return out


def age_label(current: dict, prior: dict | None) -> str:
    if not prior:
        return "unavailable"
    hours = (float(current.get("computed") or 0.0)
             - float(prior.get("computed") or 0.0)) / 3600
    return f"{hours / 24:.1f}d ago" if hours >= 36 else f"{hours:.1f}h ago"


def seed() -> Path:
    """Archive the current expected.json without recomputing anything."""
    from robo import expected
    table = json.loads(expected.CACHE.read_text(encoding="utf-8"))
    return archive(table)  # type: ignore[return-value]


if __name__ == "__main__":
    print(seed())
