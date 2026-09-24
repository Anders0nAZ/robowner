"""Deterministic plain English for a decision record.

WHY THIS IS NOT A MODEL. Every sentence here is assembled from fields the
deciding code already wrote down. Nothing is summarised, inferred or softened,
because a narration that can disagree with the record is worse than no
narration -- it would be the most readable thing on the page and the only part
nobody can check. Where the deciding module already emitted a sentence
(`faab.quote()["reason"]`, `moves._option_row()["why"]`) that sentence is
quoted, not paraphrased.

WHAT IT ADDS. The records are written in the vocabulary of the module that
made the call: "below simulator noise", "rejected at causal_delta", "cleared;
outranked by the coverage floor". Those are exact and they are opaque unless
you have read the module. This turns each one into a sentence that says what
bar was applied and what the number actually was.

LOCAL ONLY. Some of what this renders quotes injury reporting. It belongs to
the audit app, which is unredacted by design; nothing here may be handed to
`decisions.publish()` or the public status page.
"""

from __future__ import annotations

from robo import news_audit

# What each screen is FOR. A news-triggered run applies two different bars and
# a reader who does not know that reads the second set of rejections as the
# system changing its mind.
PHASE_PURPOSE = {
    "causal screen": "whether what happened gives us standing to act on this man at all",
    "simulator": "whether the move is actually worth making",
}


def _n(value, fmt="{:+.2f}", dash="—"):
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return dash


def _name(row: dict | None, default="(open roster spot)") -> str:
    row = row or {}
    return str(row.get("name") or row.get("player_id") or default)


# --------------------------------------------------------------------------
# One considered player
# --------------------------------------------------------------------------

def option_story(cand: dict, thresholds: dict | None = None) -> str:
    """One sentence saying which bar this candidate met or missed, with the number."""
    t = thresholds or {}
    who = cand.get("name") or "This candidate"
    native = str(cand.get("native") or "")
    gain, se = cand.get("gain"), cand.get("se")
    ceiling = cand.get("ceiling")

    if native == "selected":
        return (f"{who} was taken: a gain of {_n(gain)} against a simulation error of "
                f"±{_n(se, '{:.2f}')}, which clears every bar in this pass.")
    if native == "below simulator noise":
        return (f"{who} looks like {_n(gain)}, but the simulator's own error on that "
                f"comparison is ±{_n(se, '{:.2f}')}. The gain is not distinguishable "
                f"from noise, so it is not a reason to touch the roster.")
    if native == "below starting-gain bar":
        bar = t.get("starting_gain")
        return (f"{who} would start, and a move into a starting slot has to be worth at "
                f"least {_n(bar, '{:.2f}')}. He is worth {_n(gain)}.")
    if native == "below bench-ceiling bar":
        bar = t.get("bench_ceiling")
        return (f"{who} would sit on the bench, where the question is not his average but "
                f"his best case. A bench flier has to show a ceiling of "
                f"{_n(bar, '{:.2f}')}; his is {_n(ceiling, '{:.2f}')}.")
    if native.startswith("cleared; outranked by the coverage floor"):
        return (f"{who} was worth making ({_n(gain)}), but another candidate refilled a "
                f"position we are short at, and coverage orders the ladder ahead of value.")
    if native.startswith("cleared; lower-ranked option"):
        return (f"{who} was worth making ({_n(gain)}) and was beaten by a better move in "
                f"the same pass.")

    # Causal screen. The recorded reasons are already terse English; expand the
    # common ones and quote the rest rather than invent a gloss for a string
    # this function has not seen.
    reason = str(cand.get("reason") or "")
    delta = cand.get("delta_ros")
    if reason == "no positive causal event delta":
        return (f"{who} did not gain value from what happened, so nothing in this event "
                f"gives us standing to add him.")
    if reason == "missing from weekly ROS table":
        return (f"{who} has no row in the rebuilt weekly table, so there was no value to "
                f"compare. He was not judged.")
    if reason == "pre/post event series is incomplete":
        return (f"{who} was missing weeks on one side of the rebuild, so the before/after "
                f"comparison would not have been like for like.")
    if reason.startswith("coverage floor would fail"):
        return (f"{who} gained {_n(delta)} from the event, but adding him would leave a "
                f"position short: {reason.split(':', 1)[-1].strip()}.")
    if reason.startswith("ROS "):
        return (f"{who} gained {_n(delta)} from the event, but he still does not beat the "
                f"man he would replace — {reason}.")
    if str(cand.get("status")) == "selected":
        return f"{who} cleared the causal screen: {reason or 'no reason recorded'}."
    return f"{who}: {reason or native or 'no reason recorded'}."


# --------------------------------------------------------------------------
# One claim
# --------------------------------------------------------------------------

def claim_story(claim: dict) -> str:
    """What this claim is, what it costs, and why that price."""
    add, drop = claim.get("add") or {}, claim.get("drop") or {}
    bid = claim.get("bid", 0)
    target = _name(add, "someone")
    cut = _name(drop)
    head = (f"Claim {target} for ${bid}"
            + (f", cutting {cut}" if drop.get("player_id") else " into an open roster spot"))
    gain, se = claim.get("bid_gain", claim.get("gain")), claim.get("bid_se", claim.get("se"))
    body = f"It is worth {_n(gain)} to the starting lineup, ±{_n(se, '{:.2f}')}."
    q_info = claim.get("quality") or (claim.get("bid_quote") or {}).get("quality")
    if q_info:
        tier = q_info.get("tier", "")
        q_val = float(q_info.get("q") or 0.0)
        opt = float(q_info.get("option_value") or 0.0)
        opt_str = f" with {opt:+.1f} option equity" if opt > 0 else ""
        body += f" Quality {tier} (Q={q_val:.2f}{opt_str})."
    quote = (claim.get("bid_quote") or {}).get("reason")
    return f"{head}. {body}" + (f" {quote}" if quote else "")


def bid_story(claim: dict) -> str:
    """Why the bid stopped where it did.

    The two numbers that sit next to each other -- "expected high $34" beside a
    $1 bid -- read as a failure of nerve until you know the ceiling is
    arithmetic: a dollar costs `lam` lineup points, so a gain of G cannot
    justify more than G/lam dollars whatever anyone else does.
    """
    q = claim.get("bid_quote") or {}
    lam = float((q.get("shadow_price") or {}).get("points_per_dollar") or 0)
    res, high = q.get("reservation_bid"), q.get("expected_highest")
    if res is None or lam <= 0:
        return ""
    eff = q.get("effective_gain")
    opt = q.get("option_value", 0.0)
    if eff is not None and opt and float(opt) > 0:
        gain_desc = f"of {_n(q.get('gain'))} plus {_n(opt)} option equity ({_n(eff)} total)"
    else:
        gain_desc = f"of {_n(q.get('gain'))}"
    out = (f"The reservation price is ${res}. At {lam:.2f} lineup points per dollar, a gain "
           f"{gain_desc} cannot justify paying more, so the curve stops there.")
    if high is not None and float(high) > float(res):
        need = (float(high) + 1) * lam
        out += (f" Beating the expected top bid of ${float(high):.0f} would need a gain of "
                f"about {need:.1f} points, so this one is out of reach by arithmetic "
                f"rather than by choice.")
    return out


# --------------------------------------------------------------------------
# A whole run
# --------------------------------------------------------------------------

def gate_sentence(run: dict) -> str:
    if run.get("blackout"):
        return f"Held short of the wire: {run['blackout']}"
    if run.get("control_block"):
        return f"Held by a roster control: {run['control_block']}"
    if run.get("gated"):
        return ("The transaction gate is closed, so everything above is a proposal. "
                "Nothing was sent to Sleeper.")
    if run.get("submitted"):
        return f"{len(run['submitted'])} submission(s) were recorded."
    if not run.get("submission_recorded"):
        return ("The gate was open, but this record does not store what was sent — read the "
                "pending portfolio or the public decision log for the outcome.")
    return "Nothing was submitted."


# What a changed field MEANS, for the "what woke it" sentence. The record's
# field names are exact and unreadable ("espn_body_part", "sleeper_news_content").
_FIELD_WORDS = {
    "status": "injury designations", "espn_availability": "ESPN availability",
    "espn_return_date": "return dates", "espn_body_part": "injury details",
    "espn_short": "ESPN injury notes", "espn_long": "ESPN injury notes",
    "sleeper_news_content": "new news stories", "depth_order": "depth-chart moves",
    "points": "projection changes", "pft": "Pro Football Talk headlines",
}


def _list(names: list[str], limit: int = 3) -> str:
    names = [n for n in names if n]
    if len(names) <= limit:
        return (", ".join(names[:-1]) + " and " + names[-1]) if len(names) > 1 else "".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def _sim_verdict(run: dict, add_id, drop_id) -> str | None:
    """What the lineup simulator said about this exact swap, if it priced it."""
    for c in run.get("candidates") or []:
        if c.get("phase") != "simulator":
            continue
        raw = c.get("raw") or {}
        if (str((raw.get("add") or {}).get("player_id") or c.get("player_id")) == str(add_id)
                and str((raw.get("drop") or {}).get("player_id") or "") == str(drop_id or "")):
            return c.get("status")
    return None


def _move_story(run: dict, row: dict, submitted: bool) -> str:
    """One move as sentences, from the same recorded fields as move_trace."""
    raw = run.get("raw") or {}
    action = raw.get("action") or {}
    idx = news_audit.player_index(raw)
    add, drop = row.get("add") or "?", row.get("drop") or "nobody"
    add_id, drop_id = str(row.get("add_id") or ""), str(row.get("drop_id") or "")
    parts = []
    delta = (action.get("event_deltas") or {}).get(add_id) or {}
    edge = str(delta.get("causal_edge") or "")
    change = (f"{float(delta.get('delta_ros') or 0):+.2f} "
              f"({delta.get('pre_ros')} to {delta.get('post_ros')})")
    if edge.startswith("successor-of:"):
        lead = news_audit.label(edge.split(":", 1)[1], idx)
        parts.append(f"{add} was not in the news himself. He is next in line behind {lead} "
                     f"in {delta.get('team') or 'his team'}'s {delta.get('pos') or ''} room, "
                     f"and {lead}'s update moved his rest-of-season value by {change}.")
    elif edge.startswith("self:"):
        parts.append(f"{add}'s own news moved his rest-of-season value by {change}.")
    elif edge.startswith("line_move:"):
        parts.append(f"A betting-line move in {edge.split(':', 1)[1]} moved {add}'s "
                      f"rest-of-season value by {change}.")
    check = next((c for c in action.get("candidate_checks") or []
                  if str(c.get("player_id")) == add_id
                  and str(c.get("drop_id") or "") == drop_id), None)
    if check:
        parts.append(f"That gave the bot grounds to act on him, and at {check.get('candidate_ros')} "
                     f"he outvalued {drop} ({check.get('drop_ros')}), the cheapest player it could "
                     f"cut without leaving a position short: a gain of "
                     f"{float(check.get('gain') or 0):+.1f} rest-of-season points.")
    elif row.get("why"):
        parts.append(f"{add} for {drop}: {row['why']} (gain {float(row.get('gain') or 0):+.1f}).")
    verdict = _sim_verdict(run, add_id, drop_id)
    if verdict and verdict != "selected":
        parts.append(f"The lineup simulator, asked separately whether this swap improves the "
                     f"lineup we would actually start, rated it '{verdict}'"
                     + (" — the two checks disagreed, and the move was made on the first."
                        if submitted else "."))
    return " ".join(parts)


def run_story(run: dict) -> list[str]:
    """What a run did and why, in plain English, from recorded facts only.

    Leads with the outcome, names the players, says what woke the run in
    words rather than field names, explains each move through its chain, and
    says so when the two independent checks disagreed -- the one thing a
    reader most needs and the old count-by-count version buried.
    """
    from robo import decision_audit
    raw = run.get("raw") or {}
    moves = decision_audit.slate(run)
    sent = news_audit.submitted(raw) if run.get("kind") == "news" else run.get("submitted") or []
    sent_names = {(s.get("add"), s.get("drop")) for s in sent if isinstance(s, dict)}
    out: list[str] = []

    # 1. What it did.
    def did(r):
        add, drop = r.get("add"), r.get("drop")
        if r.get("channel") == "waiver claim":
            return (f"put in a ${r.get('bid')} claim for {add}"
                    + (f" (dropping {drop})" if drop and drop != "(open roster spot)" else ""))
        return (f"signed {add}" + (f" and released {drop}"
                                  if drop and drop != "(open roster spot)" else " into an open spot"))
    done = [r for r in moves if (r.get("add"), r.get("drop")) in sent_names
            or (r.get("channel") == "waiver claim" and sent)]
    if done:
        out.append("This run " + _list([did(r) for r in done], limit=4) + ".")
    elif moves and not run.get("gated") and not run.get("submission_recorded"):
        # A scheduled record stores the slate, not the send. Saying "nothing was
        # sent" here was false: Tuesday's slate went in.
        out.append("This run built a slate to " + _list([did(r) for r in moves], limit=4)
                   + ". This record does not store what reached Sleeper — the "
                     "Transactions page does.")
    elif moves:
        out.append("This run wanted to " + _list([did(r) for r in moves], limit=4)
                   + ", but nothing was sent. " + gate_sentence(run))
    else:
        out.append("This run made no roster move. " + slate_absence(run))

    # 2. What woke it.
    if run.get("kind") == "news":
        events = raw.get("events") or []
        # The players whose news actually led to a move go first: they are the
        # ones a reader is looking for, and a list of eleven buries them.
        deltas = (raw.get("action") or {}).get("event_deltas") or {}
        causes = {str(deltas.get(str(r.get("add_id")), {}).get("lead_id") or r.get("add_id"))
                  for r in moves}
        ordered = sorted(events, key=lambda e: str(e.get("player_id")) not in causes)
        names = [e.get("name") for e in ordered]
        kinds = sorted({_FIELD_WORDS.get(c.get("field"), c.get("field"))
                        for e in events for c in e.get("changes") or []})
        lines = raw.get("line_events") or []
        woke = []
        if events:
            woke.append(f"fresh reports on {len(events)} player(s) — {_list(names)} "
                        f"({_list(kinds, limit=8)})")
        if lines:
            woke.append(f"betting-line moves in {_list([e.get('game_id') for e in lines])}")
        if woke:
            out.append("It was woken by " + " and ".join(woke) + ".")
    else:
        from robo.decision_audit import MODE_STORY
        out.append(MODE_STORY.get(run.get("mode"), "A scheduled roster pass ran."))

    # 3. Each move, through its chain.
    for r in moves:
        story = _move_story(run, r, (r.get("add"), r.get("drop")) in sent_names)
        if story:
            out.append(story)

    # 4. Everything it looked at and passed on.
    sim = [c for c in run.get("candidates") or [] if c.get("phase") == "simulator"]
    if sim:
        taken = sum(c["status"] == "selected" for c in sim)
        out.append(f"Separately, the lineup simulator priced {len(sim)} possible add/drop "
                   f"swaps against the lineup we would actually start and found "
                   + ("none worth making." if not taken else f"{taken} worth making."))
    if run.get("source_errors"):
        out.append("Some data sources failed this run, so it was not allowed to act: "
                   + "; ".join(str(e) for e in run["source_errors"]) + ".")
    return out


def slate_line(run: dict) -> str:
    """One scannable line of what a run actually proposed.

    A timeline whose only verdict column reads "Proposal only" says a run
    happened and nothing about what it wanted to do, so comparing two runs means
    opening both. This is the same slate the Now page renders, compressed to fit
    a row: who comes in, what it costs, and who goes out.
    """
    from robo import decision_audit
    bits = []
    for row in decision_audit.slate(run):
        add = row.get("add") or "?"
        bid = row.get("bid")
        drop = row.get("drop") or ""
        piece = f"{add}" + (f" ${bid}" if bid is not None else "")
        if drop and drop != "(open roster spot)":
            piece += f" ← {drop}"
        else:
            piece += " ← open spot"
        bits.append(piece)
    return "; ".join(bits)


def slate_absence(run: dict) -> str:
    """Why the slate is empty, when it is. An empty table explains nothing."""
    if run.get("blackout"):
        return f"No moves: {run['blackout']}"
    if run.get("control_block"):
        return f"No moves: {run['control_block']}"
    sim = [c for c in run.get("candidates") or [] if c.get("phase") == "simulator"]
    if sim:
        return (f"Nothing cleared. {len(sim)} pairing(s) were priced and none beat both the "
                f"simulator's own error and the policy bar for its slot.")
    if run.get("candidates"):
        return (f"Nothing cleared. {len(run['candidates'])} player(s) were screened and none "
                f"reached the point of being priced.")
    return "Nothing was considered in this run."
