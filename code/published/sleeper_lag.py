"""How long Sleeper takes to reprice a player after his status changes.

READ-ONLY. It prints and writes nothing.

    python -m robo.sleeper_lag              # every status event this season
    python -m robo.sleeper_lag --player "Jayden Reed"

WHY. The news pulse notices a designation within twenty minutes and re-values
the room -- but it re-values on Sleeper's weekly feed plus ESPN's floor, so the
pulse is only as current as the feed it reads. Jayden Reed went off on a
backboard on 20 Sep 2026: twenty hours later his week-3 projection was still
9.1, the next day it read 0.5, and weeks 6+ still assumed a full-strength
return while the reporting said he may not play again this season. One case is
an anecdote. This measures every status event the pulse recorded.

THREE QUESTIONS, three sources:

  * WHEN did the status change? The first news-pulse record (data/news_events)
    whose Sleeper `status` or ESPN designation/availability moved onto or off
    an absence. That is when the bot KNEW, which is the right zero for "how long
    did the number lag behind what we knew".
  * How fast did THIS WEEK reprice? The Roboner NFL model's pre-kickoff captures
    (data/nflmodel/raw/projection_archive, many a day, current week only):
    hours from the event to the first capture that moved at least half way to
    where the projection settled before his kickoff. Resolution is the capture
    spacing, printed beside every number.
  * What do FUTURE weeks assume? The daily all-weeks archive
    (data/raw/proj_archive): the first future week back to half his pre-event
    rate is Sleeper's implied return. Compared with ESPN's published return
    date, which it should not precede, and with scout's verdict.

Where the implied return week has been played, whether he actually appeared
comes from nflverse player_stats -- a player who did not play has NO ROW there.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

from robo import DATA, injuries, projarchive, scout, season
from robo.rankings import custom_points

EVENTS = DATA / "news_events"
CAPTURES = DATA / "nflmodel" / "raw" / "projection_archive"
STATS = DATA / "nflmodel" / "parquet"

ABSENT = {"out", "ir", "ir-r", "doubtful", "pup-p", "pup-r", "nfi-r", "sus"}
# Sleeper's team codes where nflverse's lines differ.
TEAM_ALIAS = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS", "ARZ": "ARI", "OAK": "LV"}
STATUS_FIELDS = ("status", "espn_designation", "espn_availability")
NEWS_FIELDS = ("sleeper_news_content", "espn_short")
# A story that says a man got hurt. Only used to pull a spell's first-knowledge
# time earlier than its first designation, never to open a spell on its own.
INJURY_WORDS = re.compile(
    r"\b(injur\w*|hurt|carted|backboard|mri|x-rays?|sprain\w*|strain\w*|"
    r"concuss\w*|fractur\w*|torn|tear|ruled out|limped|exited|left the game|"
    r"did not return|won't return|questionable to return)\b", re.I)
NEWS_LOOKBACK_S = 72 * 3600
# The future-week view reads the first daily snapshot at least this long after
# the event: post-Sunday injuries were repriced in a batch about 28h later.
AS_OF_WAIT_H = 36
# A move smaller than this is not a reprice worth timing.
MIN_MOVE = 1.0
# Only players who were worth something before the event.
MIN_PRE = 3.0


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _when(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%MZ")


def _absent(v) -> bool:
    return str(v or "").strip().lower() in ABSENT


def projection_moves() -> dict[str, list[float]]:
    """{player_id: [times]} of every weekly-projection move the pulse logged.

    The pulse reads Sleeper's weekly feed every twenty minutes and records a
    move of 2+ points or a crossing of zero, so the first one after a status
    change times the reprice to the pulse's own cadence -- far finer than the
    model captures, which can sit a day apart between Sunday and Wednesday.
    """
    out: dict[str, list[float]] = {}
    for f in sorted(EVENTS.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        at = float(doc.get("at") or 0)
        for e in doc.get("events") or []:
            if any(ch.get("field") == "weekly_projection" for ch in e.get("changes") or []):
                out.setdefault(str(e.get("player_id") or ""), []).append(at)
    return out


def _level(v) -> int | None:
    """2 absent, 1 questionable, 0 healthy; None where the value says nothing.

    ESPN relabels a man INACTIVE once his team has played -- a game-day
    formality that neither opens nor closes an injury spell.
    """
    s = str(v or "").strip().lower()
    if s in ABSENT:
        return 2
    if s == "questionable":
        return 1
    if s == "inactive":
        return None
    return 0


def _news_text(ch: dict) -> str:
    h = ch.get("headline") or {}
    return " ".join(str(x or "") for x in (h.get("title"), h.get("description"),
                                           ch.get("after") if ch.get("field") == "espn_short" else ""))


def events() -> tuple[list[dict], list[dict]]:
    """(transition events, Questionable-only spells), from the pulse's records.

    A SPELL opens when a player's worst current flag across Sleeper and ESPN
    leaves healthy and closes when it returns. Its FIRST FLAG is the earliest
    Questionable-or-worse, pulled earlier by an injury-worded story in the
    72 hours before it: the man carted off on Sunday is known to be hurt long
    before anyone designates him. Two clocks follow from that:
      * `first_at` -- when the injury was first known;
      * `at` -- the DECISIVE designation (Out/IR/Doubtful), the old single clock.
    A spell that never gets worse than Questionable is returned separately:
    there the question is not lag but whether Sleeper cut a man who then played.
    """
    state: dict[str, dict[str, int]] = {}
    level: dict[str, int] = {}
    open_: dict[str, dict] = {}
    news: dict[str, list[tuple[float, str]]] = {}
    out, q_only = [], []
    for f in sorted(EVENTS.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        at = float(doc.get("at") or 0)
        for e in doc.get("events") or []:
            pid, name = str(e.get("player_id") or ""), e.get("name") or ""
            for ch in e.get("changes") or []:
                field = ch.get("field")
                if field in NEWS_FIELDS:
                    text = _news_text(ch)
                    if INJURY_WORDS.search(text):
                        news.setdefault(pid, []).append((at, text[:90]))
                    continue
                if field not in STATUS_FIELDS:
                    continue
                s = state.setdefault(pid, {})
                if field not in s:          # the pulse's first sight of this field
                    b = _level(ch.get("before"))
                    if b is not None:
                        s[field] = b
                old = level.get(pid, max(s.values(), default=0))
                a = _level(ch.get("after"))
                if a is None:
                    continue
                s[field] = a
                new = max(s.values())
                level[pid] = new
                if old == 0 and new >= 1:
                    first, sig = at, f"{field}: {ch.get('after')}"
                    prior = [(t, x) for t, x in news.get(pid, []) if at - NEWS_LOOKBACK_S <= t <= at]
                    if prior:
                        first, sig = prior[0][0], f"news: {prior[0][1]}"
                    open_[pid] = {"first_at": first, "first_signal": sig, "decisive": False}
                spell = open_.get(pid) or {}
                if new == 2 and old < 2:
                    out.append({"player_id": pid, "name": name, "direction": "onto",
                                "at": at, "field": field, "after": ch.get("after"),
                                "first_at": spell.get("first_at", at),
                                "first_signal": spell.get("first_signal", "")})
                    if spell:
                        spell["decisive"] = True
                elif old == 2 and new < 2:
                    out.append({"player_id": pid, "name": name, "direction": "off",
                                "at": at, "field": field, "after": ch.get("after"),
                                "to": "questionable" if new == 1 else "healthy"})
                if new == 0 and old >= 1 and pid in open_:
                    sp = open_.pop(pid)
                    if not sp["decisive"]:
                        q_only.append({"player_id": pid, "name": name,
                                       "first_at": sp["first_at"], "end_at": at})
    for pid, sp in open_.items():
        if not sp["decisive"]:
            q_only.append({"player_id": pid, "name": pid, "first_at": sp["first_at"],
                           "end_at": None})
    return out, q_only


def levels() -> dict[str, list[tuple[float, int]]]:
    """{pid: [(t, level)]}: his worst flag across Sleeper and ESPN over time.

    The status AT a moment comes from here, never from a capture row: the
    projection endpoint's embedded player record does not carry live injury
    status (George Kittle read None in all 363 week-2 captures while ESPN and
    the pulse had him Questionable).
    """
    state: dict[str, dict[str, int]] = {}
    out: dict[str, list[tuple[float, int]]] = {}
    for f in sorted(EVENTS.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        at = float(doc.get("at") or 0)
        for e in doc.get("events") or []:
            pid = str(e.get("player_id") or "")
            for ch in e.get("changes") or []:
                if ch.get("field") not in STATUS_FIELDS:
                    continue
                s = state.setdefault(pid, {})
                if ch["field"] not in s:
                    b = _level(ch.get("before"))
                    if b is not None:
                        s[ch["field"]] = b
                        out.setdefault(pid, []).append((0.0, max(s.values())))
                a = _level(ch.get("after"))
                if a is None:
                    continue
                s[ch["field"]] = a
                out.setdefault(pid, []).append((at, max(s.values())))
    return out


def level_at(timeline: list[tuple[float, int]], t: float) -> int:
    lv = 0
    for tt, lv_ in timeline or []:
        if tt > t:
            break
        lv = lv_
    return lv


def _captures() -> tuple[dict, dict, dict, dict]:
    """({week: [(captured_ts, {pid: pts})]}, {(week, team): kickoff},
    {(week, pid): team}, {(week, pid): position}), oldest first.

    THE TEAM IS THE ONE THAT WEEK'S CAPTURES SHOW, not today's player dump: a
    man traded or released since would otherwise be timed against another
    team's kickoff.
    """
    sc = season.scoring()
    by_week, kick, teams, pos = {}, {}, {}, {}
    for f in sorted(glob.glob(str(CAPTURES / "proj_*_wk*_*.json"))):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        w = int(d.get("week") or 0)
        pts = {}
        for r in d.get("rows") or []:
            pid = str(r["player_id"])
            pts[pid] = custom_points(r.get("stats") or {}, sc)
            if r.get("team"):
                teams[(w, pid)] = TEAM_ALIAS.get(r["team"], r["team"])
            p = (r.get("player") or {}).get("position")
            if p:
                pos[(w, pid)] = p
        by_week.setdefault(w, []).append((_ts(d["captured_utc"]), pts))
        for g in d.get("lines") or []:
            if g.get("kickoff_utc"):
                for t in (g["away_team"], g["home_team"]):
                    kick[(w, t)] = _ts(g["kickoff_utc"])
    return by_week, kick, teams, pos


def near_week(ev: dict, caps: dict, kick: dict, teams: dict) -> dict:
    """Reprice lag for the first week whose game follows the event.

    The window ends at his kickoff OR his next status event, whichever is
    first: a man cleared on Wednesday and ruled out on Friday has two events,
    and scoring the first against Friday's zero would call it a reprice.
    """
    for w in sorted(caps):
        team = teams.get((w, ev["player_id"]))
        ko = kick.get((w, team))
        if not ko or ko <= ev["at"]:
            continue
        end = min(ko, ev.get("next_at") or ko)
        series = [(t, p.get(ev["player_id"])) for t, p in caps[w] if t < end]
        series = [(t, v) for t, v in series if v is not None]
        pre = [v for t, v in series if t <= ev["at"]]
        post = [(t, v) for t, v in series if t > ev["at"]]
        if not pre or not post:
            return {"week": w, "why": "no capture on both sides of the event"}
        p0, settled = pre[-1], post[-1][1]
        gaps = [b - a for (a, _), (b, _) in zip(series, series[1:])]
        res = st.median(gaps) / 3600 if gaps else None
        if abs(settled - p0) < MIN_MOVE:
            cut = "next event" if end < ko else "kickoff"
            return {"week": w, "pre": p0, "settled": settled, "lag_h": None,
                    "why": f"never repriced before {cut}", "resolution_h": res}
        half = p0 + (settled - p0) / 2
        prev = max(t for t, _ in series if t <= ev["at"])
        for t, v in post:
            if (settled < p0 and v <= half) or (settled > p0 and v >= half):
                # CAPTURES ARE BURSTY -- dense before a kickoff, a day apart
                # after one -- so the lag is only known to lie between the
                # previous capture and this one. `floor_h` is that lower bound.
                return {"week": w, "pre": p0, "settled": settled,
                        "lag_h": (t - ev["at"]) / 3600,
                        "floor_h": max(0.0, (prev - ev["at"]) / 3600),
                        "resolution_h": res}
            prev = t
    return {"why": "no captured week after the event"}


def future_weeks(ev: dict, snaps: list[tuple[float, Path]]) -> dict:
    """Sleeper's implied return week AS OF the first daily snapshot taken at
    least AS_OF_WAIT_H after the event -- never today's.

    Today's snapshot answers a different question (what Sleeper believes now,
    after weeks more news) and would credit an old event with later
    information. The wait is there because Sleeper reprices post-Sunday
    injuries in a batch about a day later; a snapshot minutes after the event
    would mostly show the pre-injury projection and call it "a full return".
    With no snapshot that late yet, the newest is used and says so.
    """
    before = [p for t, p in snaps if t <= ev["at"]]
    if not before:
        return {"why": "no all-weeks snapshot before the event"}
    after = [(t, p) for t, p in snaps if t >= ev["at"] + AS_OF_WAIT_H * 3600]
    t_b, p_b = after[0] if after else snaps[-1]
    a, b = projarchive.load(before[-1]), projarchive.load(p_b)

    def pts(doc, w):
        c = (doc.get("weeks") or {}).get(str(w), {}).get(ev["player_id"])
        return c.get("pts") if isinstance(c, dict) else c

    weeks = sorted(int(w) for w in (b.get("weeks") or {}))
    base = {"now_week": b.get("current_week"), "as_of": t_b,
            "as_of_waited": bool(after)}
    for w in weeks:
        pre, now = pts(a, w), pts(b, w)
        if pre and now is not None and pre >= MIN_PRE and now >= 0.5 * pre:
            return {**base, "return_week": w}
    return {**base, "return_week": None}


def _espn_dates() -> dict[str, list[tuple[float, str | None]]]:
    """{pid: [(at, return_date)]} from the pulse's own change records."""
    out: dict = {}
    for f in sorted(EVENTS.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        at = float(doc.get("at") or 0)
        for e in doc.get("events") or []:
            for ch in e.get("changes") or []:
                if ch.get("field") == "espn_return_date":
                    v = ch.get("after")
                    out.setdefault(str(e.get("player_id") or ""), []).append(
                        (at, None if str(v) in ("None", "") else str(v)))
    return out


def espn_return_as_of(pid: str, t: float, dates: dict) -> str | None:
    """ESPN's return date as it stood at `t`.

    The latest change the pulse recorded up to `t`. With none recorded, today's
    row counts only if ESPN stamped it no later than `t`; otherwise the date at
    that moment is unknown, and unknown is what is reported.
    """
    seen = [d for at, d in dates.get(pid, []) if at <= t]
    if seen:
        return seen[-1]
    row = injuries.row(pid) or {}
    stamp = row.get("as_of")
    if row.get("return_date") and stamp:
        try:
            if _ts(stamp.replace("Z", "+00:00")) <= t:
                return row["return_date"]
        except ValueError:
            pass
    return None


def _verdict_history() -> dict[str, list[dict]]:
    out: dict = {}
    path = scout.history_path()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out.setdefault(str(r.get("player_id")), []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: float(r.get("logged_at") or 0))
    return out


def verdict_as_of(pid: str, t: float, hist: dict) -> dict:
    """The newest verdict logged or first observed by `t`."""
    seen = [r for r in hist.get(pid, []) if float(r.get("logged_at") or 0) <= t]
    return seen[-1] if seen else {}


def _played() -> set[tuple[str, int]]:
    """(sleeper_id, week) for every 2026 appearance. Absence has no row."""
    try:
        import polars as pl
        from robo import roles
        by_gsis = roles._by_gsis()
        df = pl.read_parquet(STATS / f"player_stats_{season.SEASON}.parquet",
                             columns=["player_id", "week"])
        return {(by_gsis[g], int(w)) for g, w in df.iter_rows() if g in by_gsis}
    except Exception:
        return set()


def questionable_section(q_only: list[dict], caps: dict, kick: dict, teams: dict,
                         played: set, now_week: int) -> list[str]:
    """Spells that never got worse than Questionable, where his game is played.

    Not a lag question: the answer is on the field. Did he play, and how much
    had Sleeper cut him by kickoff? Cutting men who then play is over-reaction,
    the churn risk; keeping men who then sit is the opposite error.
    """
    rows = []
    for sp in q_only:
        for w in sorted(caps):
            ko = kick.get((w, teams.get((w, sp["player_id"]))))
            if not ko or ko <= sp["first_at"] or (sp["end_at"] and ko > sp["end_at"] + 7 * 86400):
                continue
            if w >= now_week:
                break                               # not played yet
            series = [(t, p.get(sp["player_id"])) for t, p in caps[w] if t < ko]
            pre = [v for t, v in series if t <= sp["first_at"] and v is not None]
            last = [v for t, v in series if v is not None]
            if pre and last and pre[-1] >= MIN_PRE:
                rows.append({**sp, "week": w, "pre": pre[-1], "ko": last[-1],
                             "played": (sp["player_id"], w) in played})
            break
    if not rows:
        return ["", "  QUESTIONABLE-ONLY SPELLS: none with a played game yet"]
    L = ["", f"  QUESTIONABLE-ONLY SPELLS with a played game: {len(rows)}"]
    for label, grp in (("played", [r for r in rows if r["played"]]),
                       ("sat", [r for r in rows if not r["played"]])):
        if not grp:
            L.append(f"    {label}: none")
            continue
        kept = sorted(r["ko"] / r["pre"] for r in grp)
        L.append(f"    {label:<6} {len(grp):3d}  Sleeper kept a median {st.median(kept):.0%} "
                 f"of his pre-flag projection by kickoff"
                 + (f"; cut 25%+ on {sum(k < 0.75 for k in kept)} who then played"
                    if label == "played" else
                    f"; still had 75%+ on {sum(k >= 0.75 for k in kept)} who then sat"))
    n_play = sum(r["played"] for r in rows)
    L.append(f"    {n_play}/{len(rows)} Questionable-only players played "
             f"({n_play / len(rows):.0%})")
    return L


def report(player: str | None = None) -> str:
    evs, q_only = events()
    for e in evs:
        e["next_at"] = min((x["at"] for x in evs
                            if x["player_id"] == e["player_id"] and x["at"] > e["at"]),
                           default=None)
    evs = [e for e in evs if not player or player.lower() in e["name"].lower()]
    caps, kick, teams, _pos = _captures()
    from robo import sleeper_read as api
    dump = api.players() or {}
    for sp in q_only:               # a display name only; nothing is decided on it
        sp["name"] = (dump.get(sp["player_id"]) or {}).get("full_name") or sp["name"]
    q_only = [s for s in q_only if not player or player.lower() in s["name"].lower()]
    snaps = [(float(projarchive.load(p).get("captured") or 0), p)
             for p in projarchive.snapshots()]
    now_week = int(projarchive.load(snaps[-1][1]).get("current_week") or 0) if snaps else 0
    hist = _verdict_history()
    dates = _espn_dates()
    played = _played()
    moves = projection_moves()

    rows = []
    for ev in evs:
        # The same pulse can log the status and the projection together:
        # a lag of 0 means Sleeper had already repriced when we noticed.
        until = ev.get("next_at") or float("inf")
        pm = [t for t in moves.get(ev["player_id"], []) if ev["at"] <= t < until]
        ev["pulse_lag_h"] = (pm[0] - ev["at"]) / 3600 if pm else None
        nw = near_week(ev, caps, kick, teams)
        # THE SECOND CLOCK: from the first flag of the spell, not the decisive
        # designation. The reprice time is the pulse's if it saw one, else the
        # capture that did.
        lag = ev["pulse_lag_h"] if ev["pulse_lag_h"] is not None else nw.get("lag_h")
        if ev["direction"] == "onto" and lag is not None:
            ev["from_first_h"] = (ev["at"] + lag * 3600 - ev["first_at"]) / 3600
            ev["head_start_h"] = (ev["at"] - ev["first_at"]) / 3600
        if nw.get("pre") is not None and max(nw["pre"], nw.get("settled") or 0) < MIN_PRE:
            continue
        fw = future_weeks(ev, snaps) if ev["direction"] == "onto" else {}
        # EVERYTHING BESIDE SLEEPER'S VIEW IS READ AT THE SAME MOMENT: ESPN's
        # date and scout's verdict as they stood when that snapshot was taken,
        # never as they stand today. ESPN's date is read directly, not through
        # floor_week(), which loses it once ESPN relabels a man INACTIVE.
        as_of = fw.get("as_of") or ev["at"]
        rd = espn_return_as_of(ev["player_id"], as_of, dates) if ev["direction"] == "onto" else None
        floor = injuries.week_of(rd) if rd else None
        v = verdict_as_of(ev["player_id"], as_of, hist)
        rows.append({**ev, **{f"n_{k}": x for k, x in nw.items()},
                     **{f"f_{k}": x for k, x in fw.items()},
                     "floor": floor, "verdict": v.get("verdict"),
                     "conf": v.get("confidence"), "scout_week": v.get("return_week")})

    L = [f"SLEEPER REPRICE LAG -- {len(rows)} status events on players projected "
         f">= {MIN_PRE:g} (read-only)", ""]
    for d in ("onto", "off"):
        lags = [r["n_lag_h"] for r in rows if r["direction"] == d and r.get("n_lag_h") is not None]
        never = sum(1 for r in rows if r["direction"] == d
                    and str(r.get("n_why", "")).startswith("never"))
        pl = sorted(r["pulse_lag_h"] for r in rows
                    if r["direction"] == d and r.get("pulse_lag_h") is not None)
        if pl:
            L.append(f"  {'placed ON' if d == 'onto' else 'taken OFF'} an absence, by the "
                     f"20-min pulse: {len(pl)} projection moves logged after the status "
                     f"change -- median {st.median(pl):.1f}h, p90 "
                     f"{pl[int(0.9 * (len(pl) - 1))]:.1f}h, max {pl[-1]:.1f}h; "
                     f"{sum(1 for x in pl if x == 0)} already moved when the status did")
        if lags:
            q = sorted(lags)
            floors = sorted(r["n_floor_h"] for r in rows if r["direction"] == d
                            and r.get("n_floor_h") is not None)
            L.append(f"  {'placed ON' if d == 'onto' else 'taken OFF'} an absence, by "
                     f"model captures: {len(lags)} repriced within median {st.median(q):.1f}h "
                     f"(p90 {q[int(0.9 * (len(q) - 1))]:.1f}h, max {q[-1]:.1f}h); "
                     f"no sooner than median {st.median(floors):.1f}h "
                     f"(p90 {floors[int(0.9 * (len(floors) - 1))]:.1f}h); "
                     f"{never} never repriced before kickoff or their next event")
    ff = sorted(r["from_first_h"] for r in rows if r.get("from_first_h") is not None)
    hs = sorted(r["head_start_h"] for r in rows if r.get("head_start_h") is not None)
    if ff:
        L.append(f"  placed ON an absence, clocked from the FIRST FLAG of the spell "
                 f"(Questionable, or an injury story up to 72h earlier): repriced after "
                 f"median {st.median(ff):.1f}h, p90 {ff[int(0.9 * (len(ff) - 1))]:.1f}h, "
                 f"max {ff[-1]:.1f}h; the first flag led the decisive designation by "
                 f"median {st.median(hs):.1f}h ({sum(h > 0 for h in hs)} of {len(hs)} "
                 f"had an earlier flag)")
    early = [r for r in rows if r.get("f_return_week") and r.get("floor")
             and r["f_return_week"] < r["floor"]]
    doubt = [r for r in rows if r.get("f_return_week") and r.get("verdict") == "avoid"
             and (r.get("conf") or 0) >= 0.7]
    waiting = sum(1 for r in rows if r["direction"] == "onto" and r.get("f_as_of")
                  and not r.get("f_as_of_waited"))
    L.append(f"  future weeks (as of the first daily snapshot {AS_OF_WAIT_H}h+ after each "
             f"event, ESPN and scout read at that same moment): {len(early)} projected back "
             f"BEFORE ESPN's return week; {len(doubt)} projected back while scout said "
             f"avoid (conf >= 0.7)"
             + (f"; {waiting} too recent for a {AS_OF_WAIT_H}h snapshot, read from the newest"
                if waiting else ""))
    L += ["", f"  {'player':<22} {'dir':<4} {'event':<12} {'wk':>2} {'pre':>5} {'->':>2} "
          f"{'set':>5} {'lag h':>11} {'pulse h':>7} {'1st flag h':>10}   {'sleeper back':>12} "
          f"{'espn back':>10} {'scout':<14} played?"]
    for r in sorted(rows, key=lambda r: r["at"]):
        dir_ = r["direction"] if r["direction"] == "onto" else (
            "offQ" if r.get("to") == "questionable" else "off")
        first = (format(r["from_first_h"], "10.1f")
                 if r.get("from_first_h") is not None else f"{'--':>10}")
        lag = (f"{r['n_floor_h']:5.1f}-{r['n_lag_h']:<5.1f}" if r.get("n_lag_h") is not None
               else f"{'never' if r.get('n_why', '').startswith('never') else '--':>11}")
        back = r.get("f_return_week")
        seen = ""
        if back and back < (r.get("f_now_week") or 0):
            seen = "yes" if (r["player_id"], back) in played else "NO"
        scout_txt = (f"{r['verdict']} {r['conf']:.2f}" if r.get("verdict")
                     and r.get("conf") is not None else "")
        L.append(f"  {r['name'][:22]:<22} {dir_:<4} {_when(r['at']):<12} "
                 f"{str(r.get('n_week') or ''):>2} "
                 f"{(r.get('n_pre') if r.get('n_pre') is not None else float('nan')):5.1f} -> "
                 f"{(r.get('n_settled') if r.get('n_settled') is not None else float('nan')):5.1f} "
                 f"{lag} "
                 f"{(format(r['pulse_lag_h'], '7.1f') if r.get('pulse_lag_h') is not None else '     --')}"
                 f" {first}"
                 f"   {str(back or ('--' if r['direction'] == 'off' else 'not in season')):>12} "
                 f"{str(r.get('floor') or ''):>10} {scout_txt:<14} {seen}")
    L += questionable_section(q_only, caps, kick, teams, played, now_week)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="how long Sleeper takes to reprice a status change")
    ap.add_argument("--player", help="substring of a player's name")
    a = ap.parse_args()
    print(report(a.player))


if __name__ == "__main__":
    main()
