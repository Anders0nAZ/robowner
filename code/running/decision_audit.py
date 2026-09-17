"""One reader over both decision-record stores.

WHY THIS EXISTS. The bot makes a roster decision two ways, and until now each
one had its own audit page named after the module that wrote it. But
`data/waiver_events/` (the scheduled pass) and `data/news_events/` (the
event-triggered pass) are the same decision reached by a different alarm
clock: both screen candidates, both price a drop, both build a waiver ladder,
and a claim in one is byte-identical in shape to a claim in the other -- the
same 26 keys, `bid_quote` and `opponent_field` included. Splitting them across
pages meant a reader had to already know which clock had fired before they
could go looking, which is exactly backwards.

WHAT IS NORMALIZED AND WHAT IS NOT. The container is normalized: when it ran,
what fired it, what it proposed, what it looked at, whether anything could
reach Sleeper. The VERDICTS are not. A scheduled run grades an option with one
of five strings from `moves._ordinary_audit_snapshot`; an event-triggered run
grades a candidate with a `stage` it died at plus an `outcome`. Those two
vocabularies mean different things and flattening them into one enum would
quietly invent a judgement neither module made. `status()` maps both onto a
coarse display bucket and `native` keeps the original words, so the evidence
table still shows what the code actually said.

READER ONLY, like its two parents. No Sleeper call, no rebuild, no decision.
A corrupt record is skipped rather than allowed to sink the page.
"""

from __future__ import annotations

import bisect

from robo import news_audit, waiver_audit

CLOCK, NEWS = "clock", "news"

# What a scheduled run was for. `moves.py` runs these as a lexicographic
# chain -- patch beats ros beats block -- so the mode IS the trigger story.
MODE_STORY = {
    "patch": "Scheduled: a starting slot could not be filled legally.",
    "ros": "Scheduled: the ordinary weekly hunt for a rest-of-season upgrade.",
    "block": "Scheduled: deny an opponent a player we do not otherwise want.",
    "ir_fill": "Scheduled: fill a spot the injured-reserve sweep just opened.",
}


def _slate_claims(slates, fallback_drop=None) -> list[dict]:
    """Flatten a ladder, carrying the slate's context onto each rung.

    A slate is a LADDER and the rung is half the decision, so the group and its
    capacity have to travel with the claim or the UI shows a bid with nothing
    saying what it was competing against.
    """
    out = []
    for slate in slates or []:
        for claim in slate.get("claims") or []:
            row = dict(claim)
            row.setdefault("drop", slate.get("drop") or fallback_drop)
            row["_group_id"] = slate.get("group_id")
            row["_group_kind"] = slate.get("kind")
            row["_group_capacity"] = slate.get("capacity")
            row["_group_exposure"] = slate.get("exposure")
            out.append(row)
    out.sort(key=lambda r: (r.get("submit_order") if r.get("submit_order") is not None
                            else r.get("priority") or r.get("seq") or 0))
    return out


def status(row: dict) -> tuple[str, str]:
    """(display bucket, the code's own words) for one considered player.

    Two vocabularies in, three buckets out. `native` is what gets shown beside
    it, because "below simulator noise" and "died at causal_delta" are both
    more informative than "rejected" and neither survives being merged.
    """
    if "verdict" in row:  # scheduled: moves._ordinary_audit_snapshot
        verdict = str(row.get("verdict") or "")
        if row.get("selected") or verdict == "selected":
            return "selected", verdict or "selected"
        if verdict.startswith("cleared"):
            return "cleared, not offered", verdict
        return "rejected", verdict or "rejected"
    native = str(row.get("stage") or "")
    outcome = str(row.get("outcome") or "rejected")
    native = f"{outcome} at {native}" if native else outcome
    return ("selected" if outcome == "proposed" else "rejected"), native


def _candidate_row(row: dict, channel: str, phase: str) -> dict:
    """One considered player, in whichever fields that run actually recorded.

    The two record types overlap only on identity. A scheduled option carries
    the simulator's margins; a news candidate carries the measured value change
    and its causal edge. Both are kept as-is and absent ones stay absent --
    a zero here would read as "we measured no change" when the truth is "that
    run never measured this."

    `phase` says WHICH screen this verdict came from. A news-triggered
    re-evaluation runs both: the causal screen decides whether the event gives
    us standing to act on a man at all, and the simulator decides whether the
    move is worth making. The same player legitimately appears in both with
    different verdicts, and collapsing them would hide the fact that two
    separate bars exist.
    """
    bucket, native = status(row)
    add = row.get("add") or {}
    drop = row.get("drop") or {}
    return {
        "player_id": str(row.get("player_id") or add.get("player_id") or ""),
        "name": row.get("name") or add.get("name"),
        "pos": row.get("pos") or add.get("pos"),
        "team": row.get("team") or add.get("team"),
        "drop_name": drop.get("name"),
        # ONE VOCABULARY ACROSS EVERY PAGE. "free now" is an outright add for
        # nothing; "on waivers" costs FAAB and waits for the weekly run. The
        # recorded `on_waivers` flag is preferred over the audit bucket the row
        # arrived in, because the flag is what the classifier actually said
        # about that player at decision time.
        "channel": ("on waivers" if row.get("on_waivers") else "free now"
                    if "on_waivers" in row else channel),
        "phase": phase,
        "status": bucket,
        "native": native,
        "reason": row.get("reason"),
        "gain": row.get("gain"),
        "se": row.get("se"),
        "ceiling": row.get("ceiling"),
        "starter": row.get("starter"),
        "noise_margin": row.get("noise_margin"),
        "policy_margin": row.get("policy_margin"),
        "coverage_priority": row.get("coverage_priority"),
        "delta_ros": row.get("delta_ros"),
        "pre_ros": row.get("pre_ros"),
        "post_ros": row.get("post_ros"),
        "causal_edge": row.get("causal_edge"),
        "raw": row,
    }


def _news(doc: dict) -> dict:
    action = doc.get("action") or {}
    claims = _slate_claims(action.get("claim_proposals"))
    free = list(action.get("free_proposals") or [])
    submitted = news_audit.submitted(doc)
    candidates = [_candidate_row(r, "free now", "causal screen") for r in
                  (action.get("candidate_checks") or action.get("rejections") or [])]
    return {
        "kind": NEWS,
        "at": doc.get("at"),
        "week": doc.get("week"),
        "fingerprint": doc.get("fingerprint"),
        "mode": "news",
        "path": doc.get("_path"),
        "report_path": doc.get("_report_path"),
        "trigger": list(doc.get("events") or []),
        "trigger_story": f"{len(doc.get('events') or [])} trigger player(s) on the news pulse.",
        "timing": doc.get("timing") or {},
        "provider_at_trigger": doc.get("provider_at_trigger") or {},
        "event_deltas": action.get("event_deltas") or {},
        "outcome": news_audit.outcome(doc),
        "free_moves": free,
        "claims": claims,
        "candidates": candidates,
        "drop_checks": list(action.get("drop_checks") or []),
        "control_checks": [],
        "thresholds": {},
        "roster_state": action.get("roster_state") or {},
        "faab_left": (action.get("roster_state") or {}).get("faab"),
        "hours_to_kickoff": (action.get("roster_state") or {}).get("hours_to_kickoff"),
        "gated": bool(action.get("free_gated") or action.get("claims_gated")),
        "free_gated": action.get("free_gated"),
        "claims_gated": action.get("claims_gated"),
        "blackout": None,
        "control_block": None,
        "sequence_basis": None,
        "submitted": submitted,
        "submission_recorded": True,
        "source_errors": list(doc.get("source_errors") or []),
        "provenance": {"capture": action.get("capture"), "export": action.get("export"),
                       "model": action.get("model")},
        "raw": doc,
    }


def _clock(doc: dict) -> dict:
    free_audit = doc.get("free_audit") or {}
    claims_audit = doc.get("claims_audit") or {}
    claims = _slate_claims(doc.get("plans"))
    free = list(doc.get("free_plans") or [])
    candidates = ([_candidate_row(r, "free now", "simulator") for r in
                   free_audit.get("options") or []]
                  + [_candidate_row(r, "on waivers", "simulator") for r in
                     claims_audit.get("options") or []])
    gated = bool(doc.get("gated"))
    roster = list(doc.get("roster") or [])
    # A scheduled record does not persist what was sent -- only whether the
    # gate was shut. A closed gate settles it; an open one does not, and
    # printing "0 submissions" for the open case would be a claim this record
    # cannot support.
    return {
        "kind": CLOCK,
        "at": doc.get("at"),
        "week": doc.get("week"),
        "fingerprint": doc.get("fingerprint"),
        "mode": doc.get("mode"),
        "path": doc.get("_path"),
        "report_path": None,
        "trigger": [],
        "trigger_story": MODE_STORY.get(str(doc.get("mode")),
                                        f"Scheduled run in `{doc.get('mode')}` mode."),
        "timing": {},
        "provider_at_trigger": {},
        "event_deltas": {},
        "outcome": _clock_outcome(doc, free, claims),
        "free_moves": free,
        "claims": claims,
        "candidates": candidates,
        "drop_checks": list(free_audit.get("drop_checks")
                            or claims_audit.get("drop_checks") or []),
        "control_checks": (list(free_audit.get("control_checks") or [])
                           + list(claims_audit.get("control_checks") or [])),
        "thresholds": claims_audit.get("thresholds") or free_audit.get("thresholds") or {},
        "roster_state": {"active": len(roster) or None, "faab": doc.get("faab_left"),
                         "hours_to_kickoff": doc.get("hours_to_kickoff")},
        "faab_left": doc.get("faab_left"),
        "hours_to_kickoff": doc.get("hours_to_kickoff"),
        "gated": gated,
        "free_gated": gated,
        "claims_gated": gated,
        "blackout": doc.get("blackout"),
        "control_block": doc.get("control_block"),
        "sequence_basis": doc.get("sequence_basis"),
        "submitted": [],
        "submission_recorded": gated,
        "source_errors": [],
        "provenance": {},
        "worst_case_faab": doc.get("worst_case_faab"),
        "valuation_computed": doc.get("valuation_computed"),
        "raw": doc,
    }


def _clock_outcome(doc: dict, free: list, claims: list) -> str:
    if doc.get("blackout"):
        return "Held: too close to kickoff"
    if doc.get("control_block"):
        return "Held: roster control block"
    if not free and not claims:
        return "No move cleared"
    if doc.get("gated"):
        return "Proposal only"
    return "Proposed, submission not recorded"


def normalize(doc: dict, kind: str) -> dict:
    return _news(doc) if kind == NEWS else _clock(doc)


# ONE DECISION, TWO FILES. A news pulse that carries an event rebuilds the
# whole waiver portfolio, and that rebuild goes through moves.run(channel=
# "claims", mode="ros") -- which writes its own waiver_events record. So a
# single re-evaluation lands in BOTH stores, about a tenth of a second apart:
# the news file holds the trigger, the room expansion and the causal screen,
# and the waiver file holds the simulator's near-miss ledger, the construction
# checks and the canonical slate. Measured on the live corpus, 38 of 39
# waiver records pair this way. Listing them separately would report twice as
# many decisions as the bot actually made and split each one's evidence in
# half -- the exact failure this module exists to fix.
PAIR_WINDOW_S = 30.0


def _claim_ids(run: dict) -> tuple:
    return tuple(sorted(str((c.get("add") or {}).get("player_id")) for c in run.get("claims") or []))


def _pair(news_rows: list[dict], clock_rows: list[dict]) -> None:
    """Fold each waiver record into the pulse that caused it, in place.

    Time alone would be enough at a 20-minute cadence, but the claim set and
    the valuation vintage are the real identity and are checked too: a pairing
    this module got wrong would silently attribute one run's near-misses to
    another run's trigger.
    """
    by_time = sorted(news_rows, key=lambda r: float(r.get("at") or 0))
    stamps = [float(r.get("at") or 0) for r in by_time]
    consumed = set()
    for clock in clock_rows:
        t = float(clock.get("at") or 0)
        i = bisect.bisect_left(stamps, t)
        best = None
        for cand in by_time[max(0, i - 2):i + 2]:
            if cand["fingerprint"] in consumed or abs(float(cand["at"]) - t) > PAIR_WINDOW_S:
                continue
            if cand.get("week") != clock.get("week"):
                continue
            ours, theirs = _claim_ids(cand), _claim_ids(clock)
            if ours and theirs and ours != theirs:
                continue
            best = cand
            break
        if best is None:
            continue
        consumed.add(best["fingerprint"])
        _absorb(best, clock)


def _absorb(news: dict, clock: dict) -> None:
    """Move the waiver record's evidence onto the pulse that caused it."""
    news["candidates"] = list(news["candidates"]) + list(clock["candidates"])
    news["control_checks"] = clock["control_checks"]
    news["thresholds"] = clock["thresholds"]
    news["drop_checks"] = news["drop_checks"] or clock["drop_checks"]
    news["claims"] = news["claims"] or clock["claims"]
    news["worst_case_faab"] = clock.get("worst_case_faab")
    news["valuation_computed"] = clock.get("valuation_computed")
    news["sequence_basis"] = clock.get("sequence_basis")
    news["blackout"] = clock.get("blackout")
    news["control_block"] = clock.get("control_block")
    news["paired_path"] = clock.get("path")
    news["paired_fingerprint"] = clock.get("fingerprint")
    news["paired_raw"] = clock.get("raw")
    news["faab_left"] = news.get("faab_left") or clock.get("faab_left")


def events(limit: int | None = 100, kinds=(CLOCK, NEWS)) -> list[dict]:
    """Every recorded decision run, both stores merged, newest first."""
    news_rows = [_news(d) for d in news_audit.events(limit=None)]
    clock_rows = [_clock(d) for d in waiver_audit.events(limit=10_000)]
    _pair(news_rows, clock_rows)
    absorbed = {r["paired_fingerprint"] for r in news_rows if r.get("paired_fingerprint")}
    out = []
    if NEWS in kinds:
        out += news_rows
    if CLOCK in kinds:
        # A waiver record that paired is no longer a run of its own; its
        # evidence now lives on the pulse that caused it.
        out += [c for c in clock_rows if c["fingerprint"] not in absorbed]
    out.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
    return out if limit is None else out[:limit]


def latest(kinds=(CLOCK, NEWS)) -> dict | None:
    """The most recent run of any kind -- what the Now page reports."""
    rows = events(limit=1, kinds=kinds)
    return rows[0] if rows else None


def find(fingerprint: str) -> dict | None:
    for row in events(limit=None):
        if row.get("fingerprint") == fingerprint:
            return row
    return None


def slate(run: dict) -> list[dict]:
    """Every move one run would make, free moves first then the ladder in order."""
    rows = []
    for p in run.get("free_moves") or []:
        add, drop = p.get("add") or {}, p.get("drop") or {}
        # The channel is WHERE he comes from, not whether a drop is paired
        # with him -- the drop column already says that, and "add / drop"
        # answered a different question in the same cell.
        rows.append({"channel": "free now",
                     "add": add.get("name"), "add_id": add.get("player_id"),
                     "drop": drop.get("name") or "(open roster spot)",
                     "drop_id": drop.get("player_id"),
                     "rung": None, "bid": None,
                     "gain": p.get("gain"), "ceiling": p.get("ceiling"),
                     "why": p.get("why"), "raw": p})
    for c in run.get("claims") or []:
        add, drop = c.get("add") or {}, c.get("drop") or {}
        rows.append({"channel": "waiver claim", "add": add.get("name"),
                     "add_id": add.get("player_id"),
                     "drop": drop.get("name") or "(open roster spot)",
                     "drop_id": drop.get("player_id"),
                     "rung": c.get("priority", c.get("seq")), "bid": c.get("bid"),
                     "gain": c.get("gain"), "ceiling": c.get("ceiling"),
                     "why": c.get("why"), "raw": c})
    return rows


def _main() -> None:
    """Coverage assertion: every record on disk must land in exactly one run."""
    rows = events(limit=None)
    clock = sum(r["kind"] == CLOCK for r in rows)
    news = sum(r["kind"] == NEWS for r in rows)
    paired = sum(bool(r.get("paired_fingerprint")) for r in rows)
    on_disk = len(news_audit.events(limit=None)) + len(waiver_audit.events(limit=10_000))
    accounted = len(rows) + paired
    print(f"{len(rows)} decision runs  ({news} news-triggered, {clock} scheduled; "
          f"{paired} carry a paired waiver record)")
    print(f"records on disk {on_disk} · accounted for {accounted} · "
          f"{'OK' if accounted == on_disk else 'MISMATCH — records dropped'}")
    print()
    print(f"{'when':<24}{'trigger':<9}{'mode':<8}{'cand':>5}{'moves':>6}  outcome")
    for r in rows[:30]:
        print(f"{news_audit.local_time(r['at']):<24}{r['kind']:<9}"
              f"{str(r['mode'] or '-'):<8}{len(r['candidates']):>5}"
              f"{len(slate(r)):>6}  {r['outcome']}")


if __name__ == "__main__":
    _main()
