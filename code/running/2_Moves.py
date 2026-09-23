"""Moves & Waivers Pipeline Audit — Front-to-Back Decision Trace.

Exhaustively traces every roster re-evaluation across 8 interactive pipeline stages:
  1. Trigger & Room Context: What set off this run (news fact, story hash, or clock mode).
  2. Candidate Generation & Causal Filter: Succession tree from lead player to room successors.
  3. Tier Quality Screen (PQI): Q-score in [0, 1], T1-T4 classifications, replacement floor & no-downgrade checks.
  4. Drop Pool & Cut Cost: Evaluation of drop candidates (hold_mean, hold_tail, lottery protection).
  5. LLM Dead-Heat Arbitration: Near-tie LLM evaluations (Ollama qualitative rationales, confidence, verdicts).
  6. Multi-Bar Simulator Screens: Marginal lineup gain vs SE, bench ceiling, claim-over-free, direct ROS veto.
  7. FAAB Pricing & Priority Ladder: Opponent demand forecast, utility curves, shadow price of FAAB.
  8. Execution & Portfolio State: Submit gate verification, 90m kickoff blackout, Sleeper queue status.
  9. Raw Evidence Inspector: Full unredacted JSON records and field definitions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from robo import decision_audit, evidence, narrate, news_audit, newswatch, ui, ui_player_card

st.title("📋 Moves & Waivers Pipeline Audit")
ui.gate_banner(st)

# Top Bar: Global Search & Deep-Link Handlers
col_s1, col_s2 = st.columns([3, 1])
with col_s1:
    ui_player_card.render_player_search_bar(key="moves_player_search")
ui_player_card.check_query_params_player()


@st.cache_data(ttl=10, show_spinner=False)
def _load_runs():
    return decision_audit.events(limit=200)


@st.cache_data(ttl=10, show_spinner=False)
def _load_pulse_state():
    return news_audit.state()


@st.cache_data(ttl=600, show_spinner=False)
def _name_fallback() -> dict:
    from robo import expected
    return {str(pid): {"name": r.get("name") or str(pid), "pos": r.get("pos"), "team": r.get("team")}
            for pid, r in (expected.load().get("players") or {}).items()}


def _parse_trigger_change(e: dict, c: dict, idx: dict | None = None) -> dict:
    """Parse raw trigger details into clean data-source-specific table columns.

    Categorical and scalar transitions (status, availability, projections, depth chart)
    populate 'before' and 'after'. Prose updates (Sleeper news, ESPN reports, PFT)
    parse the headline and story content into separate dedicated fields, leaving
    before/after as '—' instead of exposing internal cryptographic hashes.
    """
    field = c.get("field") or "event"
    b_raw = c.get("before")
    a_raw = c.get("after")
    h = c.get("headline") or {}

    before, after = "—", "—"
    headline, story = "—", "—"

    if field == "sleeper_news_content":
        title = h.get("title")
        src = f" [{h['source']}]" if h.get("source") else ""
        headline = f"{title}{src}" if title else ("(headline not recorded)" if b_raw or a_raw else "—")
        story = h.get("description") or "—"
        # Content hashes are internal verification keys; no human value in before/after
        before, after = "—", "—"

    elif field == "status":
        before = str(b_raw) if b_raw else "Active"
        after = str(a_raw) if a_raw else "Active"

    elif field == "espn_availability":
        before = str(b_raw).title() if b_raw else "Active"
        after = str(a_raw).title() if a_raw else "Active"

    elif field in ("espn_short", "espn_long"):
        headline = "ESPN Short Report" if field == "espn_short" else "ESPN Story & Analysis"
        story = str(a_raw) if a_raw else "—"
        before, after = "—", "—"

    elif field == "pft_report":
        pft_items = e.get("pft") or []
        if pft_items:
            item = pft_items[0]
            headline = f"{item.get('title')} [PFT]" if item.get("title") else "PFT Rumor Mill Report"
            story = item.get("description") or str(a_raw) or "—"
        else:
            headline = "PFT Rumor Mill Report"
            story = str(a_raw) if a_raw else "—"
        before, after = "—", "—"

    elif field == "weekly_projection":
        try:
            before = f"{float(b_raw):.1f} pts" if b_raw is not None else "—"
            after = f"{float(a_raw):.1f} pts" if a_raw is not None else "—"
        except (ValueError, TypeError):
            before, after = str(b_raw or "—"), str(a_raw or "—")

    elif field == "depth_order":
        before = f"#{b_raw}" if b_raw is not None else "—"
        after = f"#{a_raw}" if a_raw is not None else "—"

    elif field == "trending_top5":
        before = "—"
        after = "Top 5 Trending"

    elif field in ("espn_designation", "espn_body_part", "espn_return_date"):
        before = str(b_raw) if b_raw else "—"
        after = str(a_raw) if a_raw else "—"

    elif field == "espn_entry":
        if isinstance(a_raw, dict):
            headline = "Added to ESPN Report"
            story = f"{a_raw.get('name')} ({a_raw.get('team')}): {a_raw.get('designation') or 'Injured'}"
            before, after = "—", str(a_raw.get("designation") or "Listed")
        else:
            before, after = "—", str(a_raw or "Listed")

    elif field == "news_updated":
        try:
            before = news_audit.local_time(float(b_raw) / 1000) if b_raw else "—"
            after = news_audit.local_time(float(a_raw) / 1000) if a_raw else "—"
        except (TypeError, ValueError):
            before, after = "—", "—"

    else:
        if isinstance(b_raw, (dict, list)):
            before = json.dumps(b_raw, sort_keys=True)
        else:
            before = str(b_raw) if b_raw is not None else "—"
        if isinstance(a_raw, (dict, list)):
            after = json.dumps(a_raw, sort_keys=True)
        else:
            after = str(a_raw) if a_raw is not None else "—"

    signal_labels = {
        "sleeper_news_content": "Sleeper News",
        "status": "Injury Status",
        "espn_availability": "ESPN Availability",
        "espn_short": "ESPN Short Note",
        "espn_long": "ESPN Analysis",
        "pft_report": "PFT Report",
        "weekly_projection": "Weekly Proj",
        "depth_order": "Depth Chart",
        "trending_top5": "Trending Adds",
        "espn_designation": "ESPN Designation",
        "espn_body_part": "Injured Body Part",
        "espn_return_date": "Return Date",
        "espn_entry": "ESPN Entry",
        "news_updated": "News Timestamp",
    }
    signal_display = signal_labels.get(field, field)

    player_name = e.get("name")
    if not player_name and idx is not None:
        player_name = news_audit.label(e.get("player_id"), idx)
    if not player_name:
        player_name = str(e.get("player_id") or "")

    return {
        "player": player_name,
        "signal": signal_display,
        "signal_raw": field,
        "before": before,
        "after": after,
        "headline": headline,
        "story": story,
        "why counted": "; ".join(e.get("reasons") or []),
        "player_id": e.get("player_id"),
    }



runs = _load_runs()
if not runs:
    st.info("No decision runs recorded yet. Waiting for news pulse or scheduled moves pass.")
    st.stop()

pulse = _load_pulse_state()

# ------------------------------------------------------------- 1. Timeline & Selector
st.subheader("Decision Run Timeline")
r_cols = st.columns(4)
r_cols[0].metric("Last News Pulse", ui.fmt_age(pulse.get("last_poll")))
r_cols[1].metric("Last Decision Run", ui.fmt_age(runs[0].get("at")))
r_cols[2].metric("Total Runs Cached", len(runs))
r_cols[3].metric("Latest Outcome", runs[0]["outcome"])

filter_mode = st.radio(
    "Filter runs by trigger:",
    ["All Runs", "News Pulses", "Scheduled Clock Passes"],
    horizontal=True
)
filtered_runs = runs
if filter_mode == "News Pulses":
    filtered_runs = [d for d in runs if d["kind"] == decision_audit.NEWS]
elif filter_mode == "Scheduled Clock Passes":
    filtered_runs = [d for d in runs if d["kind"] == decision_audit.CLOCK]

if not filtered_runs:
    st.info(f"No decision runs recorded matching filter '{filter_mode}'.")
    st.stop()

# Select specific run
wanted_run = st.query_params.get("run")
run_keys = [d["fingerprint"] for d in filtered_runs]
run_labels = {
    d["fingerprint"]: f"{news_audit.local_time(d.get('at'))} · {d['kind'].upper()} ({d.get('mode') or 'pulse'}) · {d['outcome']}"
    for d in filtered_runs
}
default_idx = run_keys.index(wanted_run) if wanted_run in run_keys else 0
chosen_fp = st.selectbox("Select a Decision Run to Audit Front-to-Back:", run_keys, index=default_idx, format_func=run_labels.get)
doc = next((d for d in filtered_runs if d["fingerprint"] == chosen_fp), filtered_runs[0])
raw = doc.get("raw") or {}
idx = news_audit.player_index(raw, _name_fallback())

# ------------------------------------------------------------- 2. Run Executive Summary
st.divider()
st.subheader(f"Run Summary: {news_audit.local_time(doc.get('at'))}")
slate = decision_audit.slate(doc)
if slate:
    st.success(f"**This run proposed {len(slate)} transaction(s):**")
    df_slate = pd.DataFrame([{
        "channel": r.get("channel"),
        "add": r.get("add"),
        "drop": r.get("drop") or "(open spot)",
        "bid": r.get("bid"),
        "gain": r.get("gain"),
        "ceiling": r.get("ceiling"),
        "player_id": r.get("add_id") or (r.get("raw") or {}).get("add", {}).get("player_id"),
    } for r in slate])
    ev_slate = st.dataframe(
        df_slate[["channel", "add", "drop", "bid", "gain", "ceiling"]],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="slate_table",
        column_config={
            "bid": st.column_config.NumberColumn("FAAB Bid", format="$%d"),
            "gain": st.column_config.NumberColumn("Lineup Gain", format="%+.2f pts"),
            "ceiling": st.column_config.NumberColumn("Ceiling", format="%.2f pts"),
        }
    )
    ui_player_card.attach_player_selection(df_slate, ev_slate, id_col="player_id", week=doc.get("week"))
else:
    st.info(f"**This run proposed no moves.** {narrate.slate_absence(doc)}")

with st.expander("Plain English Narrative of this Run"):
    for p in narrate.run_story(doc):
        st.write(p)

# ------------------------------------------------------------- 3. 8-Stage Pipeline Trace
st.divider()
st.markdown("### 🔍 Front-to-Back Pipeline Trace")
st.caption("Walk through each stage of the decision engine from raw trigger to Sleeper GraphQL submission.")

tabs = st.tabs([
    "1·Trigger",
    "2·Candidates",
    "3·Tier Quality (PQI)",
    "4·Drop Pool",
    "5·LLM Arbitration",
    "6·Sim Screens",
    "7·Bidding Ladder",
    "8·Execution",
    "9·Raw Evidence"
])

# STAGE 1: TRIGGER
with tabs[0]:
    st.markdown("#### Stage 1: Trigger & Room Context")
    st.caption(doc.get("trigger_story") or "Trigger details:")
    if doc["kind"] == decision_audit.NEWS:
        trigger_rows = []
        for e in doc.get("trigger") or []:
            for c in e.get("changes") or [{"field": "event", "before": None, "after": None}]:
                trigger_rows.append(_parse_trigger_change(e, c, idx))
        if trigger_rows:
            df_trig = pd.DataFrame(trigger_rows)
            display_cols = ["player", "signal", "before", "after", "headline", "story", "why counted"]
            ev_trig = st.dataframe(
                df_trig[display_cols],
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="trigger_table",
                row_height=65,
                column_config={
                    "player": st.column_config.TextColumn("Player", width="small"),
                    "signal": st.column_config.TextColumn("Signal", width="small", help="Underlying feed or telemetry triggering this event"),
                    "before": st.column_config.TextColumn("Before", width="small", help="Prior state for status, availability, projection, or depth chart changes"),
                    "after": st.column_config.TextColumn("After", width="small", help="Updated state for status, availability, projection, or depth chart changes"),
                    "headline": st.column_config.TextColumn("Headline", width="medium", help="Parsed report headline or title"),
                    "story": st.column_config.TextColumn("Story Content", width="large", help="Parsed story summary and prose reporting"),
                    "why counted": st.column_config.TextColumn("Why Counted", width="medium", help="The decision engine rule that validated this event"),
                }
            )
            ui_player_card.attach_player_selection(df_trig, ev_trig, id_col="player_id", week=doc.get("week"))

        timing = doc.get("timing") or {}
        st.markdown("**Deterministic Timing & Prose Bounds:**")
        tc = st.columns(4)
        tc[0].metric("Dated by Rule", len(timing.get("deterministic") or []))
        reads = decision_audit.prose_reads(doc)
        tc[1].metric("Prose Read", len(reads.get("mine") or []))
        tc[2].metric("Quarantined", len(timing.get("quarantined") or {}))
        tc[3].metric("Retired", len(timing.get("cleared") or []))
        if timing.get("quarantined"):
            st.warning(" · ".join(f"{news_audit.label(pid, idx)}: {why}" for pid, why in timing["quarantined"].items()))
    else:
        st.info(f"Scheduled Pass Mode: `{doc.get('mode')}`. {decision_audit.MODE_STORY.get(doc.get('mode'), '')}")

# STAGE 2: CANDIDATES & CAUSAL FILTER
with tabs[1]:
    st.markdown("#### Stage 2: Candidates & Room Succession")
    st.caption("Causal succession: what players in the affected position room moved in value and qualify as acquisition targets.")
    deltas = doc.get("event_deltas") or {}
    if deltas:
        provider = doc.get("provider_at_trigger") or {}
        d_rows = []
        for pid, d in deltas.items():
            edge = str(d.get("causal_edge") or "")
            relation = ("Trigger himself" if edge.startswith("self:")
                        else f"Inherits from {news_audit.label(d.get('lead_id'), idx)}"
                        if edge.startswith("successor-of:") else "Same room (no causal edge)")
            d_rows.append({
                "player": d.get("name") or news_audit.label(pid, idx),
                "pos": d.get("pos") or (idx.get(pid) or {}).get("pos"),
                "team": d.get("team") or (idx.get(pid) or {}).get("team"),
                "relation": relation,
                "acquirable": "✅ Yes" if d.get("acquisition_candidate") else "❌ No",
                "val_before": d.get("pre_ros"),
                "val_after": d.get("post_ros"),
                "delta_ros": d.get("delta_ros"),
                "player_id": pid,
            })
        df_deltas = pd.DataFrame(d_rows)
        ev_deltas = st.dataframe(
            df_deltas[["player", "pos", "team", "relation", "acquirable", "val_before", "val_after", "delta_ros"]],
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="candidates_table",
            column_config={
                "val_before": st.column_config.NumberColumn("ROS Before", format="%.1f"),
                "val_after": st.column_config.NumberColumn("ROS After", format="%.1f"),
                "delta_ros": st.column_config.NumberColumn("Δ ROS", format="%+.2f"),
            }
        )
        ui_player_card.attach_player_selection(df_deltas, ev_deltas, id_col="player_id", week=doc.get("week"))
    else:
        st.caption("No event deltas recorded (clock run evaluated all wire candidates directly).")

# STAGE 3: TIER QUALITY (PQI)
with tabs[2]:
    st.markdown("#### Stage 3: Tier Quality Calculations (PQI)")
    st.caption(
        "Proposal 2A: The Player Quality Index enforces discrete tiers (T1_BREAKOUT, T2_CONTRIBUTOR, T3_SPECULATIVE, T4_REPLACEMENT). "
        "Two ironclad rules: (1) Cannot cut a rostered player for a T4 replacement (Q < 0.25); (2) Cannot downgrade quality tier (e.g. drop T2 for T3)."
    )
    cands = doc.get("candidates") or []
    q_screen_rows = []
    for c in cands:
        screens = c.get("screens") or []
        for s in screens:
            if s.get("name") == "quality_tier":
                q_screen_rows.append({
                    "add": c.get("name"),
                    "pos": c.get("pos"),
                    "drop": c.get("drop_name") or "(open spot)",
                    "verdict": s.get("verdict"),
                    "status": "❌ Refused" if s.get("refused") else "✅ Cleared",
                    "margin_to_floor": s.get("margin"),
                    "detail": s.get("detail"),
                    "player_id": c.get("player_id"),
                })
    if q_screen_rows:
        df_q = pd.DataFrame(q_screen_rows)
        ev_q = st.dataframe(
            df_q[["add", "pos", "drop", "status", "verdict", "margin_to_floor", "detail"]],
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="quality_tier_table",
            column_config={
                "margin_to_floor": st.column_config.NumberColumn("Margin (Q - 0.25)", format="%+.2f"),
                "detail": st.column_config.TextColumn("Quality Evaluation Detail", width="large"),
            }
        )
        ui_player_card.attach_player_selection(df_q, ev_q, id_col="player_id", week=doc.get("week"))
    else:
        st.info("No quality tier screen evaluations recorded in this run.")

# STAGE 4: DROP POOL EVALUATION
with tabs[3]:
    st.markdown("#### Stage 4: Drop Side & Cut Cost Evaluation")
    st.caption("Evaluation of active roster assets to determine legally droppable players, cut costs (hold_mean), and lottery ticket protection.")
    drops = doc.get("drop_checks") or []
    if drops:
        drop_rows = []
        for r in drops:
            drop_rows.append({
                "player": r.get("drop_name") or r.get("name") or news_audit.label(r.get("drop_id") or r.get("player_id"), idx),
                "pos": r.get("drop_pos") or r.get("pos"),
                "eligible": "✅ Yes" if r.get("eligible") else "❌ No",
                "cut_cost": r.get("drop_ros", r.get("drop_price")),
                "reason": r.get("reason") or "Eligible for drop",
                "player_id": r.get("drop_id") or r.get("player_id"),
            })
        df_drops = pd.DataFrame(drop_rows)
        ev_drops = st.dataframe(
            df_drops[["player", "pos", "eligible", "cut_cost", "reason"]],
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="drops_table",
            column_config={
                "cut_cost": st.column_config.NumberColumn("Cut Cost (ROS pts)", format="%.2f"),
            }
        )
        ui_player_card.attach_player_selection(df_drops, ev_drops, id_col="player_id", week=doc.get("week"))
    else:
        st.caption("No drop checks recorded (moves evaluated against an open roster spot).")

# STAGE 5: LLM DEAD-HEAT ARBITRATION
with tabs[4]:
    st.markdown("#### Stage 5: Qualitative LLM Dead-Heat Arbitration")
    st.caption(
        "Proposal 2B: When quantitative modeling yields near-zero difference (gain <= 1.5 pts or ROS diff <= 5.0 pts) in the same tier, "
        "local Ollama (qwen3.8:27b-mtp-96k) arbitrates beat reporting to protect incumbents against lateral churn."
    )
    arb_found = False
    for c in cands:
        for s in (c.get("screens") or []):
            if s.get("name") == "llm_arbitration":
                arb_found = True
                st.markdown(f"**Proposal:** Add **{c.get('name')}** vs Drop **{c.get('drop_name')}**")
                st.markdown(f"**Screen Verdict:** `{s.get('verdict')}` (Refused: `{s.get('refused')}`)")
                st.write(s.get("detail", ""))
                st.divider()

    if not arb_found:
        st.info("No near-tie moves in this run triggered qualitative LLM dead-heat arbitration.")

# STAGE 6: SIMULATOR SCREENS
with tabs[5]:
    st.markdown("#### Stage 6: Multi-Bar Simulator Screens")
    st.caption("Evaluates each candidate across simulation noise margin (gain >= 2.0x SE), bench ceiling bar, claim-over-free, and direct ROS veto.")
    if cands:
        sim_rows = []
        for c in cands:
            all_screens = " · ".join(s.get("verdict") or "" for s in (c.get("screens") or []))
            sim_rows.append({
                "add": c.get("name"),
                "pos": c.get("pos"),
                "drop": c.get("drop_name") or "(open spot)",
                "channel": c.get("channel"),
                "status": c.get("status"),
                "gain": c.get("gain"),
                "se": c.get("se"),
                "ceiling": c.get("ceiling"),
                "screens_verdict": all_screens or c.get("native"),
                "plain_english": narrate.option_story(c, doc.get("thresholds")),
                "player_id": c.get("player_id"),
            })
        df_sim = pd.DataFrame(sim_rows)
        ev_sim = st.dataframe(
            df_sim[["add", "pos", "drop", "channel", "status", "gain", "se", "ceiling", "screens_verdict", "plain_english"]],
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="sim_screens_table",
            column_config={
                "gain": st.column_config.NumberColumn("Gain", format="%+.2f pts"),
                "se": st.column_config.NumberColumn("SE", format="%.2f"),
                "ceiling": st.column_config.NumberColumn("Ceiling", format="%.2f pts"),
                "plain_english": st.column_config.TextColumn("Plain English Verdict", width="large"),
            }
        )
        ui_player_card.attach_player_selection(df_sim, ev_sim, id_col="player_id", week=doc.get("week"))

# STAGE 7: FAAB PRICING & BIDDING LADDER
with tabs[6]:
    st.markdown("#### Stage 7: FAAB Pricing & Priority Ladder")
    claims = doc.get("claims") or []
    if claims:
        st.caption("A slate is a ladder: Sleeper reaches the rungs in bid order and the first winner takes the slot.")
        for claim in claims:
            add = claim.get("add") or {}
            q = claim.get("bid_quote") or {}
            field = claim.get("opponent_field") or {}
            st.markdown(f"##### Rung {claim.get('priority', 0)} · {add.get('name')} · Bid: \\${claim.get('bid', 0)}")
            bc = st.columns(4)
            bc[0].metric("Lineup Worth", f"{float(claim.get('bid_gain') or 0):+.2f} pts")
            bc[1].metric("Win Probability", f"{float(q.get('p_win') or 0):.0%}")
            bc[2].metric("Expected High Rival", f"${float(q['expected_highest']):.1f}" if q.get("expected_highest") is not None else "Pooled")
            band = q.get("near_optimal")
            if not isinstance(band, (list, tuple)) or len(band) < 2:
                b_val = claim.get("bid", 0)
                band = [b_val, b_val]
            bc[3].metric("Optimal Band", f"${band[0]}–${band[1]}")

            if q.get("curve"):
                curve = pd.DataFrame(q["curve"])
                u_col = "expected_utility" if "expected_utility" in curve.columns else ("utility" if "utility" in curve.columns else None)
                if "bid" in curve.columns and u_col:
                    chart_df = curve.set_index("bid")[[u_col]].rename(columns={u_col: "Expected Utility"})
                    st.line_chart(chart_df, height=180)
    else:
        st.caption("No FAAB claims built in this run (free agent adds require no bid).")

# STAGE 8: EXECUTION & SLEEPER QUEUE
with tabs[7]:
    st.markdown("#### Stage 8: Execution & Sleeper Verification")
    ec = st.columns(4)
    ec[0].metric("Submission Mode", "Dry Run" if raw.get("dry_run") else ("Gated" if doc.get("gated") else "Live"))
    ec[1].metric("Submitted Claims", len(doc.get("submitted") or []) if doc.get("submission_recorded") else "—")
    ec[2].metric("Source Failures", len(doc.get("source_errors") or []))
    ec[3].metric("FAAB Remaining", f"${doc.get('faab_left', 0)}")
    st.write(narrate.gate_sentence(doc))

# STAGE 9: RAW EVIDENCE
with tabs[8]:
    st.markdown("#### Stage 9: Raw Immutable Evidence")
    fields = evidence.explain_fields(raw)
    df_fields = pd.DataFrame(fields)
    st.dataframe(df_fields[["path", "value", "meaning"]], use_container_width=True, hide_index=True)
    with st.expander("Raw JSON"):
        st.json(raw, expanded=False)
