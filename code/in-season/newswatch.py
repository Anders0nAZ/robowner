"""Ten-minute injury and opportunity watcher.

This is deliberately a small poller, not the daily refresh. It reads the
current weekly projection (with ETag), ESPN injuries, Sleeper trending and PFT.
Provider timestamps wake content verification but never valuation by
themselves; only a changed fact pays for model or transaction work.

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
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from robo import (DATA, LEAGUE_ID_2026, ROOT, injuries, rankings, scout_queue,
                  season, vegas)

STATE = DATA / "news_watch.json"
LOCK = DATA / "news_watch.lock"
LOG = ROOT / "news-watch.log"
AUDIT_DIR = DATA / "news_events"
PFT_RSS = "https://www.nbcsports.com/profootballtalk.rss"
POINT_MOVE = 2.0
TOP_TRENDING = 5
SKILL = {"QB", "RB", "WR", "TE"}
INACTIVE_BURST = 5
INACTIVE_DEBOUNCE_S = 60
NEWS_BATCH = 40
CASCADE_GUARD_MIN = 5
FULL_CASCADE_CLOCKS = ((7, 0), (9, 0), (16, 0))
KICKOFF_STATUS_GRACE_MIN = 20


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
        q = p.get("bid_quote") or {}
        if q:
            band = q.get("near_optimal") or [p.get("bid", 0), p.get("bid", 0)]
            lines.append(f"       BID ${p.get('bid', 0)} from paired roster value "
                         f"{float(p.get('bid_gain') or 0):+.2f} +/- "
                         f"{float(p.get('bid_se') or 0):.2f}; P(win) "
                         f"{float(q.get('p_win') or 0):.0%}; expected-high "
                         f"{q.get('expected_highest')}; near-optimal ${band[0]}-${band[1]}")
            field = p.get("opponent_field") or {}
            valid = field.get("validation") or {}
            if valid:
                lines.append(f"       HOLDOUT demand Brier {valid.get('demand_brier')} vs "
                             f"{valid.get('pooled_demand_brier')} pooled; bid CRPS "
                             f"{valid.get('bid_crps')} vs {valid.get('pooled_bid_crps')} "
                             f"pooled; {'PASS' if valid.get('passes') else 'FALLBACK'}")
            for o in sorted(field.get("opponents") or [],
                            key=lambda x: -float(x.get("p_claim") or 0)):
                need = o.get("need") or {}
                evidence = o.get("evidence") or {}
                lines.append(f"       RIVAL {o.get('manager')}: P(claim) "
                             f"{float(o.get('p_claim') or 0):.0%}; need "
                             f"{need.get('need')} ({float(need.get('gain') or 0):+.2f}); "
                             f"bid p50/p75/p90 {o.get('bid_p50')}/"
                             f"{o.get('bid_p75')}/{o.get('bid_p90')}; "
                             f"FAAB {o.get('faab_left')}; priority "
                             f"{o.get('waiver_position')}; manager samples "
                             f"{evidence.get('manager_samples')}")
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
    def __init__(self):
        self.token = uuid.uuid4().hex
        self.held = False

    @staticmethod
    def _owner() -> tuple[int | None, str | None]:
        try:
            raw = LOCK.read_text(encoding="utf-8")
            try:
                doc = json.loads(raw)
                return int(doc.get("pid") or 0) or None, doc.get("token")
            except (json.JSONDecodeError, AttributeError):
                # Read the original one-line PID format during rollout.
                return int(raw.strip()) or None, None
        except (OSError, TypeError, ValueError):
            return None, None

    def __enter__(self):
        from robo.runlock import pid_alive
        try:
            if LOCK.exists():
                pid, _ = self._owner()
                stale_unknown = (pid is None and
                                 time.time() - LOCK.stat().st_mtime > 15 * 60)
                if (pid is not None and not pid_alive(pid)) or stale_unknown:
                    LOCK.unlink()
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = json.dumps({"pid": os.getpid(), "token": self.token,
                                  "at": time.time()}).encode("utf-8")
            os.write(fd, payload)
            os.close(fd)
            self.held = True
        except FileExistsError:
            raise RuntimeError("another news watcher is still running")
        return self

    def __exit__(self, *_):
        if not self.held:
            return
        try:
            _, token = self._owner()
            if token == self.token:
                LOCK.unlink()
        except (OSError, TypeError, ValueError):
            pass
        self.held = False


def game_pause_reason(week: int | None = None, now: float | None = None) -> str:
    """Why the watcher must stand down during live football, or ``""``.

    Sleeper's game status is the same source used to lock lineup players. A
    clock-derived approximation would either keep firing after an early final
    or resume during overtime. If the schedule cannot be read, fail closed:
    the next scheduled run can try again without mutating valuation state.
    """
    week = week if week is not None else season.current_week()
    try:
        games = [g for g in season.schedule()
                 if int(g.get("week") or 0) == int(week)]
    except Exception as e:
        return f"game status unavailable ({str(e)[:100]})"
    live = [g for g in games if g.get("status") == "in_game"]
    if live:
        teams = [f"{g.get('away') or '?'}@{g.get('home') or '?'}" for g in live]
        shown = ", ".join(teams[:3]) + ("..." if len(teams) > 3 else "")
        return f"{len(live)} game(s) in progress ({shown})"
    # Sleeper's status can trail the published kickoff by a poll. The 10:03
    # Week-1 run proved that `pre_game` alone is not an adequate opening gate.
    # Cover that short propagation gap with the independent nflverse clock;
    # after twenty minutes the live status remains the authoritative end gate.
    now = time.time() if now is None else float(now)
    try:
        just_started = [k for k in vegas.kickoffs(season.SEASON, week)
                        if 0 <= now - k <= KICKOFF_STATUS_GRACE_MIN * 60]
    except Exception:
        just_started = []
    if just_started:
        local = datetime.fromtimestamp(max(just_started)).astimezone()
        return f"kickoff window opened at {local:%H:%M}; awaiting live game status"
    return ""


def scheduled_cascade_pause_reason(now: float | None = None,
                                   week: int | None = None) -> str:
    """Reserve the minutes immediately before scheduled cascade starts."""
    from robo import prekick
    now = time.time() if now is None else float(now)
    here = datetime.fromtimestamp(now).astimezone()
    for hour, minute in FULL_CASCADE_CLOCKS:
        fire = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
        seconds = fire.timestamp() - now
        if 0 <= seconds <= CASCADE_GUARD_MIN * 60:
            return f"full cascade scheduled at {fire:%H:%M}"
    week = week if week is not None else season.current_week()
    try:
        for slot in prekick.slots(season.SEASON, week):
            fire = slot - prekick.LEAD_MIN * 60
            seconds = fire - now
            if 0 <= seconds <= CASCADE_GUARD_MIN * 60:
                local = datetime.fromtimestamp(fire).astimezone()
                return f"pregame cascade scheduled at {local:%H:%M}"
    except Exception:
        # The shared writer lock still prevents actual overlap. Failure to read
        # future reservations is not grounds to disable the watcher all day.
        return ""
    return ""


def record_pause(reason: str) -> dict:
    """Record an intentional no-poll without disturbing the last good state."""
    state = _read(STATE, {})
    now = time.time()
    state["last_attempt"] = now
    state["paused"] = {"at": now, "reason": reason}
    _write(STATE, state)
    _log(f"paused: {reason}")
    return state


def record_failure(error: Exception) -> dict:
    """Expose a failed attempt without pretending its source snapshot landed."""
    state = _read(STATE, {})
    now = time.time()
    detail = f"pulse: {type(error).__name__}: {str(error)[:180]}"
    state["last_attempt"] = now
    state["paused"] = None
    state["source_errors"] = [detail]
    state["last_failure"] = {"at": now, "reason": detail}
    _write(STATE, state)
    _log(f"FAILED: {detail}")
    return state


def completed_teams(week: int) -> set[str]:
    """NFL teams whose current-week game can no longer change a lineup."""
    out = set()
    try:
        games = season.schedule()
    except Exception:
        # main() already fails closed when the schedule is unavailable. Keep
        # direct/library callers conservative rather than turning a filter
        # lookup into a second failure point.
        return out
    for game in games:
        if int(game.get("week") or 0) != int(week) or game.get("status") != "complete":
            continue
        out.update(str(game.get(k)) for k in ("home", "away") if game.get(k))
    return out


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
            "depth_order": p.get("depth_chart_order"),
            "news_updated": p.get("news_updated"),
            "points": round(pts, 3), "game_id": row.get("game_id"),
        }
    return out, str(r.headers.get("ETag") or ""), True


def trending() -> list[str]:
    from robo import sleeper_read as api
    return [str(r["player_id"]) for r in api.trending("add", 24, TOP_TRENDING)]


def _content_fingerprint(items: list[dict]) -> str:
    """Hash readable facts, deliberately excluding provider timestamps."""
    facts = []
    for item in items:
        fact = {k: re.sub(r"\s+", " ", str(item.get(k) or "")).strip()
                for k in ("source", "title", "description", "analysis")}
        if any(fact.values()):
            facts.append(fact)
    body = json.dumps(sorted(facts, key=lambda x: json.dumps(x, sort_keys=True)),
                      sort_keys=True)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def sleeper_news_fingerprints(player_ids) -> dict[str, str]:
    """Fetch many players' story bodies in a handful of GraphQL requests.

    This is the verification stage behind ``news_updated``. A source timestamp
    may wake it, but only a changed text hash is allowed to wake the model.
    Unlike scout.player_news(), failures raise so the caller can preserve the
    last fingerprints rather than mistaking an outage for deleted reporting.
    """
    from robo.sleeper_write import gql
    ids = sorted({str(pid) for pid in player_ids if str(pid)})
    out = {}
    for start in range(0, len(ids), NEWS_BATCH):
        chunk = ids[start:start + NEWS_BATCH]
        fields = []
        for i, pid in enumerate(chunk):
            safe = json.dumps(pid)
            fields.append(
                f'n{i}: get_player_news(sport: "nfl", player_id: {safe}, limit: 5) '
                '{ source metadata }')
        data = gql("NewsWatch", "query NewsWatch { " + " ".join(fields) + " }")
        for i, pid in enumerate(chunk):
            items = []
            for row in data.get(f"n{i}") or []:
                meta = row.get("metadata") or {}
                items.append({"source": row.get("source"),
                              "title": meta.get("title"),
                              "description": meta.get("description"),
                              "analysis": meta.get("analysis")})
            out[pid] = _content_fingerprint(items)
    return out


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


def _espn_availability(row: dict) -> str:
    token = str(row.get("designation") or row.get("espn_status") or "").upper()
    if token in {"ACTIVE", "HEALTHY"}:
        return "active"
    if token == "INACTIVE" or token in set(injuries.ABSENT):
        return "out"
    return token.lower()


def _report_text(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return "" if text and not any(c.isspace() for c in text) else text


def _body_fact(value) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"", "undisclosed", "unknown"} else text


def detect(prior: dict, current: dict, espn: dict,
           pft: list[dict], top: list[str],
           news_fingerprints: dict | None = None,
           pft_fingerprints: set[str] | None = None,
           filter_stats: dict | None = None,
           completed: set[str] | None = None) -> list[dict]:
    old = prior.get("weekly") or {}
    seen_pft = set(prior.get("pft_fingerprints") or [])
    old_top = set(prior.get("trending_top") or [])
    old_news_fp = prior.get("sleeper_news_fingerprints") or {}
    news_fingerprints = news_fingerprints or {}
    pft_fingerprints = pft_fingerprints or set()
    filter_stats = filter_stats if filter_stats is not None else {}
    completed = completed or set()
    events = {}

    def filtered(kind: str):
        filter_stats[kind] = int(filter_stats.get(kind) or 0) + 1

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
        game_complete = (now.get("team") or was.get("team")) in completed
        if now.get("status") != was.get("status"):
            add(pid, f"status {was.get('status')} -> {now.get('status')}",
                field="status", before=was.get("status"), after=now.get("status"))
        if now.get("news_updated") != was.get("news_updated"):
            before_fp, after_fp = old_news_fp.get(pid), news_fingerprints.get(pid)
            # Missing old hashes are a migration baseline, not evidence. A
            # failed verification omits after_fp and likewise cannot trigger.
            if before_fp is not None and after_fp is not None and before_fp != after_fp:
                add(pid, "Sleeper news content changed",
                    field="sleeper_news_content", before=before_fp, after=after_fp)
            elif before_fp is not None and after_fp is not None:
                filtered("sleeper_timestamp_only")
        if "depth_order" in was and now.get("depth_order") != was.get("depth_order"):
            add(pid, f"depth-chart order {was.get('depth_order')} -> {now.get('depth_order')}",
                field="depth_order", before=was.get("depth_order"),
                after=now.get("depth_order"))
        a, b = float(was.get("points") or 0), float(now.get("points") or 0)
        if (bool(a) != bool(b)) or abs(b - a) >= POINT_MOVE:
            # Projection feeds zero and restamp a game's rows after the final.
            # That is scoring cleanup, not new information about future value.
            if game_complete:
                filtered("postgame_projection")
            else:
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
            team = (current.get(pid) or {}).get("team") or (old.get(pid) or {}).get("team")
            # ESPN's INACTIVE rows are game-day participation records. After
            # that team's final they are expired housekeeping, as is removing
            # any old injury row. Neither is evidence of a new future state.
            if not now:
                if was:
                    filtered("espn_record_removed")
                continue
            if team in completed and str(now.get("designation") or "").upper() == "INACTIVE":
                filtered("postgame_inactive")
                continue
            before_changes = len((events.get(pid) or {}).get("changes") or [])
            before_avail, after_avail = _espn_availability(was), _espn_availability(now)
            if before_avail != after_avail:
                add(pid, f"ESPN availability {before_avail or 'none'} -> {after_avail or 'none'}",
                    field="espn_availability", before=before_avail or None,
                    after=after_avail or None)
            for key, label in (("return_date", "return date"),
                               ("body_part", "injury type")):
                before_value = (_body_fact(was.get(key)) if key == "body_part"
                                else was.get(key))
                after_value = (_body_fact(now.get(key)) if key == "body_part"
                               else now.get(key))
                if after_value != before_value:
                    add(pid, f"ESPN {label} changed", field=f"espn_{key}",
                        before=before_value or None, after=after_value or None)
            for key, label in (("short", "short report"), ("long", "analysis")):
                before_text, after_text = _report_text(was.get(key)), _report_text(now.get(key))
                if before_text != after_text:
                    add(pid, f"ESPN {label} changed", field=f"espn_{key}",
                        before=before_text or None, after=after_text or None)
            after_changes = len((events.get(pid) or {}).get("changes") or [])
            if now.get("as_of") != was.get("as_of") and after_changes == before_changes:
                filtered("espn_timestamp_only")

    names = [(pid, (r.get("name") or "").lower()) for pid, r in current.items()
             if len((r.get("name") or "").split()) >= 2]
    for item in pft if "pft_fingerprints" in prior else []:
        item_fp = _content_fingerprint([item])
        if item_fp in seen_pft or item_fp not in pft_fingerprints:
            continue
        hay = f"{item.get('title')} {item.get('description')}".lower()
        if not re.search(r"\b(out|miss|injur|surgery|return|back|start|role|inactive)\w*\b", hay):
            continue
        title = str(item.get("title") or "").lower()
        matched = False
        for pid, name in names:
            # The title identifies the report's subject. Descriptions contain
            # related links and opponent/player lists that produced the false
            # Cooper Rush -> Bijan Robinson association seen on Sep 13.
            if name and name in title:
                matched = True
                full = dict(item)
                full["analysis"] = _article_text(item.get("link") or "")
                add(pid, f"PFT: {item.get('title')}", full,
                    field="pft_report", before=None, after=item.get("link"))
        if not matched and any(name and name in hay for _, name in names):
            filtered("pft_indirect_mention")

    for pid in top if "trending_top" in prior else []:
        # Trending is corroboration, not authority. It may strengthen a real
        # status/projection/news/depth event, but a crowd click cannot create
        # an opportunity change by itself.
        if pid not in old_top and pid in events:
            add(pid, "entered Sleeper top-five trending adds", field="trending_top5",
                before=False, after=True)
        elif pid not in old_top and pid in current:
            filtered("trending_without_corroboration")
    return list(events.values())


def inactive_burst(events: list[dict]) -> bool:
    """Whether a status-release wave deserves one short consolidation wait."""
    inactive = set()
    for event in events:
        for change in event.get("changes") or []:
            if change.get("field") == "status" and str(change.get("after") or "").lower() \
                    in {"out", "doubtful"}:
                inactive.add(str(event.get("player_id")))
            if change.get("field") == "espn_availability" and change.get("after") == "out":
                inactive.add(str(event.get("player_id")))
    return len(inactive) >= INACTIVE_BURST


def event_mix(events: list[dict]) -> str:
    counts = {}
    for event in events:
        for change in event.get("changes") or []:
            field = str(change.get("field") or "unknown")
            counts[field] = counts.get(field, 0) + 1
    return ", ".join(f"{field}={count}" for field, count in
                     sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def affected_room(events: list[dict], rows: dict) -> set[str]:
    # A free agent can legitimately have team=None. None is not a team room:
    # expanding (None, WR) once pulled every unsigned receiver in the database
    # into a single 1,059-player event. The primary still remains visible in
    # the event record; without an NFL team there is simply no room to expand.
    pairs = {((rows.get(e["player_id"]) or {}).get("team"),
              (rows.get(e["player_id"]) or {}).get("pos")) for e in events}
    pairs = {(team, pos) for team, pos in pairs if team and pos in SKILL}
    return {pid for pid, r in rows.items() if (r.get("team"), r.get("pos")) in pairs}


def monday_categories(events: list[dict], rows: dict,
                      league_id: str = LEAGUE_ID_2026) -> dict[str, list[str]]:
    """Partition Monday news by what can still affect a real decision."""
    ids = {str(e.get("player_id")) for e in events if e.get("player_id")}
    out = {"remaining_starters": [], "our_unlocked_roster": [],
           "free_now": [], "waiver_candidates": [], "background": []}
    if not ids:
        return out
    roster = season.mine(league_id)
    ours = {str(pid) for pid in (roster.get("players") or [])}
    starters = {str(pid) for pid in (roster.get("starters") or [])}
    states = season.transaction_states(ids, league_id)
    live = season.week_points(season.current_week(), season.SEASON, league_id)
    for pid in sorted(ids):
        state = states[pid]
        game = live.get(pid) or {}
        if pid in starters and game.get("has_game") and not game.get("locked"):
            out["remaining_starters"].append(pid)
        elif pid in ours and state["roster_movement"] == "movable":
            out["our_unlocked_roster"].append(pid)
        elif state["acquisition"] == "free_now":
            out["free_now"].append(pid)
        elif state["acquisition"] in {"weekly_waiver", "drop_waiver"}:
            out["waiver_candidates"].append(pid)
        else:
            out["background"].append(pid)
    return out


def _queue_reviews(events: list[dict], rows: dict,
                   categories: dict[str, list[str]] | None = None) -> dict:
    """Defer this poll's ambiguous prose to the shared scout queue.

    The queue's priority ladder is the Monday one: a starter who can still play
    this week, then anyone whose roster state we could actually change, then
    somebody we might acquire, then news that only matters to a future decision.
    Off a Monday there is no guard partition to read, so everything the pulse
    defers is background -- the actionable half of an event was already handled
    synchronously by the cascade above, and this is the reading that follows it.
    """
    if not events:
        return {"queued": scout_queue.pending_count()}
    by_id = {str(e.get("player_id")): e for e in events if e.get("player_id")}
    rank = {"remaining_starters": "monday_starter",
            "our_unlocked_roster": "emergency",
            "free_now": "waiver_candidate",
            "waiver_candidates": "waiver_candidate"}
    cats = {}
    for key, queue_category in rank.items():
        for pid in (categories or {}).get(key) or []:
            if str(pid) in by_id:
                cats[str(pid)] = queue_category
    return scout_queue.enqueue_ids(
        list(by_id), categories=cats,
        names={pid: (rows.get(pid) or {}).get("name") or pid for pid in by_id},
        extra_news={pid: e.get("pft") or [] for pid, e in by_id.items()})


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
    espn = espn or {}
    by_primary = {e["player_id"]: e for e in events}
    for pid, event in by_primary.items():
        name = (rows.get(pid) or {}).get("name") or pid
        fields = {str(c.get("field") or "") for c in event.get("changes") or []}
        # ESPN prose is already in the local injury snapshot. Do not turn a
        # 200-player Monday feed refresh into 200 serial Sleeper GraphQL reads.
        # Sleeper story bodies are needed immediately only when Sleeper itself
        # supplied the material signal; otherwise the bounded advisory queue
        # gathers them later.
        sleeper_news = (scout.player_news(pid)
                        if fields & {"status", "sleeper_news_content"} else [])
        news = [dict(n, subject_player_id=pid) for n in
                (injuries.prose(pid) + sleeper_news + event.get("pft", []))]
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
                "model_reviewed": [], "advisory_reviews": [],
                "queued": 0, "_pending_events": []}
    # A Monday feed can move hundreds of rows together. Deterministic timing is
    # applied above in the same run; model-read timing is advisory only and must
    # not delay the valuation/action cascade, so the rest is handed to
    # robo.scout_queue and drained one bounded batch per later poll.
    #
    # NOTHING IS GATHERED HERE. Building a corpus costs a Sleeper read per
    # player, which is the whole reason this work is deferred -- paying two
    # hundred of them to decide which four to read would defeat the deferral.
    ordered = sorted(ambiguous, key=lambda pid: (
        -float((rows.get(pid) or {}).get("points") or 0),
        (rows.get(pid) or {}).get("name") or pid,
        pid))
    return {"summary": f"{len(deterministic)} explicit; {len(ordered)} advisory queued",
            "deterministic": sorted(deterministic), "quarantined": quarantined,
            "bounds": deterministic_bounds, "cleared": sorted(cleared),
            "advisory": [], "model_reviewed": [],
            "advisory_reviews": [], "queued": len(ordered),
            "_pending_events": [by_primary[pid] for pid in ordered]}


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
    expected.save(ex)
    pre_expected = pre_expected or {"players": {}, "weights": ex.get("weights") or {}}
    deltas = event_deltas(pre_expected, ex, affected, events or [])
    # expected.json is the roster engine. ros.json is the slower legacy/public
    # table and the daily refresh still maintains it; rebuilding all 900 rows of
    # its separate upside report would spend most of the three-minute reaction
    # budget without changing a transaction price.
    marginal.board.cache_clear()
    if season.monday_guard_active():
        guard = cascade.monday_roster_guard(apply=apply,
                                            league_id=LEAGUE_ID_2026,
                                            week=week)
        patch = guard.get("patch") or {}
        fills = guard.get("ir_fills") or {}
        return {"capture": capture, "capture_ok": capture_ok,
                "export": export, "export_ok": export_ok, "model": model,
                "expected_players": len(ex.get("players") or {}),
                "event_deltas": deltas, "monday_guard": guard,
                "candidate_checks": [], "rejections": [], "drop_checks": [],
                "roster_state": season.slots(LEAGUE_ID_2026),
                "free_plans": len(patch.get("plans") or []) + len(fills.get("plans") or []),
                "free_proposals": list(patch.get("plans") or []) + list(fills.get("plans") or []),
                "free_gated": bool(patch.get("gated") or fills.get("gated")),
                "free_submitted": list(patch.get("submitted") or []) +
                                  list(fills.get("submitted") or []),
                "claim_plans": 0, "claim_proposals": [],
                "claims_gated": True, "claims_submitted": [],
                "monday_suppressed": ["ordinary_ros", "streaming",
                                      "speculative_adds", "waiver_submissions"],
                "duration_seconds": round(time.monotonic() - started, 3)}
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
    # A news event may revise waivers, but it may never APPEND one isolated
    # event claim to an older Tuesday slate. Rebuild the complete canonical ROS
    # portfolio from the live roster after the free-agent channel had its turn;
    # waiver_manager then replaces only bot-owned pending transactions.
    claims_ctx = moves._context(LEAGUE_ID_2026, mode="ros")
    claims = moves.run("claims", apply=apply, mode="ros",
                       league_id=LEAGUE_ID_2026, verbose=True, _ctx=claims_ctx,
                       source="newswatch")
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


def poll(apply: bool = True, _debounced: bool = False) -> dict:
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
        pft_ok = True
    except Exception as e:
        pft = []
        pft_seen = prior.get("pft_seen") or []
        pft_ok = False
        errors.append(f"PFT: {str(e)[:120]}")
    try:
        top = trending()
    except Exception as e:
        top = prior.get("trending_top") or []
        errors.append(f"trending: {str(e)[:120]}")

    old_news_fp = prior.get("sleeper_news_fingerprints") or {}
    changed_news = [pid for pid, row in weekly.items()
                    if (prior.get("weekly") or {}).get(pid)
                    and row.get("news_updated") !=
                    ((prior.get("weekly") or {}).get(pid) or {}).get("news_updated")]
    # Schema migration establishes one complete content baseline. Thereafter
    # only players whose cheap timestamp changed pay for story verification.
    verify_ids = list(weekly) if "sleeper_news_fingerprints" not in prior else changed_news
    news_fp = dict(old_news_fp)
    news_verified = not verify_ids
    if verify_ids:
        try:
            news_fp.update(sleeper_news_fingerprints(verify_ids))
            news_verified = True
        except Exception as e:
            errors.append(f"Sleeper news verification: {str(e)[:120]}")

    pft_fp = ({_content_fingerprint([item]) for item in pft} if pft_ok else
              set(prior.get("pft_fingerprints") or []))
    filter_stats = {}
    events = detect(prior, weekly, espn, pft, top,
                    news_fingerprints=news_fp, pft_fingerprints=pft_fp,
                    filter_stats=filter_stats, completed=completed_teams(week))
    if events and not _debounced and inactive_burst(events):
        _log(f"inactive release burst ({len(events)} events); consolidating for "
             f"{INACTIVE_DEBOUNCE_S}s; {event_mix(events)}")
        time.sleep(INACTIVE_DEBOUNCE_S)
        return poll(apply=apply, _debounced=True)
    affected = affected_room(events, weekly)
    categories = (monday_categories(events, weekly)
                  if season.monday_guard_active() else {})
    fp = _fingerprint(events, affected) if events else ""
    handled = list(prior.get("handled") or [])[-199:]
    # Anything this watcher had queued under its own old scheme moves into the
    # shared queue on the first poll after the change and is then forgotten
    # here; scout_queue is the only pending list.
    deferred = list(prior.get("pending_reviews") or [])
    action = None
    timing = {}
    audit_path = None
    if events and fp not in handled:
        # This is the actual pre-event decision table, retained before timing
        # or provider refresh can alter it. Deltas against a reconstructed
        # baseline are not evidence that this event caused anything.
        pre_expected = _read(expected.CACHE, {})
        timing = update_timing(events, affected, weekly, week, espn=espn)
        deferred += timing.pop("_pending_events", [])
        monday_actionable = bool(categories.get("remaining_starters") or
                                 categories.get("our_unlocked_roster"))
        if season.monday_guard_active() and not monday_actionable:
            action = {"monday_guard": "background-only event; no roster cascade",
                      "categories": categories, "event_deltas": {},
                      "free_proposals": [], "claim_proposals": [],
                      "free_gated": True, "claims_gated": True,
                      "free_submitted": [], "claims_submitted": [],
                      "duration_seconds": 0.0}
        else:
            action = rebuild_and_move(affected, apply=apply and not errors,
                                      pre_expected=pre_expected, events=events,
                                      fingerprint=fp)
            action["categories"] = categories
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
                 "categories": categories,
                 "market_snapshot": {
                     "trending_top": list(top),
                     "pricing_status": "archived only; not calibrated for transaction pricing",
                 },
                 "provider_at_trigger": provider_at_trigger,
                 "timing": timing, "action": action,
                 "submission_authorized": bool(apply and not errors),
                 "dry_run": not bool(apply and not errors)}

    # Hand the deferred prose to the shared queue, then ask it for ONE batch --
    # and only after every actionable consequence of this source snapshot has
    # completed. The queue owns the rate, the retry budget and the record of
    # what still needs a human; this loop owns none of it.
    queued = _queue_reviews(deferred, weekly, categories)
    try:
        drained = scout_queue.drain(verbose=False)
    except Exception as e:
        # Advisory work is never allowed to roll back or replay an action.
        errors.append(f"advisory review: {str(e)[:120]}")
        drained = {"status": "failed", "queued": queued.get("queued", 0)}
    timing["advisory"] = sorted(
        str(v.get("player_id")) for v in (drained.get("verdicts") or [])
        if v.get("advisory_return_week") is not None)
    timing["model_reviewed"] = list(drained.get("judged") or [])
    timing["advisory_reviews"] = [
        {k: v.get(k) for k in ("player_id", "name", "verdict", "confidence",
                               "reason", "advisory_return_week",
                               "return_basis", "role_week")}
        for v in (drained.get("verdicts") or [])]
    pending = int(drained.get("queued") or queued.get("queued") or 0)
    timing["queued"] = pending
    timing["batch"] = drained.get("status")
    timing["attention"] = list(drained.get("attention") or [])
    timing["summary"] = "; ".join(x for x in (
        timing.get("summary"),
        f"queue {drained.get('status')}: {len(drained.get('judged') or [])} judged, "
        f"{pending} pending") if x)
    if action:
        audit["timing"] = timing
        audit["advisory_pending"] = pending
        audit_json, audit_report = _write_audit(fp, audit)
        audit_path = {"json": str(audit_json), "report": str(audit_report)}

    # Pending claims outlive the decision run that created them. A human
    # lineup edit, an IR move, or a quiet roster change can invalidate the drop
    # without producing a new news event, so every pulse performs the cheap
    # fingerprint check. It only invokes the full planner when facts changed.
    try:
        from robo import moves as _moves
        maintenance = _moves.maintain_pending_claims(
            apply=apply and not errors, league_id=LEAGUE_ID_2026,
            reason="newswatch state change")
    except Exception as e:
        maintenance = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        errors.append(f"waiver maintenance: {str(e)[:120]}")

    now = time.time()
    state = {"schema": 4, "last_poll": now, "last_attempt": now,
             "paused": None, "week": week,
             "weekly_etag": etag, "weekly": weekly,
             "weekly_changed": changed,
             "espn": espn,
             "pft_seen": pft_seen,
             "pft_fingerprints": sorted(pft_fp),
             "sleeper_news_fingerprints": news_fp,
             "trending_top": top, "handled": handled,
             "monday_categories": categories,
             "filtered": filter_stats,
             "waiver_maintenance": maintenance,
             "source_errors": errors,
             "last_event": ({"at": now, "fingerprint": fp, "events": events,
                             "affected": sorted(affected), "timing": timing,
                             "action": action, "audit_path": audit_path}
                            if action else prior.get("last_event"))}
    if not news_verified and "sleeper_news_fingerprints" not in prior:
        # Retry the full migration baseline after a transient GraphQL failure.
        state.pop("sleeper_news_fingerprints", None)
    _write(STATE, state)
    ignored = sum(filter_stats.values())
    _log(f"{len(events)} event(s), {len(affected)} affected, "
         f"{'acted' if action else 'quiet'}, {ignored} metadata-only ignored"
         + (f", {pending} advisory queued" if pending else "")
         + (f"; errors: {errors}" if errors else ""))
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    with SingleInstance():
        reason = game_pause_reason() or scheduled_cascade_pause_reason()
        if reason:
            record_pause(reason)
            return
        from robo.runlock import DecisionRun, RunBusy
        try:
            # Never wait behind a scheduled cascade. The pulse is retried on
            # its own interval; the lineup run has a kickoff deadline.
            with DecisionRun("news pulse"):
                try:
                    poll(apply=not a.dry_run)
                except Exception as e:
                    record_failure(e)
                    raise
        except RunBusy as e:
            record_pause(str(e))


if __name__ == "__main__":
    main()
