"""Ten-minute injury and opportunity watcher.

This is deliberately a small poller, not the daily refresh.  It reads the
current weekly projection (with ETag), ESPN injuries, Sleeper trending and PFT;
only a material event pays for model rebuilds or transaction evaluation.

    python -m robo.newswatch             # poll; act only if the global gate is open
    python -m robo.newswatch --dry-run   # detect and value, never submit
"""

import argparse
import hashlib
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from robo import DATA, LEAGUE_ID_2026, ROOT, injuries, rankings, season

STATE = DATA / "news_watch.json"
LOCK = DATA / "news_watch.lock"
LOG = ROOT / "news-watch.log"
AUDIT_DIR = DATA / "news_events"
PFT_RSS = "https://www.nbcsports.com/profootballtalk.rss"
POINT_MOVE = 2.0
TOP_TRENDING = 5
SKILL = {"QB", "RB", "WR", "TE"}


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path: Path, doc) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(path)


def _render_audit(doc: dict) -> str:
    action = doc.get("action") or {}
    lines = [f"NEWS EVENT {doc.get('fingerprint')}",
             datetime.fromtimestamp(float(doc.get("at") or 0), timezone.utc).isoformat(),
             "DRY RUN - NO SUBMISSION" if doc.get("dry_run") else "SUBMISSION PATH REQUESTED",
             ""]
    for e in doc.get("events") or []:
        lines.append(f"EVENT  {e.get('name') or e.get('player_id')}: "
                     + "; ".join(e.get("reasons") or []))
        for c in e.get("changes") or []:
            lines.append(f"       {c.get('field')}: {c.get('before')} -> {c.get('after')}")
    timing = doc.get("timing") or {}
    if timing:
        lines += ["", "TIMING  " + str(timing.get("summary") or timing)]
        for pid, why in (timing.get("quarantined") or {}).items():
            lines.append(f"  QUARANTINE {pid}: {why}")
    lines += ["", "MEASURED ROS DELTAS"]
    for pid, d in (action.get("event_deltas") or {}).items():
        provider = (doc.get("provider_at_trigger") or {}).get(pid) or {}
        sleeper = (f"; Sleeper-now {float(provider.get('points_before') or 0):.2f}"
                   f" -> {float(provider.get('points_at_trigger') or 0):.2f}"
                   if provider else "")
        lines.append(f"  {d.get('name') or pid} ({d.get('pos') or '?'} {d.get('team') or '-'}, "
                     f"{d.get('ownership') or 'ownership not frozen'}): "
                     f"{d.get('pre_ros', 0):.2f} -> {d.get('post_ros', 0):.2f} "
                     f"({d.get('delta_ros', 0):+.2f}); edge={d.get('causal_edge') or 'none'}; "
                     f"complete={bool(d.get('complete'))}{sleeper}")
    candidates = action.get("candidate_checks") or []
    if candidates:
        lines += ["", "ACQUISITION SCREEN"]
        for c in candidates:
            lines.append(f"  {c.get('name') or c.get('player_id')}: "
                         f"{c.get('outcome', 'rejected')} at {c.get('stage', 'unknown')}; "
                         f"event {float(c.get('delta_ros') or 0):+.2f}; {c.get('reason')}")
    drops = action.get("drop_checks") or []
    if drops:
        lines += ["", "INCUMBENT DROP CHECKS"]
        for d in drops:
            lines.append(f"  for {d.get('candidate_id')}, {d.get('drop_name') or d.get('drop_id')} "
                         f"({float(d.get('drop_ros') or 0):.2f}): "
                         f"{'eligible' if d.get('eligible') else 'excluded'}; {d.get('reason')}")
    proposals = list(action.get("free_proposals") or [])
    proposals += [c for s in (action.get("claim_proposals") or []) for c in s.get("claims") or []]
    lines += ["", "PROPOSALS"]
    if not proposals:
        lines.append("  none")
    for p in proposals:
        lines.append(f"  ADD {p['add']['name']} {p.get('add_value', 0):.2f}; "
                     f"DROP {p['drop']['name']} {p.get('drop_value', 0):.2f}; "
                     f"gain {p.get('gain', 0):+.2f}")
    rejects = action.get("rejections") or []
    if rejects:
        lines += ["", "REJECTIONS"]
        for r in rejects:
            lines.append(f"  {r.get('player_id')}: {r.get('reason')}")
    if doc.get("source_errors"):
        lines += ["", "SOURCE FAILURES"] + [f"  {x}" for x in doc["source_errors"]]
    lines += ["", "FINAL STATE",
              f"  reevaluation {float(action.get('duration_seconds') or 0):.1f}s; "
              f"free gate={'closed' if action.get('free_gated') else 'open'}; "
              f"waiver gate={'closed' if action.get('claims_gated') else 'open'}; "
              f"submitted={len(action.get('free_submitted') or []) + len(action.get('claims_submitted') or [])}"]
    return "\n".join(lines) + "\n"


def _write_audit(fingerprint: str, doc: dict) -> tuple[Path, Path]:
    """Write-once event evidence; later polls never rewrite history."""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = AUDIT_DIR / f"{stamp}_{fingerprint}.json"
    n = 1
    while path.exists():
        path = AUDIT_DIR / f"{stamp}_{fingerprint}_{n}.json"
        n += 1
    _write(path, doc)
    report = path.with_suffix(".txt")
    report.write_text(_render_audit(doc), encoding="utf-8")
    return path, report


def _log(text: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class SingleInstance:
    def __enter__(self):
        try:
            if LOCK.exists() and time.time() - LOCK.stat().st_mtime > 15 * 60:
                LOCK.unlink()
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            raise RuntimeError("another news watcher is still running")
        return self

    def __exit__(self, *_):
        try:
            LOCK.unlink()
        except OSError:
            pass


def _week_url(week: int) -> str:
    pos = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF"))
    return (f"https://api.sleeper.app/projections/nfl/{season.SEASON}/{week}"
            f"?season_type=regular&{pos}")


def weekly_snapshot(prior: dict, week: int) -> tuple[dict, str, bool]:
    headers = {}
    if prior.get("weekly_etag"):
        headers["If-None-Match"] = prior["weekly_etag"]
    r = requests.get(_week_url(week), headers=headers, timeout=30)
    if r.status_code == 304:
        return prior.get("weekly") or {}, prior.get("weekly_etag") or "", False
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 500:
        raise ValueError(f"Sleeper weekly feed returned only {len(rows)} rows")
    try:
        scoring = season.scoring(LEAGUE_ID_2026)
    except Exception:
        scoring = {}
    out = {}
    for row in rows:
        p = row.get("player") or {}
        pos = p.get("position") or ((p.get("fantasy_positions") or [None])[0])
        if pos not in SKILL:
            continue
        stats = row.get("stats") or {}
        try:
            pts = rankings.custom_points(stats, scoring) if scoring else float(
                stats.get("pts_half_ppr") or stats.get("pts_ppr") or 0.0)
        except Exception:
            pts = 0.0
        out[str(row["player_id"])] = {
            "name": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
            "team": p.get("team") or row.get("team"), "pos": pos,
            "status": p.get("injury_status"),
            "body": p.get("injury_body_part"),
            "news_updated": p.get("news_updated"),
            "points": round(pts, 3), "game_id": row.get("game_id"),
        }
    return out, str(r.headers.get("ETag") or ""), True


def trending() -> list[str]:
    from robo import sleeper_read as api
    return [str(r["player_id"]) for r in api.trending("add", 24, TOP_TRENDING)]


def _article_text(url: str) -> str:
    try:
        body = requests.get(url, timeout=20).text
    except Exception:
        return ""
    paras = re.findall(r"<p[^>]*>(.*?)</p>", body, flags=re.I | re.S)
    clean = [html.unescape(re.sub(r"<[^>]+>", " ", p)) for p in paras]
    return " ".join(re.sub(r"\s+", " ", p).strip() for p in clean if p)


def pft_items() -> list[dict]:
    r = requests.get(PFT_RSS, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.findall("./channel/item"):
        row = {k: item.findtext(k) or "" for k in ("title", "description", "link", "pubDate")}
        row["source"] = "PFT"
        row["published"] = row.pop("pubDate")
        out.append(row)
    return out


def detect(prior: dict, current: dict, espn: dict,
           pft: list[dict], top: list[str]) -> list[dict]:
    old = prior.get("weekly") or {}
    seen = set(prior.get("pft_seen") or [])
    old_top = set(prior.get("trending_top") or [])
    events = {}

    def add(pid: str, reason: str, item: dict | None = None,
            field: str | None = None, before=None, after=None):
        e = events.setdefault(pid, {"player_id": pid,
                                    "name": (current.get(pid) or {}).get("name"),
                                    "reasons": [], "pft": [],
                                    "changes": []})
        e["reasons"].append(reason)
        if field:
            e["changes"].append({"field": field, "before": before, "after": after})
        if item:
            e["pft"].append(item)

    for pid, now in current.items():
        was = old.get(pid)
        if not was:
            continue
        if now.get("status") != was.get("status"):
            add(pid, f"status {was.get('status')} -> {now.get('status')}",
                field="status", before=was.get("status"), after=now.get("status"))
        if now.get("news_updated") != was.get("news_updated"):
            add(pid, "Sleeper news timestamp changed", field="news_updated",
                before=was.get("news_updated"), after=now.get("news_updated"))
        a, b = float(was.get("points") or 0), float(now.get("points") or 0)
        if (bool(a) != bool(b)) or abs(b - a) >= POINT_MOVE:
            add(pid, f"weekly projection {a:.2f} -> {b:.2f}",
                field="weekly_projection", before=a, after=b)

    # ESPN is the structured authority and can move before Sleeper's player
    # object does.  Compare only fields that communicate a new availability
    # fact; cache generation time itself is intentionally excluded.
    old_espn = prior.get("espn") or {}
    if "espn" in prior:  # first poll establishes a baseline, never a transaction
        for pid in set(old_espn) | set(espn):
            if pid not in current:
                continue
            was, now = old_espn.get(pid) or {}, espn.get(pid) or {}
            if not was:
                add(pid, f"ESPN injury entry: {now.get('designation') or now.get('espn_status')}",
                    field="espn_entry", before=None, after=now)
                continue
            for key, label in (("designation", "designation"),
                               ("return_date", "return date"),
                               ("as_of", "injury timestamp")):
                if now.get(key) != was.get(key):
                    add(pid, f"ESPN {label} {was.get(key)} -> {now.get(key)}",
                        field=f"espn_{key}", before=was.get(key), after=now.get(key))

    names = [(pid, (r.get("name") or "").lower()) for pid, r in current.items()
             if len((r.get("name") or "").split()) >= 2]
    for item in pft if "pft_seen" in prior else []:
        if item.get("link") in seen:
            continue
        hay = f"{item.get('title')} {item.get('description')}".lower()
        if not re.search(r"\b(out|miss|injur|surgery|return|back|start|role|inactive)\w*\b", hay):
            continue
        for pid, name in names:
            if name and name in hay:
                full = dict(item)
                full["analysis"] = _article_text(item.get("link") or "")
                add(pid, f"PFT: {item.get('title')}", full,
                    field="pft_report", before=None, after=item.get("link"))

    for pid in top if "trending_top" in prior else []:
        if pid not in old_top and pid in current:
            add(pid, "entered Sleeper top-five trending adds", field="trending_top5",
                before=False, after=True)
    return list(events.values())


def affected_room(events: list[dict], rows: dict) -> set[str]:
    # A free agent can legitimately have team=None. None is not a team room:
    # expanding (None, WR) once pulled every unsigned receiver in the database
    # into a single 1,059-player event. The primary still remains visible in
    # the event record; without an NFL team there is simply no room to expand.
    pairs = {((rows.get(e["player_id"]) or {}).get("team"),
              (rows.get(e["player_id"]) or {}).get("pos")) for e in events}
    pairs = {(team, pos) for team, pos in pairs if team and pos in SKILL}
    return {pid for pid, r in rows.items() if (r.get("team"), r.get("pos")) in pairs}


def _fingerprint(events: list[dict], affected: set[str]) -> str:
    payload = {"events": events, "affected": sorted(affected)}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def _timestamp(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Sleeper sometimes uses milliseconds.
        return float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
    try:
        return parsedate_to_datetime(str(value)).timestamp()
    except Exception:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None


def _active_contradiction(pid: str, bounds: dict, rows: dict, espn: dict) -> str:
    """Return a quarantine reason for a hard bound contradicted by newer facts."""
    erow = espn.get(pid) or {}
    status = str(erow.get("designation") or erow.get("espn_status") or "").upper()
    active = bool(erow) and status in {"ACTIVE", "HEALTHY"} and not erow.get("body_part")
    positive = float((rows.get(pid) or {}).get("points") or 0) > 0
    if not (bounds.get("out_for_season") and active and positive):
        return ""
    report_at = _timestamp(bounds.get("timing_reported_at"))
    structured_at = _timestamp(erow.get("as_of"))
    if report_at is not None and structured_at is not None and report_at > structured_at:
        return ""  # newer, explicitly attributable reporting may supersede the feed
    return ("quarantined: out-for-season prose conflicts with ACTIVE/no-injury "
            "structured status and a positive current-week projection")


def update_timing(events: list[dict], affected: set[str], rows: dict, week: int,
                  espn: dict | None = None) -> dict:
    from robo import scout
    deterministic = set()
    deterministic_bounds = {}
    quarantined = {}
    cleared = set()
    advisory = set()
    espn = espn or {}
    by_primary = {e["player_id"]: e for e in events}
    for pid, event in by_primary.items():
        name = (rows.get(pid) or {}).get("name") or pid
        news = [dict(n, subject_player_id=pid) for n in
                (injuries.prose(pid) + scout.player_news(pid) + event.get("pft", []))]
        bounds = scout.timing_bounds(news, week, injuries.floor_week(pid),
                                     subject_name=name, subject_id=pid)
        if bounds:
            conflict = _active_contradiction(pid, bounds, rows, espn)
            if conflict:
                scout.clear_timing(pid, name, conflict)
                quarantined[pid] = conflict
            else:
                scout.merge_timing(pid, name, bounds, news)
                deterministic.add(pid)
                deterministic_bounds[pid] = bounds
        else:
            old = scout.role_signal(pid)
            erow = espn.get(pid) or {}
            status = str(erow.get("designation") or erow.get("espn_status") or "").upper()
            active_at = _timestamp(erow.get("as_of"))
            timing_at = _timestamp(old.get("timing_reported_at")) or _timestamp(old.get("judged_at"))
            if status in {"ACTIVE", "HEALTHY"} and old.get("return_week_min") is not None \
                    and active_at is not None and (timing_at is None or active_at > timing_at):
                scout.clear_timing(pid, name, "newer structured ACTIVE status cleared older timing")
                cleared.add(pid)

    ambiguous = set(by_primary) - deterministic - set(quarantined) - cleared
    if not ambiguous:
        return {"summary": f"{len(deterministic)} explicit timing signal(s)",
                "deterministic": sorted(deterministic), "quarantined": quarantined,
                "bounds": deterministic_bounds,
                "cleared": sorted(cleared), "advisory": [],
                "model_reviewed": [], "advisory_reviews": []}
    bundles = scout.gather(only=ambiguous)
    for b in bundles:
        b["news"] += by_primary.get(b["player_id"], {}).get("pft", [])
    if not bundles:
        return {"summary": f"{len(deterministic)} explicit; no ambiguous player in scout pool",
                "deterministic": sorted(deterministic), "quarantined": quarantined,
                "bounds": deterministic_bounds,
                "cleared": sorted(cleared), "advisory": [],
                "model_reviewed": [], "advisory_reviews": []}
    verdicts = scout.judge(bundles, verbose=False, timeout=120)
    for v in verdicts:
        # The local model may interpret prose for a human reviewer, but an
        # inferred date is never transaction authority.
        if v.get("return_week") is not None:
            v["advisory_return_week"] = v.get("return_week")
            advisory.add(str(v.get("player_id")))
        v["return_week"] = None
        v["return_week_min"] = None
        v["return_week_max"] = None
        v["timing_actionable"] = False
    scout.write_verdicts(verdicts, scout.LOCAL_MODEL, bundles=bundles)
    return {"summary": (f"{len(deterministic)} explicit, "
                        f"{len(verdicts)}/{len(bundles)} model-reviewed (advisory only)"),
            "deterministic": sorted(deterministic), "quarantined": quarantined,
            "bounds": deterministic_bounds,
            "cleared": sorted(cleared), "advisory": sorted(advisory),
            "model_reviewed": [str(v.get("player_id")) for v in verdicts],
            "advisory_reviews": [{k: v.get(k) for k in
                                  ("player_id", "name", "verdict", "confidence", "reason",
                                   "advisory_return_week", "return_basis", "role_week")}
                                 for v in verdicts]}


def _series_complete(table: dict, pid: str) -> bool:
    row = (table.get("players") or {}).get(pid) or {}
    required = len([w for w, weight in (table.get("weights") or {}).items()
                    if float(weight or 0) > 0])
    return bool(row) and len(row.get("by_week") or {}) >= max(1, required - 1)


def event_deltas(pre: dict, post: dict, affected: set[str],
                 events: list[dict]) -> dict:
    """Measured pre/post value changes and the causal edge that admits them."""
    primary = {str(e["player_id"]) for e in events}
    compatible = pre.get("schema") == post.get("schema")
    before, after = pre.get("players") or {}, post.get("players") or {}
    out = {}
    for pid in sorted(affected):
        a, b = before.get(pid) or {}, after.get(pid) or {}
        weeks = sorted(set(a.get("by_week") or {}) | set(b.get("by_week") or {}),
                       key=lambda x: int(x))
        by_week = {str(w): round(float((b.get("by_week") or {}).get(str(w), {}).get("final") or 0)
                                - float((a.get("by_week") or {}).get(str(w), {}).get("final") or 0), 3)
                   for w in weeks}
        lead = b.get("lead_id")
        edge = (f"self:{pid}" if pid in primary else
                (f"successor-of:{lead}" if lead and str(lead) in primary else None))
        pre_ros, post_ros = float(a.get("ros") or 0), float(b.get("ros") or 0)
        identity = b or a
        out[pid] = {"name": identity.get("name") or pid,
                    "pos": identity.get("pos"), "team": identity.get("team"),
                    "primary_event": pid in primary,
                    "pre_ros": pre_ros, "post_ros": post_ros,
                    "delta_ros": round(post_ros - pre_ros, 3),
                    "by_week": by_week, "lead_id": lead,
                    "causal_edge": edge,
                    "complete": (compatible and _series_complete(pre, pid)
                                 and _series_complete(post, pid))}
    return out


def rebuild_and_move(affected: set[str], apply: bool,
                     pre_expected: dict | None = None,
                     events: list[dict] | None = None,
                     fingerprint: str | None = None) -> dict:
    from robo import cascade, expected, marginal, moves, refresh
    started = time.monotonic()
    week = season.current_week()
    capture_ok, capture = cascade.capture_week(week)
    export_ok, export = cascade.export_week(week)
    model = refresh.pull_model() if export_ok else "kept prior model"
    ex = expected.build(league_id=LEAGUE_ID_2026)
    _write(expected.CACHE, ex)
    pre_expected = pre_expected or {"players": {}, "weights": ex.get("weights") or {}}
    deltas = event_deltas(pre_expected, ex, affected, events or [])
    # expected.json is the roster engine. ros.json is the slower legacy/public
    # table and the daily refresh still maintains it; rebuilding all 900 rows of
    # its separate upside report would spend most of the three-minute reaction
    # budget without changing a transaction price.
    marginal.board.cache_clear()
    # One context means one paired simulation board for both channels. The
    # first call prices free agents and waivers together; the second reuses it.
    ctx = moves._context(LEAGUE_ID_2026, "news", affected=affected,
                         event_deltas=deltas, event_fingerprint=fingerprint)
    available = {str(r.get("player_id")) for r in ctx.get("available") or []}
    ours = {str(pid) for pid in (ctx.get("roster") or {}).get("players") or []}
    on_waivers = {str(pid) for pid in ctx.get("on_waivers") or set()}
    for pid, delta in deltas.items():
        delta["ownership"] = ("ours" if pid in ours else
                              "waivers" if pid in available and pid in on_waivers else
                              "free agent" if pid in available else "other roster")
        delta["acquisition_candidate"] = pid in available
    free = moves.run("free", apply=apply, mode="news", affected=affected,
                     league_id=LEAGUE_ID_2026, verbose=True, _ctx=ctx)
    claims = moves.run("claims", apply=apply, mode="news", affected=affected,
                       league_id=LEAGUE_ID_2026, verbose=True, _ctx=ctx)
    return {"capture": capture, "capture_ok": capture_ok,
            "export": export, "export_ok": export_ok, "model": model,
            "expected_players": len(ex.get("players") or {}),
            "event_deltas": deltas,
            "candidate_checks": ctx.get("_news_candidates") or [],
            "rejections": ctx.get("_news_rejections") or [],
            "drop_checks": ctx.get("_news_drop_checks") or [],
            "roster_state": {"active": ctx.get("slots", {}).get("active"),
                             "maximum": ctx.get("slots", {}).get("roster_max"),
                             "open": ctx.get("slots", {}).get("open"),
                             "faab": ctx.get("faab"),
                             "hours_to_kickoff": ctx.get("hours_to_kickoff")},
            "free_plans": len(free.get("plans") or []),
            "free_proposals": free.get("plans") or [],
            "free_gated": free.get("gated"),
            "free_submitted": free.get("submitted") or [],
            "claim_plans": len(claims.get("plans") or []),
            "claim_proposals": claims.get("plans") or [],
            "claims_gated": claims.get("gated"),
            "claims_submitted": claims.get("submitted") or [],
            "duration_seconds": round(time.monotonic() - started, 3)}


def poll(apply: bool = True) -> dict:
    from robo import expected
    prior = _read(STATE, {})
    week = season.current_week()
    errors = []
    try:
        weekly, etag, changed = weekly_snapshot(prior, week)
    except Exception as e:
        weekly, etag, changed = prior.get("weekly") or {}, prior.get("weekly_etag") or "", False
        errors.append(f"Sleeper: {str(e)[:120]}")
    try:
        injury_doc, why = injuries.fetch(timeout=20)
        if why:
            errors.append(why)
            espn = prior.get("espn") or {}
        else:
            espn = injury_doc.get("players") or {}
    except Exception as e:
        espn = prior.get("espn") or {}
        errors.append(f"ESPN: {str(e)[:120]}")
    try:
        pft = pft_items()
        pft_seen = [x.get("link") for x in pft if x.get("link")]
    except Exception as e:
        pft = []
        pft_seen = prior.get("pft_seen") or []
        errors.append(f"PFT: {str(e)[:120]}")
    try:
        top = trending()
    except Exception as e:
        top = prior.get("trending_top") or []
        errors.append(f"trending: {str(e)[:120]}")

    events = detect(prior, weekly, espn, pft, top)
    affected = affected_room(events, weekly)
    fp = _fingerprint(events, affected) if events else ""
    handled = list(prior.get("handled") or [])[-199:]
    action = None
    timing = {}
    audit_path = None
    if events and fp not in handled:
        # This is the actual pre-event decision table, retained before timing
        # or provider refresh can alter it. Deltas against a reconstructed
        # baseline are not evidence that this event caused anything.
        pre_expected = _read(expected.CACHE, {})
        timing = update_timing(events, affected, weekly, week, espn=espn)
        action = rebuild_and_move(affected, apply=apply and not errors,
                                  pre_expected=pre_expected, events=events,
                                  fingerprint=fp)
        handled.append(fp)
        old_weekly = prior.get("weekly") or {}
        provider_at_trigger = {}
        for pid in sorted(affected):
            before, now_row = old_weekly.get(pid) or {}, weekly.get(pid) or {}
            provider_at_trigger[pid] = {
                "name": now_row.get("name") or before.get("name") or pid,
                "pos": now_row.get("pos") or before.get("pos"),
                "team": now_row.get("team") or before.get("team"),
                "points_before": before.get("points"),
                "points_at_trigger": now_row.get("points"),
                "points_delta": round(float(now_row.get("points") or 0)
                                      - float(before.get("points") or 0), 3),
                "status_before": before.get("status"),
                "status_at_trigger": now_row.get("status"),
                "news_updated_before": before.get("news_updated"),
                "news_updated_at_trigger": now_row.get("news_updated"),
            }
        audit = {"schema": 2, "at": time.time(), "week": week,
                 "fingerprint": fp, "events": events,
                 "affected": sorted(affected), "source_errors": errors,
                 "provider_at_trigger": provider_at_trigger,
                 "timing": timing, "action": action,
                 "submission_authorized": bool(apply and not errors),
                 "dry_run": not bool(apply and not errors)}
        audit_json, audit_report = _write_audit(fp, audit)
        audit_path = {"json": str(audit_json), "report": str(audit_report)}

    now = time.time()
    state = {"schema": 2, "last_poll": now, "week": week,
             "weekly_etag": etag, "weekly": weekly,
             "weekly_changed": changed,
             "espn": espn,
             "pft_seen": pft_seen,
             "trending_top": top, "handled": handled,
             "source_errors": errors,
             "last_event": ({"at": now, "fingerprint": fp, "events": events,
                             "affected": sorted(affected), "timing": timing,
                             "action": action, "audit_path": audit_path}
                            if action else prior.get("last_event"))}
    _write(STATE, state)
    _log(f"{len(events)} event(s), {len(affected)} affected, "
         f"{'acted' if action else 'quiet'}" + (f"; errors: {errors}" if errors else ""))
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    with SingleInstance():
        poll(apply=not a.dry_run)


if __name__ == "__main__":
    main()
