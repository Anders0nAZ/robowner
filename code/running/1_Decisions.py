"""Every roster re-evaluation, front to back, whatever started it.

ONE TIMELINE, TWO ALARM CLOCKS. A decision reached because the news moved and
a decision reached because it was Wednesday are the same decision: both screen
candidates, both price a drop, both build a waiver ladder. They used to live on
separate pages named after the module that wrote the record, which meant a
reader had to already know which clock had fired before they could go looking.

AND THE BID IS ON THIS PAGE. The waiver record carries `bid_quote` and
`opponent_field` on every claim; those used to be rendered on a different page
from the claim itself, so following one transaction meant remembering a name
and retyping it. The whole argument for a bid now sits under the claim it
prices.
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from robo import decision_audit, evidence, narrate, news_audit, newswatch, ui

st.title("Decisions")
ui.gate_banner(st)


@st.cache_data(ttl=10, show_spinner=False)
def runs():
    return decision_audit.events(limit=200)


@st.cache_data(ttl=10, show_spinner=False)
def pulse_state():
    return news_audit.state()


@st.cache_data(ttl=600, show_spinner=False)
def name_fallback() -> dict:
    from robo import expected
    return {str(pid): {"name": r.get("name") or str(pid), "pos": r.get("pos"),
                       "team": r.get("team")}
            for pid, r in (expected.load().get("players") or {}).items()}


def _show(field, value) -> str:
    """A trigger value as a reader can check it.

    `news_updated` is milliseconds since the epoch, which says nothing at all
    until it is a clock time.
    """
    if value is None:
        return "—"
    if field == "news_updated":
        try:
            return news_audit.local_time(float(value) / 1000)
        except (TypeError, ValueError):
            pass
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


docs = runs()
if not docs:
    st.info("No decision run has been recorded yet.")
    st.stop()

state = pulse_state()
m = st.columns(4)
m[0].metric("Last poll", ui.fmt_age(state.get("last_poll")))
m[1].metric("Last decision", ui.fmt_age(docs[0].get("at")))
m[2].metric("Runs recorded", len(docs))
m[3].metric("Latest result", docs[0]["outcome"])

if state.get("paused"):
    st.info("Watcher intentionally paused: "
            + str(state["paused"].get("reason") or "scheduled pause"))
if state.get("source_errors"):
    st.error("Latest poll source failures: " + "; ".join(state["source_errors"]))
if state.get("filtered"):
    labels = {"sleeper_timestamp_only": "Sleeper timestamp-only",
              "espn_timestamp_only": "ESPN timestamp-only",
              "pft_indirect_mention": "indirect PFT mention",
              "trending_without_corroboration": "uncorroborated trending",
              "postgame_projection": "completed-game projection cleanup",
              "postgame_inactive": "expired postgame inactive row",
              "espn_record_removed": "removed ESPN row"}
    st.caption("Signals the latest poll saw and declined to act on: "
               + ", ".join(f"{labels.get(k, k)} {v}"
                           for k, v in state["filtered"].items() if v))
if state.get("pending_reviews"):
    st.caption(f"Advisory prose queue: {len(state['pending_reviews'])} player(s); up to "
               f"{newswatch.PULSE_REVIEW_BATCH} are reviewed per pulse.")

# ------------------------------------------------------------- the timeline
st.subheader("Every re-evaluation")
kinds = st.radio("Started by", ["everything", "the news", "the clock"], horizontal=True,
                 help="A news pulse that carries an event rebuilds the whole waiver "
                      "portfolio, so most runs are news-triggered. A clock run is one "
                      "of the scheduled passes that ran on its own.")
view = docs
if kinds == "the news":
    view = [d for d in docs if d["kind"] == decision_audit.NEWS]
elif kinds == "the clock":
    view = [d for d in docs if d["kind"] == decision_audit.CLOCK]
if not view:
    st.info("No run of that kind has been recorded.")
    st.stop()

st.dataframe(pd.DataFrame([{
    "when": news_audit.local_time(d.get("at")),
    "started by": "news" if d["kind"] == decision_audit.NEWS else f"clock · {d['mode']}",
    "week": d.get("week"),
    "considered": len(d["candidates"]),
    "moves": len(decision_audit.slate(d)),
    "result": d["outcome"],
    "fingerprint": d["fingerprint"],
} for d in view]), use_container_width=True, hide_index=True,
    height=min(280, 38 + 35 * len(view)))

# A run picked from the Now page arrives as ?run=<fingerprint>.
wanted = st.query_params.get("run")
keys = [d["fingerprint"] for d in view]
labels = {d["fingerprint"]: f"{news_audit.local_time(d.get('at'))} · {d['outcome']}"
          for d in view}
index = keys.index(wanted) if wanted in keys else 0
chosen = st.selectbox("Open a decision", keys, index=index, format_func=labels.get)
doc = next(d for d in view if d["fingerprint"] == chosen)
raw = doc.get("raw") or {}
action = raw.get("action") or {}
idx = news_audit.player_index(raw, name_fallback())

st.divider()
st.subheader("What happened")
for paragraph in narrate.run_story(doc):
    st.write(paragraph)

# ------------------------------------------------------------- 1 · trigger
st.subheader("1 · What set this off")
st.caption(doc.get("trigger_story") or "")
if doc["kind"] == decision_audit.NEWS:
    rows = []
    for e in doc.get("trigger") or []:
        for c in e.get("changes") or [{"field": "event", "before": None, "after": None}]:
            rows.append({
                "player": e.get("name") or news_audit.label(e.get("player_id"), idx),
                "signal": c.get("field"),
                "before": _show(c.get("field"), c.get("before")),
                "after": _show(c.get("field"), c.get("after")),
                "why it counted": "; ".join(e.get("reasons") or [])})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    timing = doc.get("timing") or {}
    t = st.columns(4)
    t[0].metric("Dated by rule", len(timing.get("deterministic") or []),
                help="Only a date that comes from a rule may change availability.")
    t[1].metric("Prose read", news_audit.model_review_count(timing),
                help="What the local model read. Visible context; it cannot move a number.")
    t[2].metric("Thrown out", len(timing.get("quarantined") or {}),
                help="Prose the structured feed contradicted.")
    t[3].metric("Retired", len(timing.get("cleared") or []),
                help="Old verdicts dropped because the designation went away.")
    st.caption(timing.get("summary") or "No timing pass recorded.")
    if timing.get("quarantined"):
        st.warning(" · ".join(f"{news_audit.label(pid, idx)}: {why}"
                              for pid, why in timing["quarantined"].items()))
    if timing.get("bounds"):
        st.dataframe(pd.DataFrame([{
            "player": news_audit.label(pid, idx),
            "earliest he may play": b.get("return_week_min"),
            "latest reporting supports": b.get("return_week_max"),
            "out for the season": bool(b.get("out_for_season")),
            "where the date came from": b.get("return_basis"),
            "the sentence it was read from": b.get("timing_sentence"),
        } for pid, b in timing["bounds"].items()]),
            use_container_width=True, hide_index=True)
    if timing.get("advisory_reviews"):
        with st.expander("What the local model read"):
            st.dataframe(pd.DataFrame(timing["advisory_reviews"]),
                         use_container_width=True, hide_index=True)
elif doc.get("sequence_basis"):
    st.caption(f"Claims were priced against the **{doc['sequence_basis']}** — not the "
               "roster as it stood before the free-agent pass, or the same slot would "
               "be counted empty twice.")

# -------------------------------------------------------- 2 · what moved
st.subheader("2 · What moved, and whether we may act on it")
deltas = doc.get("event_deltas") or {}
if deltas:
    provider = doc.get("provider_at_trigger") or {}
    rows = []
    for pid, d in deltas.items():
        edge = str(d.get("causal_edge") or "")
        relation = ("he is the trigger" if edge.startswith("self:")
                    else f"inherits from {news_audit.label(d.get('lead_id'), idx)}"
                    if edge.startswith("successor-of:") else "same room, no causal edge")
        rows.append({
            "player": d.get("name") or news_audit.label(pid, idx),
            "pos": d.get("pos") or (idx.get(pid) or {}).get("pos"),
            "team": d.get("team") or (idx.get(pid) or {}).get("team"),
            "why he is here": relation,
            "ours or theirs": d.get("ownership", "not frozen in this record"),
            "can we get him": d.get("acquisition_candidate"),
            "Sleeper before": (provider.get(pid) or {}).get("points_before"),
            "Sleeper after": (provider.get(pid) or {}).get("points_at_trigger"),
            "value before": d.get("pre_ros"), "value after": d.get("post_ros"),
            "change": d.get("delta_ros"),
        })
    rows.sort(key=lambda r: (not bool(r["can we get him"]),
                             -abs(float(r["change"] or 0)), r["player"]))
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                 column_config={
                     "value before": st.column_config.NumberColumn(format="%.2f"),
                     "value after": st.column_config.NumberColumn(format="%.2f"),
                     "Sleeper before": st.column_config.NumberColumn(format="%.2f"),
                     "Sleeper after": st.column_config.NumberColumn(format="%.2f"),
                     "change": st.column_config.NumberColumn(format="%+.2f"),
                     "why he is here": st.column_config.TextColumn(
                         help="Only the trigger himself or a fitted successor gives us "
                              "standing to add. A same-room player may explain the "
                              "rebuild but cannot authorise a move."),
                 })
    st.caption("Sleeper's own number barely moves for a man who is out for weeks — it "
               "does not propagate an absence forward. The availability ramp inside "
               "`expected.py` is what applies it, which is why the value columns and "
               "the Sleeper columns disagree.")
    pick = st.selectbox("Week by week, for", list(deltas),
                        format_func=lambda pid: news_audit.label(pid, idx))
    weekly = (deltas[pick].get("by_week") or {})
    st.dataframe(pd.DataFrame([{"week": int(w), "change": v}
                               for w, v in sorted(weekly.items(), key=lambda x: int(x[0]))]),
                 use_container_width=True, hide_index=True,
                 column_config={"change": st.column_config.NumberColumn(format="%+.3f")})
else:
    st.caption("This run was not triggered by a value change, so nothing was measured "
               "here. It went straight to pricing the board.")

# ------------------------------------------------------ 3 · who was judged
st.subheader("3 · Who was considered, and why each one lost")
cands = doc.get("candidates") or []
if not cands:
    st.info("No player reached the screen in this run.")
else:
    phases = sorted({c["phase"] for c in cands})
    phase = st.radio("Which screen", phases, horizontal=True,
                     format_func=lambda p: f"{p} — {narrate.PHASE_PURPOSE.get(p, '')}")
    sel = [c for c in cands if c["phase"] == phase]
    only_close = st.checkbox("Only the ones that nearly made it", value=False,
                             help="Cleared every bar but was not the move taken, or "
                                  "missed a bar by less than a tenth of a point.")
    if only_close:
        sel = [c for c in sel
               if c["status"] == "cleared, not offered"
               or (c.get("noise_margin") is not None
                   and -0.1 <= float(c["noise_margin"]) < 0)]
    t = doc.get("thresholds") or {}
    if t and phase == "simulator":
        st.caption(ui.money(
            f"Bars in force for this run: a gain must beat {t.get('noise_multiple')}× the "
            f"simulator's own error; a move into a starting slot must reach "
            f"{t.get('starting_gain')}; a bench flier must show a ceiling of "
            f"{t.get('bench_ceiling')}; we will not cut more than {t.get('drop_floor')}. "
            "These are frozen at decision time, so a near-miss is never re-judged "
            "against today's settings."))
    st.dataframe(pd.DataFrame([{
        "add": c["name"], "pos": c["pos"], "drop": c["drop_name"] or "(open spot)",
        "channel": c["channel"], "result": c["status"],
        "in plain terms": narrate.option_story(c, t),
        "gain": c["gain"], "error": c["se"], "ceiling": c["ceiling"],
        "event change": c["delta_ros"],
        "the code's words": c["native"],
    } for c in sel]), use_container_width=True, hide_index=True, height=360,
        column_config={
            "gain": st.column_config.NumberColumn(format="%+.2f"),
            "error": st.column_config.NumberColumn(format="%.2f"),
            "ceiling": st.column_config.NumberColumn(format="%.2f"),
            "event change": st.column_config.NumberColumn(format="%+.2f"),
            "in plain terms": st.column_config.TextColumn(width="large"),
        })

drops = doc.get("drop_checks") or []
if drops:
    with st.expander(f"Who could legally be cut ({len(drops)} checked)"):
        st.caption("Injured reserve is capacity, not bookkeeping: three slots on top of "
                   "the active roster, so every eligible body parked there is an active "
                   "slot a claim can use.")
        st.dataframe(pd.DataFrame([{
            "for": news_audit.label(r.get("candidate_id"), idx) if r.get("candidate_id")
                   else "any move",
            "player": r.get("drop_name") or r.get("name")
                      or news_audit.label(r.get("drop_id") or r.get("player_id"), idx),
            "pos": r.get("drop_pos") or r.get("pos"),
            "can we cut him": r.get("eligible"),
            "what he costs to cut": r.get("drop_ros", r.get("drop_price")),
            "why not": r.get("reason"),
        } for r in drops]), use_container_width=True, hide_index=True)

controls = doc.get("control_checks") or []
if controls:
    with st.expander(f"Roster-shape checks ({len(controls)})"):
        st.caption("The coverage floor ORDERS the ladder, it does not close the door. A "
                   "move is refused only if it makes a position worse; a roster already "
                   "under a floor still evaluates everyone, and the candidates that "
                   "refill the short position rank first. Priority 0 relieves or "
                   "preserves every floor; 1 is merely not worse.")

        def counts(c, key):
            got = (c.get("coverage") or {}).get(key) or {}
            return " ".join(f"{k}{v}" for k, v in got.items()) or "—"

        st.dataframe(pd.DataFrame([{
            "add": (c.get("add") or {}).get("name") or c.get("add_id"),
            "drop": (c.get("drop") or {}).get("name") or c.get("drop_id"),
            "allowed": c.get("eligible"), "priority": c.get("coverage_priority"),
            "before": counts(c, "counts_before"), "after": counts(c, "counts"),
            "fixes": ", ".join((c.get("coverage") or {}).get("relieves") or []),
            "why not": c.get("reason"),
        } for c in controls]).drop_duplicates(),
            use_container_width=True, hide_index=True)

# --------------------------------------------------------- 4 · the bid
claims = doc.get("claims") or []
free = doc.get("free_moves") or []
if free:
    st.subheader("4 · Moves that need no bid")
    st.dataframe(pd.DataFrame([{
        "add": (p.get("add") or {}).get("name"),
        "drop": (p.get("drop") or {}).get("name") or "(open roster spot)",
        "gain": p.get("gain"), "ceiling": p.get("ceiling"), "why": p.get("why"),
    } for p in free]), use_container_width=True, hide_index=True,
        column_config={"gain": st.column_config.NumberColumn(format="%+.2f"),
                       "ceiling": st.column_config.NumberColumn(format="%.2f")})

if claims:
    st.subheader(f"{'5' if free else '4'} · What each claim is worth, and what it costs")
    st.caption("A slate is a ladder: Sleeper reaches the rungs in bid order and the "
               "first winner takes the slot.")
    for claim in claims:
        add = claim.get("add") or {}
        q = claim.get("bid_quote") or {}
        field = claim.get("opponent_field") or {}
        st.markdown(f"**Rung {claim.get('priority', 0)} · {add.get('name')} · "
                    f"\\${claim.get('bid', 0)}**")
        st.write(ui.money(narrate.claim_story(claim)))
        c = st.columns(4)
        c[0].metric("Worth to the lineup", f"{float(claim.get('bid_gain') or 0):+.2f}",
                    f"± {float(claim.get('bid_se') or 0):.2f}")
        c[1].metric("Chance we win", f"{float(q.get('p_win') or 0):.0%}")
        c[2].metric("Expected top rival bid",
                    f"${float(q['expected_highest']):.1f}"
                    if q.get("expected_highest") is not None else "pooled fallback")
        band = q.get("near_optimal") or [claim.get("bid", 0), claim.get("bid", 0)]
        c[3].metric("Near-optimal band", f"${band[0]}–${band[1]}")
        story = narrate.bid_story(claim)
        if story:
            st.caption(ui.money(story))
        shadow = q.get("shadow_price") or {}
        if shadow:
            st.caption(ui.money(
                f"A FAAB dollar is priced at {float(shadow.get('points_per_dollar') or 0):.2f} "
                f"lineup points ({shadow.get('status')}: {shadow.get('basis')}). FAAB is "
                "scarce and expires worthless, so the price is paced, not fixed."))
        direct = claim.get("direct_ros") or {}
        if direct:
            d = st.columns(4)
            d[0].metric("Simple add value", f"{float(direct.get('add_ros') or 0):.1f}")
            d[1].metric("Simple drop value", f"{float(direct.get('drop_ros') or 0):.1f}")
            d[2].metric("Simple difference", f"{float(direct.get('gain') or 0):+.1f}")
            d[3].metric("Two engines", "agree" if direct.get("prefers_move") else "disagree")
            st.caption("The simple comparator just subtracts two season totals. The "
                       "paired figure above stays authoritative because it measures the "
                       "change in Robowner's optimised lineups from the same inputs — "
                       "a defence summing to 118 is not better than a fourth receiver "
                       "summing to 45 if neither changes who starts.")
        if q.get("curve"):
            curve = pd.DataFrame(q["curve"])
            with st.expander("Every dollar we could have bid"):
                st.caption("The whole objective, not a summary of it. FAAB is paid only "
                           "on a win, so the utility is the chance of winning times what "
                           "the claim is worth after paying.")
                shown = curve.copy()
                if "p_win" in shown:
                    shown["p_win"] = shown["p_win"].map(lambda v: f"{float(v):.1%}")
                st.dataframe(shown, use_container_width=True, hide_index=True,
                             column_config={
                                 "bid": st.column_config.NumberColumn("bid", format="$%d"),
                                 "p_win": "chance we win",
                                 "net_if_won": st.column_config.NumberColumn(
                                     "worth it if we win", format="%.2f"),
                                 "utility": st.column_config.NumberColumn(
                                     "expected value", format="%.3f")})
            if "bid" in curve and "utility" in curve:
                st.line_chart(curve.set_index("bid")[["utility"]], height=200)
        if field.get("available"):
            valid = field.get("validation") or {}
            if valid:
                st.caption(
                    "Held-out check on the per-manager model: demand Brier "
                    f"{valid.get('demand_brier')} against {valid.get('pooled_demand_brier')} "
                    f"for the pooled fallback, bid CRPS {valid.get('bid_crps')} against "
                    f"{valid.get('pooled_bid_crps')}. "
                    + ("It beat pooled, so it was used."
                       if valid.get("passes") else
                       "It did not beat pooled, so pooled history was used instead."))
            st.dataframe(pd.DataFrame([{
                "manager": o.get("manager"),
                "what he needs": (o.get("need") or {}).get("need"),
                "what the player is worth to him": (o.get("need") or {}).get("gain"),
                "weeks he would start": (o.get("need") or {}).get("starts"),
                "chance he claims": f"{float(o.get('p_claim') or 0):.0%}",
                "typical bid": o.get("bid_p50"), "high bid": o.get("bid_p90"),
                "FAAB left": o.get("faab_left"),
                "tie-break": o.get("waiver_position"),
                "claims seen": (o.get("evidence") or {}).get("manager_samples"),
            } for o in sorted(field.get("opponents") or [],
                              key=lambda x: -float(x.get("p_claim") or 0))]),
                use_container_width=True, hide_index=True,
                column_config={"what the player is worth to him":
                               st.column_config.NumberColumn(format="%+.2f")})
        else:
            st.warning("No per-manager forecast, so pooled league history was used: "
                       + str(field.get("reason") or "no reason recorded"))

# ----------------------------------------------------- final state
st.subheader("Could any of this reach Sleeper?")
f = st.columns(4)
f[0].metric("Mode", "dry run" if raw.get("dry_run") else
            ("gate closed" if doc.get("gated") else "live"))
f[1].metric("Source failures", len(doc.get("source_errors") or []))
f[2].metric("Proposed", len(free) + len(claims))
f[3].metric("Submitted", len(doc.get("submitted") or [])
            if doc.get("submission_recorded") else "—")
st.write(narrate.gate_sentence(doc))
prov = doc.get("provenance") or {}
if any(prov.values()):
    st.caption(f"Projection capture: {prov.get('capture', 'not recorded')} · "
               f"Export: {prov.get('export', 'not recorded')} · "
               f"Model: {prov.get('model', 'not recorded')}. The capture must run before "
               "the export or the export is a cache hit and the weekly number stays "
               "anchored on an older run.")

# ---------------------------------------------------------- the evidence
st.divider()
st.subheader("The record itself")
st.caption("Every field the deciding code wrote down, with what it means. This is the "
           "immutable evidence — it is what the bot acted on, not a re-derivation.")
fields = evidence.explain_fields(raw)
if doc.get("paired_raw"):
    fields += evidence.explain_fields(doc["paired_raw"], prefix="waiver record")
    st.caption("This decision wrote two files: the pulse recorded the trigger and the "
               "screen, and the waiver pass recorded the near-miss ledger and the slate. "
               "Both are below.")
hide_empty = st.checkbox("Hide empty and container rows", value=True)
shown = [r for r in fields if not hide_empty or r["value"] not in {"", "—"}]
st.dataframe(pd.DataFrame(shown)[["path", "value", "meaning"]],
             use_container_width=True, hide_index=True, height=420,
             column_config={"path": st.column_config.TextColumn("field", width="medium"),
                            "meaning": st.column_config.TextColumn(width="large")})

with st.expander("The raw JSON, and where it lives on disk"):
    report = Path(doc.get("report_path") or "")
    if report.is_file():
        st.code(report.read_text(encoding="utf-8"), language="text")
    st.json(raw, expanded=False)
    st.caption(doc.get("path") or "")
    if doc.get("paired_path"):
        st.json(doc["paired_raw"], expanded=False)
        st.caption(doc["paired_path"])
