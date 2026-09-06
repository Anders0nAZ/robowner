"""Public status dashboard — is the bot alive, and is what it knows current?

Renders decision-log/status.html, the third page of the public site alongside the
decision log and the dev log. Nate checks this from a phone, so it answers the
questions you cannot answer from a phone today: is the responder up, how close is
it to its hourly cap in each chat, when did each data source last land, when does
it next run, and is it keeping up with the season.

The scheduled-job table spans BOTH projects -- Roboner's own tasks and the NFL
Model capture that produces the weekly projections the lineup is decided on.
A data source whose producer is off the page can only ever show a symptom.

GitHub Pages is static, so this page is a SNAPSHOT. That is its main design
hazard: a frozen page reading ALL GREEN is worse than no page. It therefore
carries its own generation timestamp and ages itself in the browser, going amber
past 20 minutes and red past 45.

PUBLIC MEANS PUBLIC. Two rules follow:
  * Everything rendered goes through _scrub(). groupme.py passes the access token
    as a QUERY PARAMETER, requests puts the full URL in its exception text, and
    chat_responder writes that text into the heartbeat's `note` -- so the naive
    version of this page publishes the GroupMe token the first time GroupMe
    returns a 401.
  * Machine load (VRAM, model residency, GPU placement) is Nate's business, not
    the league's. llm() is collected for the terminal report and never reaches
    the page beyond a bare reachable/not-reachable line.
The page carries operational state only -- is it up, is its data current, is it
ready. Anything a collector returns that is not already public stays in the
terminal report; when in doubt, it does not go on the page.

python -m robo.status              # full report to the terminal (unredacted)
python -m robo.status --render     # write the page
python -m robo.status --publish    # write + throttled commit/push
python -m robo.status --json       # machine-readable snapshot
"""

import argparse
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from robo import DATA, DRAFT_ID_2026, MODEL_ROOT, RAW, ROOT, injuries

OUT = ROOT / "decision-log" / "status.html"
STATE = DATA / "status_state.json"
REPLY_HISTORY = DATA / "reply_history.json"
REFRESH_LOG = ROOT / "refresh.log"

STALE_HEARTBEAT = 300      # matches RobonerWatchdog's $staleAfter

# Push at least this often even when nothing has changed. Deliberately just
# UNDER the watchdog's 15-minute timer, so every watchdog run publishes and the
# live page is never more than one cycle behind. At 30 minutes it was not: the
# file was rewritten every 15 minutes but only PUSHED on a changed verdict, so
# the only copy anyone reads sat 15-30 minutes stale while claiming otherwise,
# and tripped its own 20-minute staleness warning on a perfectly healthy bot.
# It still throttles the draft guard, which runs every 2 minutes for eight hours
# on draft day; a live draft bypasses it entirely (see publish()).
PUSH_MAX_INTERVAL = 13 * 60

# When the page should distrust itself, in seconds. Both are comfortably past
# two missed publishes, so amber means "the machine writing this has stopped",
# not "it is between runs" -- a warning that cries wolf every cycle is worse
# than no warning, because you learn to scroll past it.
PAGE_WARN_AGE = 35 * 60
PAGE_BAD_AGE = 75 * 60

HISTORY_KEEP_DAYS = 30

OK, WARN, BAD, UNK = "ok", "warn", "bad", "unknown"


# --------------------------------------------------------------------------
# redaction

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# Lookbehind rather than \b: an underscore IS a word character, so \b never
# fires inside GROUPME_BOT_ID or NATE_SLEEPER_TOKEN -- which is the exact shape
# every secret in this project's .env has.
_SECRET_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9])(token|api[_-]?key|apikey|access[_-]?token|bot[_-]?id"
    r"|secret|password|auth)\s*[=:]\s*[\"']?([^\s&\"'<>,}]{4,})")
# Backslash only, deliberately. An earlier version accepted a forward slash too,
# so it matched the "s:/" inside "https://..." and shredded every URL in an error
# message while leaving the part that actually mattered behind.
_WINPATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]*")
# The project path contains spaces, which the pattern above stops at, so on its
# own it would publish " Robo Owner\robo\groupme.py" out of a real traceback.
# All three spellings, because a path reaches us raw from an OSError, escaped
# from a repr()'d traceback, and slash-separated from anything pathlib touched.
# BOTH repo roots. The NFL Model's directory name has a space in it too, so a
# traceback naming its export file published everything after "C:\NFL " until
# this covered it.
_ROOT_FORMS = tuple(
    form for base in (ROOT, MODEL_ROOT)
    for form in (str(base).replace("\\", "\\\\"), str(base),
                 str(base).replace("\\", "/")))


def _scrub(text, limit: int = 240) -> str:
    """Everything that reaches the page passes through here. See module docstring."""
    if text is None:
        return ""
    s = str(text)
    s = _JWT.sub("<redacted-token>", s)
    s = _SECRET_KV.sub(lambda m: m.group(1) + "=<redacted>", s)
    for form in _ROOT_FORMS:
        s = s.replace(form, "<path>")
    s = _WINPATH.sub("<path>", s)
    return s[:limit]


# --------------------------------------------------------------------------
# small helpers

def _safe(fn, default=None):
    """Collectors never raise. A failed probe is 'unknown', not a traceback."""
    try:
        return fn()
    except Exception as e:
        if default is not None:
            return default
        return {"error": _scrub(repr(e))}


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _ago(ts) -> str:
    if not ts:
        return "never"
    d = max(0.0, time.time() - float(ts))
    if d < 90:
        return "%ds ago" % int(d)
    if d < 5400:
        return "%dm ago" % int(d / 60)
    if d < 172800:
        return "%.1fh ago" % (d / 3600)
    return "%dd ago" % int(d / 86400)


def _clock(ts) -> str:
    if not ts:
        return "not scheduled"
    return time.strftime("%a %d %b %H:%M", time.localtime(float(ts)))


def _tz_label() -> str:
    """How to name the clock every time on this page is measured against.

    Derived from the machine's actual offset rather than written down: the name
    and the offset cannot then disagree, which is the only way a hardcoded "MST"
    could quietly become a lie.

    ASCII only, like every other collector string: the same value is printed to
    a cp1252 Windows console and embedded in UTF-8 HTML, and the console is the
    one that raises.
    """
    lt = time.localtime()
    off = -(time.altzone if lt.tm_isdst else time.timezone)
    h, m = divmod(abs(off) // 60, 60)
    utc = "UTC%s%d%s" % ("+" if off >= 0 else "-", h, (":%02d" % m) if m else "")
    if off == -7 * 3600 and not lt.tm_isdst:
        return "Arizona time (MST, %s), which never shifts for daylight saving" % utc
    return utc


# Exception text as the responder logs it, in words that say what went wrong.
# Substring match on purpose: the point is to recognise the SHAPE of a common
# failure, not to enumerate every exception the transports can raise.
_ERROR_SHAPES = (
    ("Expecting value", "got a reply that was not JSON"),
    ("JSONDecode", "got a reply that was not JSON"),
    ("timed out", "timed out"),
    ("Timeout", "timed out"),
    ("Max retries", "could not be reached"),
    ("ConnectionError", "could not be reached"),
    ("NewConnectionError", "could not be reached"),
    ("401", "was refused (401 unauthorised)"),
    ("403", "was refused (403 forbidden)"),
    ("429", "was rate-limited (429)"),
    ("500", "hit a server error (500)"),
    ("502", "hit a bad gateway (502)"),
    ("503", "hit an unavailable service (503)"),
)


def _plain(err: str) -> str:
    for needle, said in _ERROR_SHAPES:
        if needle in err:
            return said
    return "failed with " + err[:60]


def _responder_why(hb) -> tuple[str, str]:
    """Why the responder is not OK, in a sentence that is not the dot's colour.

    This used to read "the last cycle reported degraded", which told the reader
    the thing they could already see and nothing else. The note behind it is
    written by chat_responder._run as "channel: exception", joined by "; ", so
    WHICH channel broke and WHAT it hit are both already in hand -- they were
    just never said. The first real use of this row found a three-day Sleeper
    chat outage that had been sitting behind that sentence.
    """
    note = (hb.get("note") or "").strip()
    if not note:
        return "its last cycle did not finish cleanly", ""
    chans, causes = [], []
    for part in (x.strip() for x in note.split(";")):
        if not part:
            continue
        chan, sep, err = part.partition(":")
        chans.append(chan.strip() if sep else "a channel")
        causes.append(_plain((err or part).strip()))
    if not chans:
        return "its last cycle did not finish cleanly", note
    detail = "Reported by the responder itself on its most recent cycle: " + note
    if len(set(causes)) == 1:
        who = (chans[0] if len(chans) == 1
               else ", ".join(chans[:-1]) + " and " + chans[-1])
        return ("the %s channel%s %s on the last poll"
                % (who, "" if len(chans) == 1 else "s", causes[0]), detail)
    # Different failures on different channels: pair them up rather than
    # gluing the causes together, which read as one run-on sentence about
    # both -- "the groupme and sleeper channels timed out; got a reply that
    # was not JSON".
    return ("; ".join("%s %s" % (c, k) for c, k in zip(chans, causes))
            + " on the last poll", detail)


def _age_verdict(ts, max_age_s):
    """(status, why). Fresh under budget, warn over it, bad at double.

    Returns the REASON alongside the verdict rather than leaving the renderer to
    work backwards from a colour. A page that can say "amber" but not "amber
    because" makes the reader open a terminal, which is the one thing it exists
    to avoid.
    """
    if not ts:
        return BAD, "has never landed"
    age = time.time() - float(ts)
    if age <= max_age_s:
        return OK, ""
    over = (age - max_age_s) / 3600.0
    budget = "%gh" % round(max_age_s / 3600.0, 1)
    if age <= max_age_s * 2:
        return WARN, "%.1fh past its %s budget" % (over, budget)
    return BAD, "more than double its %s budget" % budget


def _worst(*states):
    for s in (BAD, WARN, UNK, OK):
        if s in states:
            return s
    return UNK


# --------------------------------------------------------------------------
# collectors

def responder() -> dict:
    """Heartbeat, process uptime, and per-channel rate-cap headroom."""
    from robo import chat_responder as cr

    hb = _read_json(DATA / "roboner_heartbeat.json", {}) or {}
    hb_ts = hb.get("ts")
    age = (time.time() - hb_ts) if hb_ts else None
    alive = bool(age is not None and age < STALE_HEARTBEAT)

    pid, uptime = None, None
    try:
        import psutil
        for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if "robo.chat_responder" in cmd:
                pid = p.info["pid"]
                uptime = time.time() - p.info["create_time"]
                break
    except Exception:
        pass

    fails = _read_json(DATA / "chat_cycle_failures.json", {}) or {}
    chans = []
    for name in cr.CHANNELS:
        stamps = _read_json(DATA / ("chat_replies_%s.json" % name), []) or []
        hour = len([s for s in stamps if s > time.time() - 3600])
        nfail = int(fails.get(name, 0) or 0)
        cwhy = ""
        if nfail >= cr.MAX_BATCH_ATTEMPTS:
            cs = BAD
            cwhy = ("%d failed batches in a row (max %d) -- this channel has "
                    "given up on its current batch" % (nfail, cr.MAX_BATCH_ATTEMPTS))
        elif hour >= cr.MAX_REPLIES_PER_HOUR:
            cs = WARN
            cwhy = ("at the hourly cap, %d of %d -- further replies wait for the "
                    "hour to roll over" % (hour, cr.MAX_REPLIES_PER_HOUR))
        elif nfail:
            cs = WARN
            cwhy = "%d failed batch%s so far (gives up at %d)" % (
                nfail, "" if nfail == 1 else "es", cr.MAX_BATCH_ATTEMPTS)
        else:
            cs = OK
        chans.append({
            "name": name,
            "last_reply": max(stamps) if stamps else None,
            "hour": hour,
            "cap": cr.MAX_REPLIES_PER_HOUR,
            "headroom": max(0, cr.MAX_REPLIES_PER_HOUR - hour),
            "failures": nfail,
            "max_failures": cr.MAX_BATCH_ATTEMPTS,
            "status": cs, "why": cwhy, "why_detail": "",
        })

    status = OK if alive else BAD
    why = "" if alive else "no heartbeat -- the responder process is not running"
    why_detail = ""
    if alive and (hb.get("status") or "ok") != "ok":
        status = WARN
        why, why_detail = _responder_why(hb)
    return {
        "status": status, "alive": alive,
        "why": why, "why_detail": why_detail,
        "heartbeat_ts": hb_ts, "heartbeat_age": age,
        "heartbeat_status": hb.get("status") or "unknown",
        "note": hb.get("note") or "",
        "pid": pid, "uptime": uptime,
        "channels": chans, "model": cr.MODEL,
    }


def reply_history() -> dict:
    """A durable reply tally the responder does not have to maintain.

    _mark_reply() prunes chat_replies_*.json to two hours, so 24h/7d counts do
    not exist on disk. This runs every 15 minutes, well inside that window, so
    merging what it finds into data/reply_history.json loses nothing -- and it
    keeps the bookkeeping out of the responder's hot loop.
    """
    from robo import chat_responder as cr
    hist = _read_json(REPLY_HISTORY, {}) or {}
    cutoff = time.time() - HISTORY_KEEP_DAYS * 86400
    for name in cr.CHANNELS:
        stamps = _read_json(DATA / ("chat_replies_%s.json" % name), []) or []
        old = hist.get(name) or []
        merged = sorted({round(float(s), 3) for s in list(old) + list(stamps)
                         if float(s) > cutoff})
        hist[name] = merged
    try:
        REPLY_HISTORY.write_text(json.dumps(hist), encoding="utf-8")
    except OSError:
        pass

    now = time.time()
    allst = [s for v in hist.values() for s in v]
    buckets = [0] * 24
    for s in allst:
        h = int((now - s) // 3600)
        if 0 <= h < 24:
            buckets[23 - h] += 1
    return {
        "day": len([s for s in allst if s > now - 86400]),
        "week": len([s for s in allst if s > now - 7 * 86400]),
        "hourly": buckets,
        "per_channel_day": {k: len([s for s in v if s > now - 86400])
                            for k, v in hist.items()},
    }


def llm() -> dict:
    """TERMINAL REPORT ONLY -- see the module docstring. The page gets a bare
    reachable/not line and never a number."""
    import requests
    from robo import chat_responder as cr
    out = {"reachable": False, "model": cr.MODEL, "present": False,
           "resident": False, "vram_frac": None, "status": BAD}
    try:
        tags = requests.get("http://localhost:11434/api/tags", timeout=8).json()
        out["reachable"] = True
        out["present"] = any(m.get("name") == cr.MODEL for m in tags.get("models", []))
    except Exception:
        return out
    try:
        ps = requests.get("http://localhost:11434/api/ps", timeout=8).json()
        for m in ps.get("models", []):
            if m.get("name") == cr.MODEL:
                out["resident"] = True
                size = m.get("size") or 0
                out["vram_frac"] = (m.get("size_vram") or 0) / size if size else None
                out["expires_at"] = m.get("expires_at")
    except Exception:
        pass
    # Nothing resident is normal: keep_alive is 30m and triggers are rare.
    if not out["present"]:
        out["status"] = BAD
    elif out["resident"] and (out["vram_frac"] or 0) < 0.5:
        out["status"] = BAD   # the same threshold RobonerGpu.ps1 asserts
    else:
        out["status"] = OK
    return out


# refresh.py step name -> what the league would call it, how stale is too stale,
# and which scheduled task refreshes it.
SOURCES = [
    ("players",     "Sleeper player dump",  26 * 3600, "RobonerRefresh"),
    ("projections", "Sleeper projections",  26 * 3600, "RobonerRefresh"),
    ("buzz",        "Trending adds/drops",  26 * 3600, "RobonerRefresh"),
    ("board",       "Ranking board",        26 * 3600, "RobonerRefresh"),
    # 30 hours, matching model_proj.MAX_AGE_H: the page should go amber at the
    # same moment the lineup stops trusting the file, not before it and not
    # after. Moving one without the other is how the page reports healthy on a
    # source nothing is using.
    ("model",       "NFL Model projections", 30 * 3600, "RobonerRefresh"),
    # 20 hours, matching ros.MAX_AGE_H, so the page goes amber at the same
    # moment the valuation stops trusting itself -- the same rule as the model
    # row above.
    ("ros",         "Rest-of-season value", 20 * 3600, "RobonerRefresh"),
    ("playoff-odds", "Playoff odds",        30 * 3600, "RobonerRefresh"),
    # 54 hours, matching injuries.MAX_AGE_H, so the page goes amber at the same
    # moment expected.py stops trusting the file -- the same rule as the model
    # and ros rows. When it does stop, the eligibility floor falls back to being
    # inferred from Sleeper's projection, which is what priced men on injured
    # reserve for weeks they are barred from playing.
    ("injuries",    "ESPN injury report",   54 * 3600, "RobonerRefresh"),
    # On this list since 5 Sep 2026. It used to be deliberately absent, because
    # verdicts fed the draft board's bench valuation and nothing else, and
    # nothing ran scout once the draft was over. Both halves changed together:
    # expected.py now reads the dated half through role_signal(), and refresh
    # runs scout daily.
    ("scout",       "Player news judged",   26 * 3600, "RobonerRefresh"),
    # Produced by the OTHER repo's daily ingest. Before week 1 is played there
    # is nothing to hold and the row reads BAD with "no player_stats yet" --
    # which is the honest state, and the reason roles.py is running on last
    # season's shares until then.
    ("usage",       "nflverse usage data",  30 * 3600, "NFLModelCaptureDaily"),
    ("chat-memory", "League chat index",    26 * 3600, "RobonerRefresh"),
    ("media-pool",  "Reaction image pool",  26 * 3600, "RobonerRefresh"),
    # ADP and ECR are deliberately absent, for the same reason and with the same
    # restore condition as scout below.
    #   adp-live is average DRAFT position. It has no in-season consumer at all
    #     -- nothing in skills, lineup, moves, season, bench or ir reads it --
    #     and the daily fetch that fed it has been dropped from the pipeline.
    #   ecr is still fetched, because the board blends it into blend_pts and the
    #     chat tools read the board. But FantasyPros publishes DRAFT rankings,
    #     which stop moving once the season starts, so a 26-hour budget would
    #     have gone amber within days of week 1 for a file that is behaving
    #     exactly as expected.
    # Scout is deliberately absent. Its verdicts feed the DRAFT board's bench
    # valuation, nothing reads them now, and RobonerScout was unregistered with
    # the rest of the draft-day tasks -- so a 72-hour freshness budget would
    # have taken the page amber in three days and red in six, blaming a job that
    # was removed on purpose. Put this row back the moment robo/value.py starts
    # consuming verdicts, and schedule the task again in the same change.
]

_LOGLINE = re.compile(
    r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]\s+([a-z-]+):\s+(OK|FAILED)\s*(.*)$")


def _refresh_log() -> dict:
    """Last OK and last FAILED per step.

    Better than file mtimes: every fetch in refresh.py validates before it
    overwrites, so a rejected pull leaves yesterday's file in place with
    yesterday's mtime and no other trace. The log is where that shows up.
    """
    out = {}
    try:
        lines = REFRESH_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = _LOGLINE.match(line.strip())
        if not m:
            continue
        stamp, step, verdict, detail = m.groups()
        try:
            ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        e = out.setdefault(step, {})
        if verdict == "OK":
            e["last_ok"] = ts
            e["detail"] = detail.strip()
        else:
            e["last_fail"] = ts
            e["fail_detail"] = detail.strip()
    return out


# Steps whose output file we can inspect directly. For these, an absent marker
# means the file itself is gone -- a hard failure, not a reason to consult the
# log. The two omitted steps (chat-memory, media-pool) write into databases whose
# freshness is only recorded in the log, so there the log is all there is.
MARKED_STEPS = {"players", "projections", "buzz", "board", "model",
                "ros", "playoff-odds", "usage", "injuries", "scout"}

# Sources whose ABSENCE is a normal state at some point in the year, and so must
# not paint the page red on a perfectly healthy bot. A false alarm that runs for
# a week teaches the reader to ignore the dot, which costs more than the warning
# was ever worth -- the same reasoning that keeps adp-live, ecr and scout off
# the SOURCES list entirely.
SOFT_ABSENT = {"usage"}


def _absence_is_expected(step: str) -> bool:
    if step != "usage":
        return False
    # nflverse cannot publish a season's usage before the season has been
    # played. Until week 1 kicks off there is nothing missing, and roles.py
    # says so itself by falling back to last season's shares.
    try:
        from robo import season as _season, vegas as _vegas
        return _vegas.next_kickoff(_season.SEASON, 1) is not None
    except Exception:
        return False


def _source_marker(step: str):
    """Each file's own freshness claim, which beats its mtime."""
    if step == "board":
        p = DATA / "board_2026.csv"
        n = 0
        try:
            with p.open(encoding="utf-8") as f:
                n = max(0, sum(1 for _ in f) - 1)
        except OSError:
            pass
        return _mtime(p), "%d players" % n
    if step == "adp-live":
        p = DATA / "adp_live.json"
        d = _read_json(p, {}) or {}
        meta = d.get("meta", {})
        return _mtime(p), "%d players, drafts through %s" % (
            len(d.get("players", [])), meta.get("end_date", "?"))
    if step == "ecr":
        p = DATA / "ecr_superflex_half.json"
        d = _read_json(p, {}) or {}
        return _mtime(p), "%d players, %s experts, dated %s" % (
            len(d.get("players", [])), d.get("experts", "?"), d.get("updated", "?"))
    if step == "buzz":
        d = _read_json(DATA / "buzz.json", {}) or {}
        return d.get("ts"), "%d players over %sh" % (
            len(d.get("net", {})), d.get("hours", "?"))
    if step == "model":
        d = _read_json(DATA / "model_week.json", {}) or {}
        ts = None
        try:
            ts = datetime.fromisoformat(d["generated_utc"]).timestamp()
        except (KeyError, TypeError, ValueError):
            pass
        return ts, "%d players, %s week %s" % (
            len(d.get("players", {})), d.get("season", "?"), d.get("week", "?"))
    if step == "ros":
        d = _read_json(DATA / "ros.json", {}) or {}
        return d.get("computed"), "%d players from week %s" % (
            len(d.get("players", {})), d.get("week", "?"))
    if step == "playoff-odds":
        d = _read_json(DATA / "playoff_odds.json", {}) or {}
        ours = (d.get("odds") or {}).get(d.get("ours") or "")
        return d.get("computed"), "%d teams, ours %s" % (
            len(d.get("odds", {})),
            "%.0f%%" % (ours * 100) if ours is not None else "?")
    if step == "usage":
        # nflverse's CURRENT-season player_stats, which roles.py reads to work
        # out who actually has the job. It is refreshed by the NFL Model's
        # ingest, not ours -- hence the NFLModelCaptureDaily attribution -- and
        # it legitimately does not exist until week 1 has been played, which the
        # detail says rather than the dot pretending it is broken.
        try:
            from robo import roles, season as _season
            f = roles.freshness(_season.SEASON)
        except Exception:
            return None, "unreadable"
        if not f.get("ok"):
            return None, f.get("why", "absent")
        import time as _t
        return _t.time() - f["age_h"] * 3600.0, "%.0fh old" % f["age_h"]
    if step == "projections":
        p = RAW / "projections_2026.json"
        return _mtime(p), "%d rows" % len(_read_json(p, []) or [])
    if step == "players":
        return _mtime(RAW / "players_nfl.json"), "cached 24h"
    if step == "injuries":
        p = DATA / "injuries_espn.json"
        d = _read_json(p, {}) or {}
        out = sum(1 for r in (d.get("players") or {}).values()
                  if (r.get("designation") or "") in injuries.ABSENT)
        return _mtime(p), "%d joined, %d unable to play" % (
            len(d.get("players") or {}), out)
    if step == "scout":
        d = _read_json(DATA / "news_verdicts.json", {}) or {}
        # COUNTS ONLY. The reasons quote injury reporting and, in one case, a
        # named player's criminal charge; only a verdict's magnitude is public.
        return d.get("written"), "%d players judged" % len(d.get("verdicts", {}))
    return None, ""


_TIMING = re.compile(r"\s*\(\d+\.\d+s\)$")
_POOL = re.compile(r"^\{(.*?)\}, (\d+) embedded$")


def _pretty(step: str, detail: str) -> str:
    """refresh.log details are written for an engineer reading a log file; this
    page is read by the league. Same facts, said out loud."""
    d = _TIMING.sub("", (detail or "").strip())
    if step == "media-pool":
        m = _POOL.match(d)
        if m:
            added = sum(int(n) for n in re.findall(r":\s*(\d+)", m.group(1)))
            return "%d new images, %d re-indexed" % (added, int(m.group(2)))
    if step == "chat-memory":
        nums = re.findall(r"\+(\d+)", d)
        if nums:
            return "%d new messages indexed" % sum(int(n) for n in nums)
    return d


def ingests() -> list:
    log = _refresh_log()
    rows = []
    for step, label, max_age, task in SOURCES:
        entry = log.get(step, {})
        mark_ts, detail = _source_marker(step)
        # The log is authoritative for "did it succeed"; the marker is
        # authoritative for "how old is what we actually hold".
        if step in MARKED_STEPS and not mark_ts:
            # No falling back to the log here. The log would happily report this
            # morning's success for a file that has since been deleted or
            # corrupted, and "fresh" is the worst possible answer to give about
            # something that is gone.
            ts, status = None, BAD
            # The marker gets to say WHY when it knows. The generic text is
            # right about a file that vanished and wrong about one that is not
            # supposed to exist yet, and those look identical from here.
            why = detail or "the file we hold is missing or unreadable"
            detail = detail or "missing or unreadable"
            if step in SOFT_ABSENT and _absence_is_expected(step):
                status = WARN
        else:
            ts = mark_ts or entry.get("last_ok")
            status, why = _age_verdict(ts, max_age)
        failed_since = bool(entry.get("last_fail")
                            and entry["last_fail"] > (entry.get("last_ok") or 0))
        if failed_since and status == OK:
            status = WARN
        if failed_since:
            why = ((why + "; ") if why else "") + "last refresh attempt failed"
        rows.append({
            "step": step, "label": label, "ts": ts, "status": status,
            "detail": detail or _pretty(step, entry.get("detail", "")),
            "failed_since": failed_since,
            "fail_detail": entry.get("fail_detail", ""),
            "why": why,
            # The failure text has been collected here since this function was
            # written and has never once been rendered -- the page said "last
            # attempt failed" and kept the actual message to itself.
            "why_detail": entry.get("fail_detail", ""),
            "task": task, "max_age": max_age,
        })
    return rows


# BOTH projects. The job that PRODUCES the weekly model artifact
# (NFLModelCaptureDaily, plus the NFLModelCapture_* one-shots it queues before
# each kickoff slot) lives in the NFL Model repo, so a Roboner-only query left
# the page able to show the artifact going stale but never why: every visible
# job read green while the one that actually failed was not on the list.
_PS_TASKS = (
    "Get-ScheduledTask | Where-Object { $_.TaskName -like 'Roboner*' "
    "-or $_.TaskName -like 'NFLModel*' } | ForEach-Object { "
    "$i = $_ | Get-ScheduledTaskInfo; [PSCustomObject]@{ "
    "name = $_.TaskName; state = [string]$_.State; "
    "last = $(if ($i.LastRunTime) { $i.LastRunTime.ToString('o') } else { '' }); "
    "result = $i.LastTaskResult; "
    "next = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('o') } else { '' }) } } "
    "| ConvertTo-Json -Compress -Depth 3"
)

# A one-time task that has never fired reports this sentinel pair. That is
# "not yet run", not a failure -- rendering it red would make every pre-draft
# task look broken right up until the moment it mattered.
NEVER_RUN_RESULT = 267011

# 0x41301, SCHED_S_TASK_RUNNING: the task is running RIGHT NOW and has no result
# yet. Not a failure, and not rare either -- the watchdog fires every fifteen
# minutes and the page is written by the watchdog, so sampling one mid-run is
# routine. Treating it as a non-zero exit painted RobonerWatchdog amber whenever
# the two lined up, which for as long as the dot had no explanation just looked
# like unexplained flakiness.
RUNNING_RESULT = 267009


def _iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def tasks() -> list:
    """Every scheduled job behind the bot, across both projects.

    The one-shot captures are transient by design -- registered before each of
    the day's kickoff slots and swept on the next daily run -- so most days this
    is the standing set and a Sunday carries three or four extras that then
    disappear. One that has been registered but has not fired yet reports the
    NEVER_RUN_RESULT sentinel and renders "not yet run", which is why that
    sentinel is not treated as a failure.
    """
    p = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_TASKS],
                       capture_output=True, timeout=90)
    raw = json.loads(p.stdout.decode("utf-8", "replace").strip() or "[]")
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for t in raw:
        last = _iso(t.get("last"))
        result = t.get("result")
        never = (result == NEVER_RUN_RESULT) or bool(last and last < 946684800)
        why, why_detail = "", ""
        if str(t.get("state", "")).lower() == "disabled":
            status = WARN
            why = "the task is disabled, so it will not run"
        elif never or result in (0, None, RUNNING_RESULT):
            status = OK
        else:
            status = WARN
            why = "its last run exited %s" % result
            why_detail = ("Windows reports exit code %s for the most recent run. "
                          "A non-zero code means the script itself failed, not "
                          "that the schedule is wrong." % result)
        out.append({
            "name": t.get("name"), "state": t.get("state"),
            "last": None if never else last,
            "result": result, "never_run": never,
            "next": _iso(t.get("next")), "status": status,
            "why": why, "why_detail": why_detail,
        })
    return sorted(out, key=lambda r: r["name"] or "")


def _jwt_expiry():
    """exp claim only, decoded locally. The token value never leaves this process."""
    tok = os.environ.get("SLEEPER_TOKEN")
    if not tok:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("SLEEPER_TOKEN="):
                tok = line.split("=", 1)[1].strip()
                break
    if not tok or tok.count(".") != 2:
        return None
    seg = tok.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    return claims.get("exp")


def sleeper() -> dict:
    from robo import sleeper_read as api
    out = {"rest": UNK, "latency_ms": None, "graphql": UNK,
           "token_exp": None, "token_days": None, "status": UNK}
    t0 = time.time()
    try:
        api.nfl_state()
        out["latency_ms"] = round((time.time() - t0) * 1000)
        out["rest"] = OK if out["latency_ms"] < 3000 else WARN
    except Exception as e:
        out["rest"] = BAD
        out["rest_error"] = _scrub(repr(e))
    try:
        from robo import sleeper_write
        out["graphql"] = OK if sleeper_write.whoami() else BAD
    except Exception as e:
        out["graphql"] = BAD
        out["graphql_error"] = _scrub(repr(e))
    exp = _safe(_jwt_expiry, 0)
    if isinstance(exp, (int, float)) and exp:
        out["token_exp"] = exp
        out["token_days"] = int((exp - time.time()) / 86400)
        if out["token_days"] < 14 and out["graphql"] == OK:
            out["graphql"] = WARN
    out["status"] = _worst(out["rest"], out["graphql"])
    bits = []
    if out["rest"] == BAD:
        bits.append("the REST API did not respond")
    elif out["rest"] == WARN:
        bits.append("the REST API took %sms to answer" % out["latency_ms"])
    if out["graphql"] == BAD:
        bits.append("the authenticated write API rejected us")
    elif out["graphql"] == WARN:
        bits.append("the write token expires in %d days" % (out["token_days"] or 0))
    out["why"] = "; ".join(bits)
    # rest_error and graphql_error have always been collected here and have
    # never reached the page: a dead Sleeper showed as a red dot and nothing else.
    out["why_detail"] = " ".join(
        x for x in (out.get("rest_error"), out.get("graphql_error")) if x)
    return out


def draft() -> dict:
    from robo import sleeper_read as api
    out = {"status": UNK}
    d = _safe(lambda: api.draft(DRAFT_ID_2026), {})
    if isinstance(d, dict) and d.get("status"):
        st = d.get("settings", {}) or {}
        out.update({
            "state": d.get("status"),
            "start_ts": ((d.get("start_time") or 0) / 1000) or None,
            "rounds": st.get("rounds"), "pick_timer": st.get("pick_timer"),
            "cpu_autopick": st.get("cpu_autopick"), "teams": st.get("teams"),
            "type": d.get("type"),
        })
    out["complete"] = out.get("state") == "complete"
    if out["complete"]:
        # Once the board is full none of what follows can change again for
        # eleven months: there is no agent to be alive, no stray practice
        # process to find, and no keeper board still being frozen. Collecting it
        # every fifteen minutes forever costs a process scan for an answer that
        # is now a historical fact.
        out["agent"] = {"for_this_draft": False}
        out["agents_running"] = out["agents_stray"] = 0
        out["keepers_frozen"] = 24
        out["status"] = OK
        return out

    hb = _read_json(DATA / "draft_heartbeat.json", {}) or {}
    # Only a heartbeat for THE draft counts. The guard applies the same test,
    # because a completed mock leaves a green-looking stamp behind.
    real = hb.get("draft_id") == DRAFT_ID_2026
    out["agent"] = {
        "for_this_draft": real,
        "ts": hb.get("ts") if real else None,
        "mock_ts": None if real else hb.get("ts"),
        "picks": hb.get("picks") if real else None,
        "next_pick": hb.get("next_pick") if real else None,
        "state": hb.get("status") if real else None,
    }
    # Draft agents currently running, split by which draft they target. Three
    # mock agents from a day of practice were still resident on 29 Aug, and the
    # guard kills every draft_agent process the moment it acts -- so a stray one
    # is not fatal, but it is the sort of thing you want to see BEFORE 3pm rather
    # than discover in the log afterwards. Counts only; no command lines.
    real = stray = 0
    try:
        import psutil
        me = os.getpid()
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            # Name AND argument shape, matching what the guard actually kills.
            # Substring-matching the command line alone counts the shell that
            # grepped for it, the nohup wrapper, and this process itself.
            if p.info["pid"] == me or "python" not in (p.info["name"] or "").lower():
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if "-m robo.draft_agent" not in cmd:
                continue
            if DRAFT_ID_2026 in cmd or "--draft-id" not in cmd:
                real += 1
            else:
                stray += 1
    except Exception:
        real = stray = -1
    out["agents_running"] = real
    out["agents_stray"] = stray

    keepers = _read_json(DATA / "keepers_2026.json", {}) or {}
    out["keepers_frozen"] = len(keepers.get("picks", []))
    out["status"] = OK if out.get("state") else UNK
    return out


def _next_waiver_run() -> float | None:
    """When Sleeper next processes waivers, in local time.

    Wednesday just after midnight, measured rather than assumed: 83 of this
    league's 93 completed 2025 claims landed on a Wednesday in local hours 0-1.
    The machine and the league both run on Phoenix time, so local arithmetic is
    the honest form here -- no zone conversion to get wrong.
    """
    try:
        now = datetime.now()
        # weekday(): Monday=0, so Wednesday=2.
        days = (2 - now.weekday()) % 7
        nxt = (now + timedelta(days=days)).replace(hour=0, minute=0, second=0,
                                                   microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=7)
        return nxt.timestamp()
    except Exception:
        return None


def inseason() -> dict:
    """Roster, lineup and move-engine state for the current week.

    Everything here is read-only and cheap: the weekly projections and the live
    roster. It deliberately does NOT run moves.py, which rebuilds the whole
    ranking board -- a status page that costs a board build every 15 minutes
    would be the most expensive thing in the project.
    """
    from robo import ir, lineup as lu, model_proj, season, value

    out: dict = {"status": UNK, "gated": not value.ready()}
    week = season.current_week()
    out["week"] = week
    sl = season.slots()
    out["slots"] = sl
    out["faab_left"] = season.faab_left()
    out["next_waiver"] = _next_waiver_run()
    out["drift"] = season.audit()
    out["ir_warnings"] = _safe(ir.warnings, []) or []

    res = lu.run(week=week, apply=False, verbose=False)
    out["projected"] = res["total"]
    # Which engine priced that number, and how much of the roster it reached.
    # A lineup quietly set on Sleeper's projection because the model artifact
    # went stale looks identical to one set on the model, and the difference is
    # the whole reason the model exists.
    out["proj_source"] = res.get("provenance") or ""
    out["proj_modelled"] = res.get("modelled", 0)
    out["proj_of_roster"] = res.get("of_roster", 0)
    out["proj_age_h"] = _safe(model_proj.age_hours, None)
    out["current_projected"] = res["current_total"]
    out["gain_available"] = res["gain"]
    out["illegal"] = res["illegal"]
    out["holes"] = res["holes"]
    out["lineup_set"] = bool(res["previous"]) and not res["changed"]

    # A hole or an illegal starter is a real problem: those are points we are
    # choosing not to score. Everything else is informational.
    if res["holes"] or res["illegal"]:
        out["status"] = BAD
    elif out["drift"] or out["ir_warnings"] or res["changed"]:
        out["status"] = WARN
    else:
        out["status"] = OK
    return out


def preflight(resp, ing, tsk, slp, drf, brain) -> list:
    """Is the engine ready. Each row is an assertion, not a description.

    Before the draft this asserts draft readiness; afterwards those rows stand
    down and the in-season jobs take their place. Asserting on a draft guard
    that correctly disabled itself painted the page red all season.
    """
    rows = []

    def add(label, ok, detail, warn=False):
        rows.append({"label": label,
                     "status": OK if ok else (WARN if warn else BAD),
                     "detail": _scrub(detail, 120)})

    add("Sleeper API reachable", slp.get("rest") == OK,
        ("%s ms round trip" % slp["latency_ms"]) if slp.get("latency_ms") else "no response",
        warn=slp.get("rest") == WARN)
    days = slp.get("token_days")
    add("Sleeper write access authenticated", slp.get("graphql") == OK,
        ("token valid for %d more days" % days) if days is not None else "token check failed",
        warn=slp.get("graphql") == WARN)

    done = drf.get("state") == "complete"

    by_step = {r["step"]: r for r in ing}
    sources = [("board", "Ranking board built"),
               ("buzz", "Market buzz current")]
    if not done:
        sources += [("adp-live", "Live ADP current"),
                    ("ecr", "Expert rankings current")]
    for step, label in sources:
        r = by_step.get(step, {})
        add(label, r.get("status") == OK,
            "%s / %s" % (r.get("detail", ""), _ago(r.get("ts"))),
            warn=r.get("status") == WARN)

    by_name = {t["name"]: t for t in tsk}

    # Everything from here down is about getting TO the draft. Once it reads
    # complete these stop being assertions and become false alarms: the guard is
    # SUPPOSED to be disabled -- it disables itself the moment the draft
    # finishes -- and the one-time pre-draft refreshes are supposed to have
    # fired and gone. Left in, they painted the whole page red all season for
    # doing exactly what they were built to do.
    if not done:
        add("Keeper board frozen", drf.get("keepers_frozen", 0) == 24,
            "%d of 24 keeper picks assigned" % drf.get("keepers_frozen", 0))

        stray = drf.get("agents_stray", 0)
        add("No stray practice agents", stray == 0,
            "clear" if stray == 0 else
            "%d agent%s still running against a mock draft" % (stray, "" if stray == 1 else "s"),
            warn=True)  # the guard clears these when it acts; worth seeing, not alarming

        guard = by_name.get("RobonerDraftGuard", {})
        start = drf.get("start_ts")
        guard_ok = bool(str(guard.get("state", "")).lower() == "ready"
                        and guard.get("next") and (not start or guard["next"] < start))
        add("Draft guard armed", guard_ok,
            "%s, next run %s" % (guard.get("state", "missing"), _clock(guard.get("next"))))

        for name in ("RobonerPreDraftRefresh", "RobonerPreDraftRefresh2"):
            t = by_name.get(name)
            add("%s scheduled" % name,
                bool(t and str(t.get("state", "")).lower() == "ready"),
                ("next run %s" % _clock((t or {}).get("next"))) if t else "task not found")

    # The jobs that are supposed to exist right now. RobonerScout is NOT among
    # them: it was a one-time pre-draft run and was unregistered with the rest
    # of the draft-day tasks, so asserting on it would be the same false alarm
    # as the guard. Run it by hand, or schedule it again when something in
    # season actually consumes its verdicts.
    # NFLModelCaptureDaily is in this list because the weekly lineup is decided
    # on what it produces: a disabled producer is a readiness failure, not just
    # a data row that will go amber in thirty hours. Its ONE-SHOTS are NOT
    # asserted -- they are supposed to be absent most days, and asserting them
    # would be the same false alarm as asserting on the draft guard that
    # correctly disabled itself.
    inseason_tasks = (["RobonerLineup", "RobonerRoster", "RobonerWaivers",
                       "NFLModelCaptureDaily"]
                      if done else ["RobonerScout"])
    for name in inseason_tasks:
        t = by_name.get(name)
        add("%s scheduled" % name,
            bool(t and str(t.get("state", "")).lower() == "ready"),
            ("next run %s" % _clock((t or {}).get("next"))) if t else "task not found")

    # Pass/fail only. No VRAM figure, no load, no model size.
    add("Local model available", bool(brain.get("present")),
        "reachable" if brain.get("reachable") else "not reachable")
    add("Chat responder running", bool(resp.get("alive")),
        "heartbeat %s" % _ago(resp.get("heartbeat_ts")))
    return rows


def snapshot() -> dict:
    resp = _safe(responder, {"status": UNK, "channels": []})
    hist = _safe(reply_history,
                 {"day": 0, "week": 0, "hourly": [0] * 24, "per_channel_day": {}})
    brain = _safe(llm, {"status": UNK, "present": False, "reachable": False})
    ing = _safe(ingests, [])
    tsk = _safe(tasks, [])
    slp = _safe(sleeper, {"status": UNK})
    drf = _safe(draft, {"status": UNK})
    pre = _safe(lambda: preflight(resp, ing, tsk, slp, drf, brain), [])
    ins = _safe(inseason, {"status": UNK})
    # Named components, so the banner can say WHICH one lost rather than
    # discarding that the moment _worst() collapses them to a colour.
    parts = ([("chat responder", resp.get("status", UNK)),
              ("Sleeper", slp.get("status", UNK)),
              ("this week", ins.get("status", UNK))]
             + [(r["label"], r["status"]) for r in ing]
             + [(r["label"], r["status"]) for r in pre])
    overall = _worst(*[st for _n, st in parts])
    cause = [n for n, st in parts if st == overall] if overall != OK else []
    return {
        "overall_why": cause,
        "generated": time.time(),
        "generated_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": overall,
        "responder": resp, "history": hist, "llm": brain,
        "ingests": ing, "tasks": tsk, "sleeper": slp,
        "draft": drf, "preflight": pre, "inseason": ins,
    }


# --------------------------------------------------------------------------
# rendering
#
# Same palette and card system as decisions.render() and devlog.render() so the
# three pages read as one site. The one addition is a set of semantic status
# tokens kept deliberately separate from --acc: "this is a link" and "this is
# healthy" must never be the same blue.

CSS = """
 :root { --bg:#0f1420; --card:#1a2233; --ink:#e8ecf5; --dim:#93a0b8; --acc:#5aa9ff;
         --line:#2a3550; --ok:#5ad48a; --warn:#f0a35a; --bad:#ff6f6f; --unk:#7b88a0; }
 * { box-sizing:border-box; }
 body { background:var(--bg); color:var(--ink); font:16px/1.6 system-ui,sans-serif;
        margin:0; padding:1rem; }
 main { max-width:880px; margin:0 auto; }
 h1 { color:var(--acc); margin-bottom:.2rem; font-size:1.6rem; }
 .sub { color:var(--dim); margin-top:0; font-size:.92rem; }
 a { color:var(--acc); }
 .num { font-variant-numeric:tabular-nums; }
 h3.sec { color:var(--dim); font-size:.8rem; letter-spacing:.08em; text-transform:uppercase;
          border-bottom:1px solid var(--line); padding-bottom:.4rem; margin:2rem 0 .8rem; }
 .card { background:var(--card); border-radius:10px; padding:.9rem 1.1rem; }
 .grid { display:grid; gap:.7rem; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); }
 .banner { display:flex; flex-wrap:wrap; align-items:center; gap:.6rem .9rem;
           background:var(--card); border-radius:10px; padding:.8rem 1.1rem;
           border-left:5px solid var(--unk); }
 .banner.s-ok { border-left-color:var(--ok); } .banner.s-warn { border-left-color:var(--warn); }
 .banner.s-bad { border-left-color:var(--bad); } .banner .gen { color:var(--dim); font-size:.85rem; }
 .pill { font-size:.7rem; letter-spacing:.09em; text-transform:uppercase; font-weight:700;
         padding:.2rem .6rem; border-radius:99px; background:#22304a; color:var(--unk); }
 .banner.s-ok .pill { background:#12351f; color:var(--ok); }
 .banner.s-warn .pill { background:#3a2412; color:var(--warn); }
 .banner.s-bad .pill { background:#3d1717; color:var(--bad); }
 .snapnote { color:var(--dim); font-size:.78rem; margin:.5rem 0 0; }
 .k { color:var(--dim); font-size:.72rem; letter-spacing:.06em; text-transform:uppercase; }
 .big { font-size:1.5rem; font-weight:600; line-height:1.2; }
 .row { display:flex; justify-content:space-between; gap:.8rem; padding:.28rem 0;
        border-bottom:1px solid rgba(255,255,255,.05); font-size:.88rem; }
 .row:last-child { border-bottom:0; } .row span:last-child { color:var(--dim); text-align:right; }
 .meter { height:7px; border-radius:99px; background:#22304a; overflow:hidden; margin:.45rem 0 .3rem; }
 .meter i { display:block; height:100%; background:var(--ok); }
 .meter.s-warn i { background:var(--warn); } .meter.s-bad i { background:var(--bad); }
 .dot { display:inline-block; width:.56rem; height:.56rem; border-radius:99px;
        background:var(--unk); margin-right:.45rem; vertical-align:baseline; flex:none; }
 .s-ok .dot,.dot.s-ok { background:var(--ok); } .s-warn .dot,.dot.s-warn { background:var(--warn); }
 .s-bad .dot,.dot.s-bad { background:var(--bad); }
 .scroll { overflow-x:auto; }
 table { border-collapse:collapse; width:100%; font-size:.86rem; }
 th { text-align:left; color:var(--dim); font-weight:500; font-size:.72rem;
      letter-spacing:.06em; text-transform:uppercase; padding:.35rem .6rem .35rem 0; }
 td { padding:.4rem .6rem .4rem 0; border-top:1px solid rgba(255,255,255,.06);
      vertical-align:top; font-variant-numeric:tabular-nums; }
 td.name { color:var(--ink); white-space:nowrap; } td.d { color:var(--dim); }
 /* Below this width a four-column table does not fit, and its last column --
    "next run", the one you most want on a phone -- ends up off-screen behind a
    sideways scroll nobody knows is there. Each row becomes its own block and
    every cell carries its own label instead. */
 @media (max-width:640px) {
   .scroll { overflow-x:visible; }
   table, tbody, tr, td { display:block; width:100%; }
   thead { position:absolute; left:-9999px; }
   tr { border-top:1px solid rgba(255,255,255,.09); padding:.55rem 0; }
   tr:first-child { border-top:0; }
   td { border-top:0; padding:.12rem 0; display:flex; justify-content:space-between;
        gap:.8rem; text-align:right; }
   td::before { content:attr(data-l); color:var(--dim); font-size:.72rem;
                letter-spacing:.06em; text-transform:uppercase; text-align:left;
                flex:none; padding-top:.15rem; }
   /* The row's own title is a heading, not a labelled field: no label, and it
      stays hard left instead of being pushed across by space-between. */
   td.name { white-space:normal; font-weight:600; justify-content:flex-start;
             text-align:left; gap:0; padding-bottom:.3rem; flex-wrap:wrap; }
   td.name::before { content:none; }
 }
 .checks li { list-style:none; display:flex; align-items:flex-start; gap:.1rem;
              padding:.34rem 0; border-bottom:1px solid rgba(255,255,255,.05); font-size:.88rem; }
 .checks { margin:0; padding:0; } .checks li:last-child { border-bottom:0; }
 .checks .what { flex:1; } .checks .why { color:var(--dim); font-size:.8rem; text-align:right; }
 /* The reason under a non-OK row. Coloured to match its dot, so the eye that
    found the amber dot lands on the amber sentence explaining it. */
 .why { white-space:normal; font-weight:400; font-size:.8rem; margin:.3rem 0 .1rem;
        display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; flex-basis:100%; }
 .w-warn { color:var(--warn); } .w-bad { color:var(--bad); } .w-unknown { color:var(--unk); }
 /* 2.5rem is 40px: the dot is .56rem and could never be an honest tap target,
    so the button is the thing you aim at. */
 .wbtn { appearance:none; -webkit-appearance:none; background:#22304a; color:var(--ink);
         border:1px solid var(--line); border-radius:6px; font:inherit; font-size:.72rem;
         letter-spacing:.06em; text-transform:uppercase; padding:.35rem .75rem;
         min-height:2.5rem; cursor:pointer; flex:none; }
 .wbtn:hover { border-color:var(--acc); } .wbtn:focus-visible { outline:2px solid var(--acc); }
 .modal { position:fixed; inset:0; background:rgba(6,10,18,.72); display:flex;
          align-items:center; justify-content:center; padding:1rem; z-index:50; }
 /* [hidden] loses to display:flex without this, so the sheet would ship open. */
 .modal[hidden] { display:none; }
 .sheet { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:1rem 1.1rem; max-width:34rem; width:100%; max-height:80vh; overflow-y:auto; }
 .mhead { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; }
 .mhead b { font-size:.95rem; }
 .mx { appearance:none; background:none; border:0; color:var(--dim); font-size:1.2rem;
       line-height:1; cursor:pointer; padding:.25rem .4rem; min-height:2.5rem; min-width:2.5rem; }
 .mx:hover { color:var(--ink); }
 #mbody { margin:.6rem 0 0; font-size:.86rem; color:var(--dim); overflow-wrap:anywhere; }
 .cause { color:var(--dim); font-size:.85rem; }
 .spark { display:flex; align-items:flex-end; gap:2px; height:34px; margin-top:.5rem; }
 .spark i { flex:1; background:var(--acc); opacity:.75; border-radius:1px; min-height:2px; }
 .foot { color:var(--dim); font-size:.8rem; margin:2.5rem 0 1rem; border-top:1px solid var(--line);
         padding-top:.9rem; }
"""

JS = """
(function () {
  var born = %d * 1000, warnAt = %d * 1000, badAt = %d * 1000;
  function fmt(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    if (s < 90) return s + ' seconds old';
    if (s < 5400) return Math.round(s / 60) + ' minutes old';
    if (s < 172800) return (s / 3600).toFixed(1) + ' hours old';
    return Math.round(s / 86400) + ' days old';
  }
  var banner = document.getElementById('banner');
  var base = banner.getAttribute('data-base');
  function tick() {
    var age = Date.now() - born;
    document.getElementById('age').textContent = fmt(age);
    // A static page cannot know it has gone stale, so it has to work it out.
    // Past 45 minutes the machine writing this has almost certainly stopped,
    // and everything below is fiction -- say so louder than the snapshot does.
    var sev = age > badAt ? 's-bad' : (age > warnAt ? 's-warn' : base);
    banner.className = 'banner ' + (sev === base ? base : sev);
    if (sev !== base) {
      document.getElementById('verdict').textContent =
        age > badAt ? 'snapshot stale' : 'snapshot ageing';
      // The cause describes what was WRONG when the snapshot was taken. Left
      // beside an overridden pill it reads as "SNAPSHOT STALE - chat responder",
      // blaming the responder for the staleness.
      var cz = document.getElementById('cause');
      if (cz) { cz.hidden = true; }
    }
    var cd = document.getElementById('countdown');
    if (cd) {
      var left = parseInt(cd.getAttribute('data-start'), 10) * 1000 - Date.now();
      if (left <= 0) { cd.textContent = 'under way'; }
      else {
        var d = Math.floor(left / 86400000), h = Math.floor(left / 3600000) %% 24,
            m = Math.floor(left / 60000) %% 60, s = Math.floor(left / 1000) %% 60;
        cd.textContent = (d ? d + 'd ' : '') + h + 'h ' + m + 'm ' + s + 's';
      }
    }
  }
  tick(); setInterval(tick, 1000);

  // One shared sheet rather than a popover per row: both tables collapse to
  // display:block under 640px, and an absolutely-positioned popover anchored
  // inside a <td> would have to be solved twice. A centred sheet is identical
  // in both layouts.
  var modal = document.getElementById('modal');
  var mtitle = document.getElementById('mtitle');
  var mbody = document.getElementById('mbody');
  var opener = null;
  function close() {
    modal.hidden = true;
    if (opener) { opener.focus(); opener = null; }
  }
  document.addEventListener('click', function (ev) {
    var t = ev.target;
    var b = t.closest ? t.closest('.wbtn') : null;
    if (b) {
      opener = b;
      mtitle.textContent = b.getAttribute('data-title') || 'Why';
      mbody.textContent = b.getAttribute('data-body') || '';
      modal.hidden = false;
      document.getElementById('mclose').focus();
      return;
    }
    // Backdrop or the close control only -- a click inside the sheet must not
    // dismiss it.
    if (t === modal || (t.closest && t.closest('.mx'))) { close(); }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !modal.hidden) { close(); }
  });
})();
"""

VERDICT = {OK: "operational", WARN: "degraded", BAD: "problem", UNK: "unknown"}


def _e(x) -> str:
    return html.escape(_scrub(x))


def _row(label, value) -> str:
    return '<div class="row"><span>%s</span><span>%s</span></div>' % (_e(label), _e(value))


def _why_html(item, title="") -> str:
    """The one-line reason under a non-OK row, and the control for the rest.

    NOTHING IS DRAWN FOR A HEALTHY ROW. A green page looks exactly as it did
    before this existed -- no affordance, nothing inviting a tap that would only
    answer "this is fine".

    The reason itself is always VISIBLE rather than hidden behind the gesture.
    This page is mostly read on a phone, where hover does not exist and the dot
    is a 9px target; making the reader find and hit it before the page will say
    what is wrong would be a worse page than the one that just says it. The
    button is only for the unabridged text -- an exception, an exit code -- which
    is too long to sit inline.
    """
    st = item.get("status")
    if st in (OK, None) or not (item.get("why") or item.get("why_detail")):
        return ""
    why = item.get("why") or "see the detail"
    detail = item.get("why_detail") or ""
    btn = ""
    if detail:
        btn = ('<button class="wbtn" type="button" data-title="%s" data-body="%s">'
               'why</button>' % (_e(title or item.get("label") or item.get("name") or ""),
                                 _e(detail)))
    return ('<div class="why w-%s"><span>%s</span>%s</div>'
            % (st, _e(why), btn))


def _card(title, body, status=None, why=None) -> str:
    dot = '<span class="dot s-%s"></span>' % status if status else ""
    reason = _why_html(why, title) if why else ""
    return ('<div class="card"><div class="k">%s%s</div>%s%s</div>'
            % (dot, _e(title), reason, body))


def _responder_html(resp, hist) -> str:
    up = resp.get("uptime")
    uptime = "not running"
    if up:
        uptime = ("%.1f hours" % (up / 3600)) if up >= 3600 else ("%d minutes" % (up / 60))
    head = ('<div class="big">%s</div>%s%s%s' % (
        "Listening" if resp.get("alive") else "Not responding",
        _row("uptime", uptime),
        _row("heartbeat", _ago(resp.get("heartbeat_ts"))),
        _row("replies", "%d today / %d this week" % (hist.get("day", 0), hist.get("week", 0)))))
    cards = [_card("responder", head, resp.get("status"), why=resp)]

    for c in resp.get("channels", []):
        pct = min(100, int(100 * c["hour"] / max(1, c["cap"])))
        cls = "s-bad" if c["status"] == BAD else ("s-warn" if pct >= 100 else "s-ok")
        body = ('<div class="big num">%d<span style="font-size:.9rem;color:var(--dim)">/%d</span></div>'
                '<div class="meter %s"><i style="width:%d%%"></i></div>'
                '%s%s%s' % (
                    c["hour"], c["cap"], cls, pct,
                    _row("this hour", "%d left before the cap" % c["headroom"]),
                    _row("last reply", _ago(c["last_reply"])),
                    _row("today", "%d" % hist.get("per_channel_day", {}).get(c["name"], 0))))
        label = {"groupme": "GroupMe", "sleeper": "Sleeper league chat",
                 "draft": "Sleeper draft room"}.get(c["name"], c["name"])
        cards.append(_card(label, body, c["status"], why=c))

    peak = max(hist.get("hourly") or [0]) or 1
    bars = "".join('<i style="height:%d%%" title="%d"></i>' % (max(6, int(100 * v / peak)), v)
                   for v in (hist.get("hourly") or [0] * 24))
    spark = _card("replies, last 24 hours",
                  '<div class="spark">%s</div>'
                  '<div class="row"><span>oldest</span><span>now</span></div>' % bars)
    return '<div class="grid">%s</div><div style="margin-top:.7rem">%s</div>' % (
        "".join(cards), spark)


def _proj_source(ins) -> str:
    """Which engine priced the lineup, and on a fallback, why.

    KEYED ON HOW MANY PLAYERS THE MODEL ACTUALLY PRICED, not on whether
    proj_source is set: that field carries the REASON when the model is not in
    use, so it is truthy either way and testing it rendered a missing artifact
    as "NFL Model, 0 of 17 players" -- the one reading this row exists to
    prevent.
    """
    n = ins.get("proj_modelled") or 0
    if not n:
        why = ins.get("proj_source") or "the model is not in use"
        return "Sleeper weekly projections - %s" % why
    age = ins.get("proj_age_h")
    return "NFL Model, %s of %s players%s" % (
        n, ins.get("proj_of_roster"),
        ", %.1fh old" % age if age is not None else "")


def _inseason_html(ins) -> str:
    """Roster, lineup and the move engine's gate.

    The gate line is stated plainly rather than hidden, because a status page
    whose whole job is "is this thing working" must not imply a capability that
    is deliberately switched off. A reader should be able to tell the difference
    between the bot deciding not to make a move and the bot being unable to.
    """
    if not ins or ins.get("status") == UNK:
        return _card("This week", _row("state", "could not be read"), UNK)

    sl = ins.get("slots") or {}
    lineup_body = [
        _row("week", ins.get("week")),
        _row("lineup", "set and optimal" if ins.get("lineup_set")
             else "a better legal lineup is available"),
        _row("projected", "%s pts (currently %s)"
             % (ins.get("projected"), ins.get("current_projected"))),
        # WHICH ENGINE, on every render. A lineup quietly set on Sleeper's
        # projection because the model artifact went stale looks identical to
        # one set on the model, and the difference is the reason the model
        # exists.
        #
        # The WORKING case is a sentence, not a log line: engine, how much of
        # the roster it priced, how old it is. The capture it was anchored on
        # is not repeated here -- decisions.record() puts it in the data blob
        # of every lineup the bot actually sets, which is published.
        #
        # The FALLBACK case keeps its reason, because a fallback with no cause
        # is precisely what this row exists to catch.
        _row("projection source", _scrub(_proj_source(ins))),
    ]
    if ins.get("holes"):
        lineup_body.append(_row("EMPTY SLOTS", ", ".join(ins["holes"])))
    if ins.get("illegal"):
        lineup_body.append(_row("should not be starting",
                                "; ".join(ins["illegal"])))
    lineup_status = (BAD if (ins.get("holes") or ins.get("illegal"))
                     else OK if ins.get("lineup_set") else WARN)
    # Summary only: the holes and illegal starters are already listed as rows
    # below, so there is nothing to open -- no why_detail, no button.
    lbits = []
    if ins.get("holes"):
        lbits.append("%d slot(s) with nobody eligible" % len(ins["holes"]))
    if ins.get("illegal"):
        lbits.append("%d starter(s) who should not be playing" % len(ins["illegal"]))
    if not lbits and lineup_status != OK:
        lbits.append("a better legal lineup is available than the one that is set")
    lineup_why = {"status": lineup_status, "why": "; ".join(lbits)}

    roster_body = [
        _row("active roster", "%s / %s (%s open)"
             % (sl.get("active"), sl.get("roster_max"), sl.get("open"))),
        _row("injured reserve", "%s / %s used"
             % (sl.get("ir_used"), sl.get("ir_slots"))),
        _row("FAAB left", ins.get("faab_left")),
        _row("next waiver run", _clock(ins.get("next_waiver"))),
        _row("roster moves",
             "held: no rest-of-season valuation yet" if ins.get("gated")
             else "live"),
    ]
    for w in ins.get("ir_warnings") or []:
        roster_body.append(_row("attention", w))
    for d in ins.get("drift") or []:
        roster_body.append(_row("league shape drift", d))
    roster_status = (WARN if (ins.get("ir_warnings") or ins.get("drift")) else OK)
    rbits = []
    if ins.get("ir_warnings"):
        rbits.append("%d reserve warning(s)" % len(ins["ir_warnings"]))
    if ins.get("drift"):
        rbits.append("%d league-shape disagreement(s)" % len(ins["drift"]))
    roster_why = {"status": roster_status, "why": "; ".join(rbits)}

    return ('<div class="grid">'
            + _card("Starting lineup", "".join(lineup_body), lineup_status,
                    why=lineup_why)
            + _card("Roster", "".join(roster_body), roster_status, why=roster_why)
            + '</div>')


def _draft_html(drf) -> str:
    start = drf.get("start_ts")
    if drf.get("complete"):
        # Historical. A countdown to a date in the past and a line reading
        # "draft agent: idle" are not status, they are a page that has not
        # noticed the season started.
        return _card("2026 draft", (
            _row("status", "complete")
            + _row("held", _clock(start))
            + _row("format", "%s, %s rounds, %s teams" % (
                drf.get("type", "?"), drf.get("rounds", "?"), drf.get("teams", "?")))
        ), OK)

    agent = drf.get("agent", {})
    if agent.get("for_this_draft"):
        agent_line = "running, %s (%s picks in)" % (_ago(agent.get("ts")), agent.get("picks"))
    elif agent.get("mock_ts"):
        agent_line = "idle since a practice run %s" % _ago(agent.get("mock_ts"))
    else:
        agent_line = "idle"
    body = ""
    if start:
        body += ('<div class="k">starts in</div><div class="big num" id="countdown" '
                 'data-start="%d">--</div>' % int(start))
    body += (_row("scheduled", _clock(start))
             + _row("status", drf.get("state", "unknown"))
             + _row("format", "%s, %s rounds, %s teams, %ss per pick" % (
                 drf.get("type", "?"), drf.get("rounds", "?"),
                 drf.get("teams", "?"), drf.get("pick_timer", "?")))
             + _row("draft agent", agent_line)
             + _row("keepers locked", "%d of 24 picks" % drf.get("keepers_frozen", 0)))
    return _card("2026 draft", body, drf.get("status"))


def _sleeper_html(slp) -> str:
    days = slp.get("token_days")
    body = (_row("read API", "%s ms" % slp.get("latency_ms") if slp.get("latency_ms")
                 else "no response")
            + _row("write access", "authenticated" if slp.get("graphql") == OK else "failing")
            + _row("credential", "valid %d more days" % days if days is not None else "unknown"))
    return _card("Sleeper connection", body, slp.get("status"), why=slp)


def _ingest_html(ing, tsk) -> str:
    nxt = {t["name"]: t.get("next") for t in tsk}
    rows = []
    for r in ing:
        note = r.get("detail", "")
        if r.get("failed_since"):
            note += " (last attempt failed; holding the previous copy)"
        rows.append(
            "<tr><td class='name'><span class='dot s-%s'></span>%s%s</td>"
            "<td data-l='last landed'>%s</td>"
            "<td class='d' data-l='we hold'>%s</td>"
            "<td class='d' data-l='next run'>%s</td></tr>"
            % (r["status"], _e(r["label"]), _why_html(r), _e(_ago(r["ts"])),
               _e(note), _e(_clock(nxt.get(r["task"])))))
    return ("<div class='scroll'><table><thead><tr><th>source</th><th>last landed</th>"
            "<th>what we hold</th><th>next run</th></tr></thead><tbody>%s</tbody></table></div>"
            % "".join(rows))


def _tasks_html(tsk) -> str:
    rows = []
    for t in tsk:
        last = "not yet run" if t.get("never_run") else _clock(t.get("last"))
        rows.append("<tr><td class='name'><span class='dot s-%s'></span>%s%s</td>"
                    "<td class='d' data-l='state'>%s</td>"
                    "<td class='d' data-l='last run'>%s</td>"
                    "<td data-l='next run'>%s</td></tr>"
                    % (t["status"], _e(t["name"]), _why_html(t, t.get("name")),
                       _e(t.get("state")), _e(last), _e(_clock(t.get("next")))))
    return ("<div class='scroll'><table><thead><tr><th>task</th><th>state</th>"
            "<th>last run</th><th>next run</th></tr></thead><tbody>%s</tbody>"
            "</table></div>" % "".join(rows))


def _preflight_html(pre) -> str:
    items = "".join(
        "<li><span class='dot s-%s'></span><span class='what'>%s</span>"
        "<span class='why'>%s</span></li>" % (r["status"], _e(r["label"]), _e(r["detail"]))
        for r in pre)
    return ("<ul class='checks'>%s</ul>"
            "<p class='snapnote'>Not checked here: whether a draft queue is armed. "
            "Sleeper offers no way to read one back, and a queue silently overrides "
            "every pick the bot tries to make, so this page says nothing rather than "
            "showing a green tick it cannot justify.</p>" % items)


def render(snap=None) -> str:
    s = snap or snapshot()
    resp, hist = s["responder"], s["history"]
    overall = "s-" + (s["overall"] if s["overall"] != UNK else "unk")
    page = (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta http-equiv="refresh" content="300">\n'
        '<title>Roboner Status — RURFFL</title>\n<style>' + CSS + '</style></head>\n'
        '<body><main>\n'
        '<h1>\U0001f4e1 Roboner Status</h1>\n'
        '<p class="sub">Whether the RURFFL AI owner is awake, what it has been told lately, '
        'and whether it is on top of this week. See also the '
        '<a href="index.html">decision log</a> and the <a href="changelog.html">dev log</a>.</p>\n'
        '<div class="banner ' + overall + '" id="banner" data-base="' + overall + '">'
        '<span class="pill" id="verdict">' + VERDICT[s["overall"]] + '</span>'
        + ('<span class="cause" id="cause">' + _e(", ".join(s["overall_why"]))
           + '</span>' if s.get("overall_why") else '') +
        '<span class="gen">snapshot <b id="age">just now</b>, taken '
        + html.escape(_clock(s["generated"])) + '</span></div>\n'
        '<p class="snapnote">This is a static snapshot, rewritten every 15 minutes '
        '(every 2 while a draft is live). If the age above turns amber or red, the '
        'machine that writes it has stopped and everything below is out of date. '
        'Every time on this page is ' + html.escape(_tz_label()) + '.</p>\n'

        '<h3 class="sec">Chat responder</h3>' + _responder_html(resp, hist) +
        '<h3 class="sec">This week</h3>' + _inseason_html(s.get("inseason") or {}) +
        '<h3 class="sec">Draft</h3><div class="grid">'
        + _draft_html(s["draft"]) + _sleeper_html(s["sleeper"]) + '</div>'
        + ('<h3 class="sec">Draft readiness</h3><div class="card">'
           if (s.get("draft") or {}).get("state") != "complete"
           else '<h3 class="sec">Engine readiness</h3><div class="card">')
        + _preflight_html(s["preflight"]) + '</div>'
        '<h3 class="sec">Data sources</h3><div class="card">'
        + _ingest_html(s["ingests"], s["tasks"]) + '</div>'
        '<h3 class="sec">Scheduled jobs</h3><div class="card">'
        + _tasks_html(s["tasks"]) + '</div>'
        '<p class="foot">Generated by the bot itself, on the machine it runs on. '
        'No account details, message contents, or roster plans appear on this page.</p>\n'
        '</main>\n'
        '<div class="modal" id="modal" hidden><div class="sheet" role="dialog" '
        'aria-modal="true" aria-labelledby="mtitle">'
        '<div class="mhead"><b id="mtitle"></b>'
        '<button class="mx" id="mclose" type="button" aria-label="Close">&#10005;</button>'
        '</div><p id="mbody"></p></div></div>\n'
        '<script>'
        + (JS % (int(s["generated"]), PAGE_WARN_AGE, PAGE_BAD_AGE))
        + '</script></body></html>\n')
    return page


# --------------------------------------------------------------------------
# publishing

def _signature(s) -> str:
    """What "nothing has changed" means.

    Deliberately ignores every clock and counter: without this the page differs
    on every single run and the decision-log repo collects ~96 commits a day of
    pure timestamp churn. Only a change in an actual verdict is worth a commit.
    """
    parts = [s["overall"], s["responder"].get("status"), str(s["responder"].get("alive")),
             s["sleeper"].get("rest"), s["sleeper"].get("graphql"),
             s["draft"].get("state"), str(s["draft"].get("agent", {}).get("for_this_draft"))]
    parts += ["%s=%s" % (c["name"], c["status"]) for c in s["responder"].get("channels", [])]
    parts += ["%s=%s" % (r["step"], r["status"]) for r in s["ingests"]]
    parts += ["%s=%s/%s" % (t["name"], t["state"], t["status"]) for t in s["tasks"]]
    parts += ["%s=%s" % (r["label"], r["status"]) for r in s["preflight"]]
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def write(snap=None) -> dict:
    s = snap or snapshot()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(s), encoding="utf-8")
    return s


def publish(snap=None) -> bool:
    """Write the page; push only when it would tell the league something new.

    Pushes on a changed verdict, on a live draft (where two minutes of staleness
    matters), or when the last push is old enough that the page's own age
    warning would otherwise fire on a perfectly healthy bot.
    """
    s = write(snap)
    state = _read_json(STATE, {}) or {}
    sig = _signature(s)
    since = time.time() - float(state.get("pushed_ts") or 0)
    drafting = s["draft"].get("state") == "drafting"
    if not (drafting or sig != state.get("signature") or since >= PUSH_MAX_INTERVAL):
        return False
    from robo import decisions
    ok = decisions.publish("status: %s" % VERDICT[s["overall"]])
    if ok:
        try:
            STATE.write_text(json.dumps({"signature": sig, "pushed_ts": time.time()}),
                             encoding="utf-8")
        except OSError:
            pass
    return ok


# --------------------------------------------------------------------------
# terminal report -- the ONLY unredacted output

def report(s) -> str:
    L = ["Roboner status  %s  (%s)" % (VERDICT[s["overall"]].upper(), _clock(s["generated"])), ""]
    r = s["responder"]
    L.append("RESPONDER  %s  pid=%s  up=%s  heartbeat %s (%s)" % (
        r.get("status"), r.get("pid"),
        ("%.1fh" % (r["uptime"] / 3600)) if r.get("uptime") else "-",
        _ago(r.get("heartbeat_ts")), r.get("heartbeat_status")))
    if r.get("note"):
        L.append("  note: %s" % r["note"][:300])
    for c in r.get("channels", []):
        L.append("  %-10s %2d/%d this hour  last %-12s failures %d/%d  [%s]" % (
            c["name"], c["hour"], c["cap"], _ago(c["last_reply"]),
            c["failures"], c["max_failures"], c["status"]))
    h = s["history"]
    L.append("  replies: %d today, %d this week  %s" % (h["day"], h["week"], h["per_channel_day"]))
    b = s["llm"]
    L.append("")
    L.append("LOCAL MODEL  %s  reachable=%s present=%s resident=%s vram=%s   [not published]" % (
        b.get("status"), b.get("reachable"), b.get("present"), b.get("resident"),
        ("%.0f%%" % (100 * b["vram_frac"])) if b.get("vram_frac") is not None else "-"))
    sl = s["sleeper"]
    L.append("SLEEPER      rest=%s (%sms) graphql=%s token=%s days" % (
        sl.get("rest"), sl.get("latency_ms"), sl.get("graphql"), sl.get("token_days")))
    d = s["draft"]
    L.append("DRAFT        %s  %s  agent=%s  keepers=%d/24" % (
        d.get("state"), _clock(d.get("start_ts")),
        "live" if d.get("agent", {}).get("for_this_draft") else "idle",
        d.get("keepers_frozen", 0)))
    L += ["", "DATA SOURCES"]
    for i in s["ingests"]:
        L.append("  %-6s %-22s %-12s %s%s" % (
            i["status"], i["label"], _ago(i["ts"]), i["detail"],
            "  <-- last attempt FAILED" if i["failed_since"] else ""))
    L += ["", "SCHEDULED JOBS"]
    for t in s["tasks"]:
        L.append("  %-6s %-26s %-9s last %-18s next %s" % (
            t["status"], t["name"], t["state"],
            "not yet run" if t["never_run"] else _clock(t["last"]), _clock(t["next"])))
    L += ["", "DRAFT READINESS"]
    for p in s["preflight"]:
        L.append("  [%-4s] %-40s %s" % (p["status"].upper()[:4], p["label"], p["detail"]))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Roboner status dashboard")
    ap.add_argument("--render", action="store_true", help="write decision-log/status.html")
    ap.add_argument("--publish", action="store_true", help="write, then push if anything changed")
    ap.add_argument("--json", action="store_true", help="machine-readable snapshot")
    args = ap.parse_args()
    s = snapshot()
    if args.json:
        print(json.dumps(s, indent=1, default=str))
        return
    if args.publish:
        pushed = publish(s)
        print("%s  %s  (%s)" % (OUT, VERDICT[s["overall"]],
                                "pushed" if pushed else "no push needed"))
        return
    if args.render:
        write(s)
        print("%s  %s" % (OUT, VERDICT[s["overall"]]))
        return
    print(report(s))


if __name__ == "__main__":
    main()
