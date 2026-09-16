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
    body = (f"It is worth {_n(gain)} to the starting lineup, ±{_n(se, '{:.2f}')}.")
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
    out = (f"The reservation price is ${res}. At {lam:.2f} lineup points per dollar, a gain "
           f"of {_n(q.get('gain'))} cannot justify paying more, so the curve stops there.")
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


def run_story(run: dict) -> list[str]:
    """Plain-English paragraphs for a decision run of either kind."""
    out: list[str] = []
    if run.get("kind") == "news":
        # The existing narrative is the trigger/room/causal half and is already
        # derived only from recorded facts. Keep it verbatim.
        out += news_audit.narrative(run.get("raw") or {})
    else:
        out.append(run.get("trigger_story") or "A scheduled roster pass ran.")
        if run.get("sequence_basis"):
            out.append("Waiver claims were priced against the "
                       f"{run['sequence_basis']}, not against the roster as it stood "
                       "before the free-agent pass — otherwise the same slot would be "
                       "counted as empty twice.")

    sim = [c for c in run.get("candidates") or [] if c.get("phase") == "simulator"]
    if sim:
        selected = sum(c["status"] == "selected" for c in sim)
        cleared = sum(c["status"] == "cleared, not offered" for c in sim)
        out.append(
            f"The simulator then priced {len(sim)} add/drop pairing(s) against our own "
            f"optimal lineup: {selected} taken, {cleared} worth making but beaten by a "
            f"better move or by a position we are shorter at, and "
            f"{len(sim) - selected - cleared} short of a bar. This is a separate question "
            f"from the screen above — that one asks "
            f"{PHASE_PURPOSE['causal screen']}, this one asks "
            f"{PHASE_PURPOSE['simulator']}.")

    moves = (run.get("free_moves") or []) + (run.get("claims") or [])
    if moves:
        free_n, claim_n = len(run.get("free_moves") or []), len(run.get("claims") or [])
        bits = []
        if free_n:
            bits.append(f"{free_n} free-agent move(s)")
        if claim_n:
            worst = run.get("worst_case_faab")
            bits.append(f"{claim_n} waiver claim(s)"
                        + (f" at a worst case of ${worst}" if worst is not None else ""))
        out.append("This run proposes " + " and ".join(bits) + ".")
    out.append(gate_sentence(run))
    if run.get("source_errors"):
        out.append("Source failures suppressed live authority: "
                   + "; ".join(run["source_errors"]))
    return out


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
