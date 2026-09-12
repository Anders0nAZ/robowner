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
from pathlib import Path

import requests

from robo import DATA, LEAGUE_ID_2026, ROOT, injuries, rankings, season

STATE = DATA / "news_watch.json"
LOCK = DATA / "news_watch.lock"
LOG = ROOT / "news-watch.log"
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

    def add(pid: str, reason: str, item: dict | None = None):
        e = events.setdefault(pid, {"player_id": pid, "reasons": [], "pft": []})
        e["reasons"].append(reason)
        if item:
            e["pft"].append(item)

    for pid, now in current.items():
        was = old.get(pid)
        if not was:
            continue
        if now.get("status") != was.get("status"):
            add(pid, f"status {was.get('status')} -> {now.get('status')}")
        if now.get("news_updated") != was.get("news_updated"):
            add(pid, "Sleeper news timestamp changed")
        a, b = float(was.get("points") or 0), float(now.get("points") or 0)
        if (bool(a) != bool(b)) or abs(b - a) >= POINT_MOVE:
            add(pid, f"weekly projection {a:.2f} -> {b:.2f}")

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
                add(pid, f"ESPN injury entry: {now.get('designation') or now.get('espn_status')}")
                continue
            for key, label in (("designation", "designation"),
                               ("return_date", "return date"),
                               ("as_of", "injury timestamp")):
                if now.get(key) != was.get(key):
                    add(pid, f"ESPN {label} {was.get(key)} -> {now.get(key)}")

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
                add(pid, f"PFT: {item.get('title')}", full)

    for pid in top if "trending_top" in prior else []:
        if pid not in old_top and pid in current:
            add(pid, "entered Sleeper top-five trending adds")
    return list(events.values())


def affected_room(events: list[dict], rows: dict) -> set[str]:
    keys = {(rows.get(e["player_id"]) or {}).get("team") for e in events}
    pairs = {((rows.get(e["player_id"]) or {}).get("team"),
              (rows.get(e["player_id"]) or {}).get("pos")) for e in events}
    return {pid for pid, r in rows.items() if (r.get("team"), r.get("pos")) in pairs
            and r.get("team") in keys}


def _fingerprint(events: list[dict], affected: set[str]) -> str:
    payload = {"events": events, "affected": sorted(affected)}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def update_timing(events: list[dict], affected: set[str], rows: dict, week: int) -> str:
    from robo import scout
    deterministic = set()
    by_primary = {e["player_id"]: e for e in events}
    for pid, event in by_primary.items():
        news = injuries.prose(pid) + scout.player_news(pid) + event.get("pft", [])
        bounds = scout.timing_bounds(news, week, injuries.floor_week(pid))
        if bounds:
            scout.merge_timing(pid, (rows.get(pid) or {}).get("name") or pid, bounds, news)
            deterministic.add(pid)

    ambiguous = set(by_primary) - deterministic
    if not ambiguous:
        return f"{len(deterministic)} explicit timing signal(s)"
    bundles = scout.gather(only=ambiguous)
    for b in bundles:
        b["news"] += by_primary.get(b["player_id"], {}).get("pft", [])
    if not bundles:
        return f"{len(deterministic)} explicit; no ambiguous player in scout pool"
    verdicts = scout.judge(bundles, verbose=False, timeout=120)
    for v in verdicts:
        if v.get("return_week") is not None:
            v["return_week_min"] = v["return_week"]
            v["return_week_max"] = v["return_week"]
    scout.write_verdicts(verdicts, scout.LOCAL_MODEL, bundles=bundles)
    return f"{len(deterministic)} explicit, {len(verdicts)}/{len(bundles)} model-parsed"


def rebuild_and_move(affected: set[str], apply: bool) -> dict:
    from robo import cascade, expected, marginal, moves, refresh
    week = season.current_week()
    capture_ok, capture = cascade.capture_week(week)
    export_ok, export = cascade.export_week(week)
    model = refresh.pull_model() if export_ok else "kept prior model"
    ex = expected.build(league_id=LEAGUE_ID_2026)
    _write(expected.CACHE, ex)
    # expected.json is the roster engine. ros.json is the slower legacy/public
    # table and the daily refresh still maintains it; rebuilding all 900 rows of
    # its separate upside report would spend most of the three-minute reaction
    # budget without changing a transaction price.
    marginal.board.cache_clear()
    # One context means one paired simulation board for both channels. The
    # first call prices free agents and waivers together; the second reuses it.
    ctx = moves._context(LEAGUE_ID_2026, "news", affected=affected)
    free = moves.run("free", apply=apply, mode="news", affected=affected,
                     league_id=LEAGUE_ID_2026, verbose=True, _ctx=ctx)
    claims = moves.run("claims", apply=apply, mode="news", affected=affected,
                       league_id=LEAGUE_ID_2026, verbose=True, _ctx=ctx)
    return {"capture": capture, "capture_ok": capture_ok,
            "export": export, "export_ok": export_ok, "model": model,
            "expected_players": len(ex.get("players") or {}),
            "free_plans": len(free.get("plans") or []),
            "free_submitted": free.get("submitted") or [],
            "claim_plans": len(claims.get("plans") or []),
            "claims_submitted": claims.get("submitted") or []}


def poll(apply: bool = True) -> dict:
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
    timing = ""
    if events and fp not in handled:
        timing = update_timing(events, affected, weekly, week)
        action = rebuild_and_move(affected, apply=apply)
        handled.append(fp)

    now = time.time()
    state = {"schema": 1, "last_poll": now, "week": week,
             "weekly_etag": etag, "weekly": weekly,
             "weekly_changed": changed,
             "espn": espn,
             "pft_seen": pft_seen,
             "trending_top": top, "handled": handled,
             "source_errors": errors,
             "last_event": ({"at": now, "fingerprint": fp, "events": events,
                             "affected": sorted(affected), "timing": timing,
                             "action": action} if action else prior.get("last_event"))}
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
