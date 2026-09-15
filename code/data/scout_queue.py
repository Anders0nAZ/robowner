"""One durable, rate-limited queue for every automated prose scout run.

THE QUEUE HOLDS IDS, NOT BUNDLES. A bundle costs about a second to build --
almost all of it one Sleeper news read per player -- so the news pulse cannot
afford to build two hundred of them to decide that four are worth a model. It
enqueues the ids it noticed and the corpus is gathered four at a time inside
drain(), which also means a batch always judges the reporting as it stands when
the model actually reads it rather than as it stood when somebody queued it.

ONE QUEUE, THREE PRODUCERS. The ten-minute news pulse, the decision cascade and
the daily refresh all defer their prose work here. They had two separate queues
for a day: the pulse kept its own pending list in news_watch.json with no retry
budget and no way to say a player had failed three times, while the cascade and
refresh used this one. Two queues meant two rates against the same single-GPU
model and two answers to "what is still waiting".

THE MODEL IS NEVER TIMING AUTHORITY. Everything drained here is marked advisory
before it is written: deterministic timing enters through scout.merge_timing(),
which reads dates out of structured facts, not out of a model's reading.
"""

import json
import re
import time
from pathlib import Path

from robo import DATA

QUEUE = DATA / "scout_queue.json"
SCHEMA = 2
BATCH_SIZE = 4
MIN_BATCH_INTERVAL = 10 * 60
RETRY_DELAYS = (10 * 60, 30 * 60, 60 * 60)
MAX_ATTEMPTS = 3
PRIORITY = {"monday_starter": 0, "emergency": 1,
            "waiver_candidate": 2, "background": 3}

# EACH STEM OWNS ITS OWN BOUNDARY. A single `\w*` applied to the whole alternation
# let the short stems swallow ordinary recap prose: `out` matched "outgaining",
# "outside" and "outlook", `sign` matched "significant", `cut` matched "cutback".
# One hit anywhere unfilters the entire bundle, so six of seven realistic
# box-score paragraphs were reaching the model -- and the filter reported itself
# as working, because `filtered_recaps` only counts what it caught. Long
# unambiguous stems still take a suffix wildcard; the short ones are exact.
_SIGNAL = re.compile(
    r"\b(?:"
    r"injur\w*|surger\w*|"                       # injury/injured, surgery/surgeries
    r"ir|pup|inactive|activat\w*|"
    r"questionable|doubtful|limited|practic\w*|"  # incl. practicing, did not practice
    r"starter(?:s)?|starting|"
    r"role(?:s)?|depth|"
    r"promot\w*|demot\w*|waiv\w*|releas\w*|"
    r"sign(?:ed|ing|s)?|trade(?:d|s)?|"
    r"suspend\w*|suspension|arrest\w*|"
    r"cut(?:s)?|out|"
    # `return` is the one stem that is both a signal and a stat. A return DATE is
    # this module's whole purpose; return YARDS are a box score, and a "returner"
    # is a job on the kick team. So no bare wildcard -- that is what let
    # "returner" through -- and never when yardage follows.
    r"return(?:s|ing|ed)?(?!\s+(?:yard|yds))"
    r")\b", re.I)
_RECAP = re.compile(
    r"\b(finished|completed|rushed|carried|caught|targeted|passed|threw|yards?|"
    r"touchdowns?|fantasy points?|box score|week \d+ (?:win|loss))\b", re.I)


def _read() -> dict:
    try:
        doc = json.loads(QUEUE.read_text(encoding="utf-8"))
        if isinstance(doc.get("items"), dict):
            # Schema 1 stored the whole bundle on every item. They are re-built
            # at drain time now, so an old file simply sheds them.
            for item in doc["items"].values():
                item.pop("bundle", None)
            doc["schema"] = SCHEMA
            return doc
    except Exception:
        pass
    return {"schema": SCHEMA, "last_batch_at": 0.0, "items": {},
            "completed": [], "filtered_recaps": 0}


def _write(doc: dict) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(QUEUE)


def pure_box_score_recap(bundle: dict) -> bool:
    """Exclude prose whose only new information is the completed stat line."""
    if (bundle.get("designation") or bundle.get("eligible_week") is not None
            or bundle.get("injury") or bundle.get("out_for_season")):
        return False
    texts = []
    for item in bundle.get("news") or []:
        text = " ".join(str(item.get(k) or "")
                        for k in ("title", "description", "analysis"))
        if text.strip():
            texts.append(text)
    return bool(texts) and all(_RECAP.search(text) and not _SIGNAL.search(text)
                               for text in texts)


def _merge_news(*groups) -> list:
    """Union of news items, arrival order kept, duplicates dropped."""
    seen, out = set(), []
    for group in groups:
        for item in group or []:
            key = json.dumps(item, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


def _place(doc: dict, pid: str, name: str, cat: str, fingerprint: str | None,
           extra_news: list | None, now: float) -> str:
    """Add or update one item. Returns what happened, for the caller's counts."""
    priority = PRIORITY.get(cat, PRIORITY["background"])
    old = (doc["items"] or {}).get(pid)
    if old and old.get("disposition") == "pending":
        # ALREADY WAITING. New content supersedes the old marker and raises its
        # priority, but it does NOT create a second item or send this one back
        # to the end of the line: the corpus is gathered fresh at drain time, so
        # re-queuing cannot make the reading any newer, and resetting `attempts`
        # every ten minutes would mean a player who fails forever never reaches
        # the attention list.
        changed = bool(fingerprint) and fingerprint != old.get("fingerprint")
        if priority < int(old.get("priority", 99)):
            old["priority"], old["category"] = priority, cat
        if fingerprint:
            old["fingerprint"] = fingerprint
        if extra_news:
            old["extra_news"] = _merge_news(old.get("extra_news"), extra_news)
        old["updated_at"] = now
        return "updated" if changed else "unchanged"
    if old and old.get("fingerprint") and old.get("fingerprint") == fingerprint:
        # Superseded work that has not changed. Retry state is preserved so a
        # player who has failed twice does not get a fresh budget every poll.
        old["updated_at"] = now
        return "unchanged"
    ledger = {}
    if old is not None:
        # NEW CONTENT REPLACES THE WORK, NEVER THE RETRY LEDGER. The only item
        # that reaches here is one already marked for attention -- a pending one
        # is superseded above, an unchanged one returns above it -- and rebuilding
        # it from scratch handed a player who had failed three times a fresh
        # budget the moment any new story landed on him. That cleared the stalled
        # row off the status page, dropped the roster card back to green and put
        # him back in front of the model, with nobody ever having seen the
        # failure. It is the same trap the pending branch is written to avoid, and
        # the men who trip it are the ones failing BECAUSE their corpus is large,
        # so their reporting moves constantly. `--reset` is the way back, and it
        # is meant to be the only one.
        ledger = {key: old[key] for key
                  in ("attempts", "last_error", "next_attempt_at",
                      "disposition", "enqueued_at") if key in old}
        # A background story must not demote work that is waiting as a starter.
        if int(old.get("priority", 99)) < priority:
            priority, cat = int(old["priority"]), old.get("category") or cat
        extra_news = _merge_news(old.get("extra_news"), extra_news)
    doc["items"][pid] = {
        "player_id": pid, "name": name or pid,
        "category": cat, "priority": priority, "fingerprint": fingerprint,
        "extra_news": list(extra_news or []),
        "enqueued_at": now, "updated_at": now,
        "attempts": 0, "last_error": None, "next_attempt_at": now,
        "disposition": "pending",
        **ledger,
    }
    return "updated" if old is not None else "added"


def enqueue(bundles: list[dict], category: str = "background",
            categories: dict[str, str] | None = None,
            now: float | None = None) -> dict:
    """Queue work whose corpus the caller has already gathered.

    The bundle is used for its fingerprint and to drop a pure box-score recap
    before it costs a model call; it is not stored. See the module docstring.
    """
    from robo import scout
    now = time.time() if now is None else float(now)
    doc = _read()
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    filtered = 0
    for bundle in bundles:
        pid = str(bundle["player_id"])
        if pure_box_score_recap(bundle):
            filtered += 1
            continue
        cat = (categories or {}).get(pid, category)
        counts[_place(doc, pid, bundle.get("name"), cat,
                      scout.fingerprint(bundle), None, now)] += 1
    doc["filtered_recaps"] = int(doc.get("filtered_recaps") or 0) + filtered
    _write(doc)
    return {**counts, "filtered_recaps": filtered, "queued": pending_count(doc)}


def enqueue_ids(ids, category: str = "background",
                categories: dict[str, str] | None = None,
                names: dict[str, str] | None = None,
                extra_news: dict[str, list] | None = None,
                now: float | None = None) -> dict:
    """Queue work by player id, without paying to gather its corpus.

    This is the news pulse's door. A Monday feed can move two hundred rows at
    once and every one of them would cost a Sleeper read to fingerprint; the
    four that reach a model get gathered in drain() instead. An item queued this
    way carries no fingerprint, so drain() asks needs_judging() whether the
    reporting has actually moved once it has the corpus in hand.
    """
    now = time.time() if now is None else float(now)
    doc = _read()
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    for pid in ids:
        pid = str(pid)
        cat = (categories or {}).get(pid, category)
        counts[_place(doc, pid, (names or {}).get(pid), cat, None,
                      (extra_news or {}).get(pid), now)] += 1
    _write(doc)
    return {**counts, "filtered_recaps": 0, "queued": pending_count(doc)}


def pending_count(doc: dict | None = None) -> int:
    doc = doc or _read()
    return sum(1 for item in (doc.get("items") or {}).values()
               if item.get("disposition") == "pending")


def _retire(doc: dict, item: dict, outcome: str, now: float) -> None:
    pid = item["player_id"]
    doc.setdefault("completed", []).append({
        "player_id": pid, "name": item.get("name"), "outcome": outcome,
        "completed_at": now, "category": item.get("category")})
    doc["items"].pop(pid, None)


def drain(now: float | None = None, timeout: int = 120,
          verbose: bool = False) -> dict:
    """Gather, judge and checkpoint at most one batch.

    The rate limit counts MODEL calls, not calls to this function: a batch that
    turns out to need no judging -- everyone in it has dropped out of the
    decision pool, or nobody's reporting has moved since his last verdict -- has
    not spent the resource the limit protects, and making it wait ten minutes
    would stall a queue for work that costs nothing.
    """
    from robo import scout
    now = time.time() if now is None else float(now)
    doc = _read()
    since = now - float(doc.get("last_batch_at") or 0)
    if since < MIN_BATCH_INTERVAL:
        return {"status": "rate_limited", "queued": pending_count(doc),
                "completed": [], "attention": [],
                "retry_in": round(MIN_BATCH_INTERVAL - since, 1)}
    due = [item for item in (doc.get("items") or {}).values()
           if item.get("disposition") == "pending"
           and float(item.get("next_attempt_at") or 0) <= now]
    due.sort(key=lambda item: (int(item.get("priority", 99)),
                               float(item.get("enqueued_at") or 0),
                               item["player_id"]))
    selected = due[:BATCH_SIZE]
    if not selected:
        return {"status": "idle", "queued": pending_count(doc),
                "completed": [], "attention": []}

    by_item = {item["player_id"]: item for item in selected}
    try:
        gathered = scout.gather(only=list(by_item))
    except Exception as e:
        for item in selected:
            _defer(item, f"{type(e).__name__}: {str(e)[:120]}", now)
        _write(doc)
        return {"status": "gather_failed", "queued": pending_count(doc),
                "completed": [], "attention": [],
                "error": f"{type(e).__name__}: {str(e)[:120]}"}

    bundles, recaps = [], []
    for b in gathered:
        pid = str(b["player_id"])
        item = by_item.get(pid)
        if item and item.get("extra_news"):
            b["news"] = list(b.get("news") or []) + list(item["extra_news"])
        if item and b.get("name") and item.get("name") in (None, "", pid):
            # A producer that queued by id alone may not have had a name. The
            # corpus does, and an attention list of bare player ids is a status
            # page nobody can act on.
            item["name"] = b["name"]
        if pure_box_score_recap(b):
            recaps.append(pid)
            continue
        bundles.append(b)
    found = {str(b["player_id"]) for b in gathered}
    # OUT OF THE POOL IS NOT A FAILURE. scout.gather() can only narrow to the
    # decision pool, so a man who has left it has no corpus to read and no
    # verdict to form; retrying him three times would fill the attention list
    # with players nobody can act on.
    dropped = [pid for pid in by_item if pid not in found]

    todo, reuse = scout.needs_judging(bundles)
    unchanged = [str(b["player_id"]) for b in bundles
                 if str(b["player_id"]) not in {str(x["player_id"]) for x in todo}]
    verdicts = []
    if todo:
        try:
            verdicts = scout.judge(todo, verbose=verbose, timeout=timeout)
        except Exception as e:
            # A dead model is the common case here -- Ollama restarting, or a
            # batch past its timeout. Back the whole batch off on the retry
            # ladder rather than letting the exception out, where the caller
            # would record an error and then ask again in ten minutes forever.
            why = f"{type(e).__name__}: {str(e)[:120]}"
            stalled = []
            for b in todo:
                item = (doc.get("items") or {}).get(str(b["player_id"]))
                if item and _defer(item, why, now):
                    stalled.append(str(b["player_id"]))
            doc["last_batch_at"] = now
            _write(doc)
            return {"status": "judge_failed", "error": why, "completed": [],
                    "attention": stalled, "queued": pending_count(doc)}
        for verdict in verdicts:
            # Advisory, always. A model reading prose may summarise a date for a
            # human, but only scout.merge_timing() -- which reads structured
            # facts -- may make one executable.
            if verdict.get("return_week") is not None:
                verdict["advisory_return_week"] = verdict.get("return_week")
            verdict["timing_actionable"] = False
        scout.write_verdicts(verdicts, scout.LOCAL_MODEL, bundles=todo)
    judged = {str(v.get("player_id")) for v in verdicts if v.get("player_id")}

    completed, attention = [], []
    for pid in by_item:
        item = (doc.get("items") or {}).get(pid)
        if item is None:
            continue
        if pid in dropped:
            _retire(doc, item, "left the decision pool", now)
            continue
        if pid in recaps:
            doc["filtered_recaps"] = int(doc.get("filtered_recaps") or 0) + 1
            _retire(doc, item, "box-score recap only", now)
            continue
        if pid in unchanged:
            _retire(doc, item, "reporting unchanged since the last verdict", now)
            completed.append(pid)
            continue
        if pid in judged:
            _retire(doc, item, "judged", now)
            completed.append(pid)
            continue
        if _defer(item, "model returned no verdict for this player", now):
            attention.append(pid)

    doc["completed"] = (doc.get("completed") or [])[-100:]
    if todo:
        doc["last_batch_at"] = now
    doc["last_batch_size"] = len(selected)
    _write(doc)
    return {"status": "processed", "attempted": len(selected),
            "judged": sorted(judged), "completed": completed,
            "unchanged": unchanged, "dropped": dropped, "recaps": recaps,
            "attention": attention, "verdicts": verdicts,
            "queued": pending_count(doc)}


def _defer(item: dict, error: str, now: float) -> bool:
    """Record a failed attempt. Returns True once it needs a human."""
    attempts = int(item.get("attempts") or 0) + 1
    item["attempts"] = attempts
    item["last_error"] = error
    item["next_attempt_at"] = now + RETRY_DELAYS[min(attempts - 1,
                                                     len(RETRY_DELAYS) - 1)]
    if attempts >= MAX_ATTEMPTS:
        item["disposition"] = "attention"
        return True
    return False


def status() -> dict:
    doc = _read()
    attention = [item for item in (doc.get("items") or {}).values()
                 if item.get("disposition") == "attention"]
    return {"queued": pending_count(doc), "attention": len(attention),
            "attention_players": [x.get("name") or x["player_id"] for x in attention],
            "attention_detail": [{"name": x.get("name") or x["player_id"],
                                  "attempts": x.get("attempts"),
                                  "why": x.get("last_error")}
                                 for x in attention],
            "last_batch_at": float(doc.get("last_batch_at") or 0),
            "last_batch_size": int(doc.get("last_batch_size") or 0),
            "filtered_recaps": int(doc.get("filtered_recaps") or 0)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--drain", action="store_true", help="run one batch now")
    ap.add_argument("--retry", action="store_true",
                    help="return everything in the attention list to the queue")
    args = ap.parse_args()
    if args.retry:
        doc = _read()
        n = 0
        for item in (doc.get("items") or {}).values():
            if item.get("disposition") == "attention":
                item.update(disposition="pending", attempts=0,
                            next_attempt_at=time.time(), last_error=None)
                n += 1
        _write(doc)
        print(f"{n} item(s) returned to the queue")
    if args.drain:
        print(drain(verbose=True))
    s = status()
    print(f"{s['queued']} pending, {s['attention']} needing attention, "
          f"{s['filtered_recaps']} recap(s) excluded to date")
    for row in s["attention_detail"]:
        print(f"  {row['name']}: {row['attempts']} attempt(s), {row['why']}")


if __name__ == "__main__":
    main()
