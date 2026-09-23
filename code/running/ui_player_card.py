"""Universal Interactive Player Card (Dossier) for the Roboner Audit GUI.

Provides an exhaustive, front-to-back modal inspector (via @st.dialog) or inline
viewer for any player across any screen. Displays all actual and derived metrics:
  - Identity, bio, depth chart, injury, and acquisition/roster state
  - Player Quality Index (PQI): Q-score in [0, 1], T1-T4 tiers, and 6 components
  - Drop valuation & lottery-ticket protection (hold_mean, hold_tail, p_matters)
  - Weekly Monte Carlo simulation distribution (Mean, Median, P10/P90, P(10+), P(20+))
  - Rest-of-Season (ROS) pathway, role inheritance, and weekly contribution waterfall
  - LLM qualitative scouting (Codex/Qwen unredacted reporting, return bounds)
  - LLM dead-heat arbitration logs (incumbent vs challenger, confidence, rationale)
  - Market buzz velocity (72h adds/drops) and ADP pedigree
  - Exact mathematical traceback from expected.trace()
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from robo import DATA, season, ui


# ---------------------------------------------------------------- Data Loaders

@st.cache_data(ttl=300, show_spinner=False)
def _load_player_core(pid: str) -> dict[str, Any]:
    from robo import sleeper_read
    players = sleeper_read.players()
    p = players.get(str(pid)) or {}
    return {
        "player_id": str(pid),
        "name": sleeper_read.player_name(players, str(pid)),
        "pos": p.get("position") or "DEF",
        "team": p.get("team") or "FA",
        "age": p.get("age"),
        "years_exp": p.get("years_exp"),
        "depth_chart_order": p.get("depth_chart_order"),
        "depth_chart_position": p.get("depth_chart_position"),
        "injury_status": p.get("injury_status"),
        "injury_body_part": p.get("injury_body_part"),
        "injury_notes": p.get("injury_notes"),
        "injury_start_date": p.get("injury_start_date"),
        "status": p.get("status"),
    }


@st.cache_data(ttl=60, show_spinner=False)
def _load_acquisition_state(pid: str, week: int | None = None) -> dict[str, Any]:
    """Determine live league availability: mine (starter/bench/ir), free, waivers, or opponent."""
    pid = str(pid)
    try:
        m = season.mine()
        starters = set(str(x) for x in (m.get("starters") or []))
        reserve = set(str(x) for x in (m.get("reserve") or []))
        mine = set(str(x) for x in (m.get("players") or []))
        rostered = set(str(x) for x in season.rostered_ids())
    except Exception:
        return {"status": "unknown", "label": "Unknown", "color": "gray", "detail": ""}

    if pid in starters:
        return {"status": "mine_starter", "label": "My Lineup (Starter)", "color": "green", "detail": "Starting on Roboner's roster"}
    if pid in reserve:
        return {"status": "mine_ir", "label": "My Roster (IR / Reserve)", "color": "blue", "detail": "Parked on Roboner's injured reserve"}
    if pid in mine:
        return {"status": "mine_bench", "label": "My Roster (Bench)", "color": "blue", "detail": "Active bench asset on Roboner's roster"}
    if pid in rostered:
        return {"status": "rostered", "label": "Rostered (Opponent)", "color": "orange", "detail": "Held on an opposing team's roster"}

    try:
        states = season.transaction_states([pid], week=week)
        st_ = states.get(pid) or {}
        acq = st_.get("acquisition")
        reason = st_.get("reason") or st_.get("unlock_basis") or ""
        unlock_at = st_.get("unlock_at")
        time_str = ""
        if unlock_at:
            dt = datetime.fromtimestamp(float(unlock_at), season.PHOENIX)
            time_str = f"clears {dt:%a %H:%M}"
        if acq == "free_now":
            return {"status": "free_now", "label": "Free Agent (Free Now)", "color": "green",
                    "detail": "Can be added outright for $0 with no wait"}
        if acq in {"weekly_waiver", "drop_waiver"}:
            label = f"On Waivers ({time_str})" if time_str else "On Waivers"
            return {"status": "on_waivers", "label": label, "color": "violet",
                    "detail": f"Costs FAAB bid; settles on waiver run ({reason})"}
    except Exception:
        pass
    return {"status": "unrostered", "label": "Unrostered", "color": "gray", "detail": "Not on any active roster"}


@st.cache_data(ttl=300, show_spinner=False)
def _load_quality_data(pid: str, week: int | None = None) -> dict[str, Any]:
    try:
        from robo import quality
        return quality.score(str(pid), week=week)
    except Exception as e:
        return {"q": 0.0, "tier": "T4_REPLACEMENT", "components": {}, "reason": f"Quality unavailable: {e}"}


@st.cache_data(ttl=300, show_spinner=False)
def _load_expected_data(pid: str) -> dict[str, Any]:
    try:
        from robo import expected
        table = expected.load()
        row = (table.get("players") or {}).get(str(pid)) or {}
        return {"table_meta": {k: v for k, v in table.items() if k != "players"}, "row": row}
    except Exception:
        return {"table_meta": {}, "row": {}}


@st.cache_data(ttl=300, show_spinner=False)
def _load_weekly_model_data(pid: str, week: int | None = None) -> dict[str, Any]:
    try:
        from robo import model_proj
        art, prov = model_proj.load()
        players = art.get("players") or {} if art else {}
        row = players.get(str(pid)) or {}
        return {"model": row, "provenance": prov}
    except Exception:
        return {"model": {}, "provenance": ""}


@st.cache_data(ttl=300, show_spinner=False)
def _load_drop_metrics(pid: str, week: int | None = None) -> dict[str, Any]:
    try:
        from robo import season, value
        mine = set(str(p) for p in (season.mine().get("players") or []))
        if str(pid) not in mine:
            return {}
        from robo import expected
        table = expected.load()
        row = (table.get("players") or {}).get(str(pid)) or {"player_id": str(pid)}
        sh = value.hold_shape(row, week or season.current_week())
        return sh or {}
    except Exception:
        return {}


@st.cache_data(ttl=600, show_spinner=False)
def _load_scout_verdict(pid: str) -> dict[str, Any]:
    path = DATA / "news_verdicts.json"
    if not path.is_file():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return (d.get("verdicts") or {}).get(str(pid)) or {}
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def _load_arbitrations_for_player(pid: str) -> list[dict[str, Any]]:
    path = DATA / "dead_heat_arbitrations.json"
    if not path.is_file():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        pid = str(pid)
        from robo import sleeper_read
        players = sleeper_read.players()
        out = []
        for key, rec in d.items():
            parts = key.split("_")
            if len(parts) >= 2:
                add_id, drop_id = parts[0], parts[1]
                if pid in (add_id, drop_id):
                    out.append({
                        "key": key,
                        "role": "Candidate (Add)" if pid == add_id else "Incumbent (Drop)",
                        "counterpart_id": drop_id if pid == add_id else add_id,
                        "counterpart_name": sleeper_read.player_name(players, drop_id if pid == add_id else add_id),
                        "week": rec.get("week"),
                        "verdict": rec.get("verdict"),
                        "confidence": rec.get("confidence"),
                        "reason": rec.get("reason"),
                        "source": rec.get("source"),
                        "time": rec.get("time"),
                    })
        out.sort(key=lambda x: float(x.get("time") or 0.0), reverse=True)
        return out
    except Exception:
        return []


@st.cache_data(ttl=600, show_spinner=False)
def _load_buzz(pid: str) -> dict[str, Any]:
    path = DATA / "buzz.json"
    if not path.is_file():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        counts = (d.get("counts") or {}).get(str(pid)) or {}
        adds = int(counts.get("adds", 0))
        drops = int(counts.get("drops", 0))
        return {"adds": adds, "drops": drops, "net": adds - drops, "updated": d.get("updated")}
    except Exception:
        return {}


@st.cache_data(ttl=600, show_spinner=False)
def _load_trace(pid: str) -> str:
    try:
        from robo import expected
        return expected.trace(player_id=str(pid))
    except Exception as e:
        return f"Trace unavailable: {e}"


# ----------------------------------------------------------- Visual Component

def render_player_card(player_id: str, week: int | None = None) -> None:
    """Render the complete player dossier inline."""
    pid = str(player_id)
    core = _load_player_core(pid)
    acq = _load_acquisition_state(pid, week=week)
    q_data = _load_quality_data(pid, week=week)
    exp = _load_expected_data(pid)
    row = exp.get("row") or {}
    meta = exp.get("table_meta") or {}
    m_data = _load_weekly_model_data(pid, week=week)
    model = m_data.get("model") or {}
    drop_m = _load_drop_metrics(pid, week=week)
    verdict = _load_scout_verdict(pid)
    arbitrations = _load_arbitrations_for_player(pid)
    buzz_data = _load_buzz(pid)

    # 1. Header Banner
    col_h1, col_h2 = st.columns([3, 2])
    with col_h1:
        st.subheader(f"{core['name']} · {core['pos']} · {core['team']}")
        desc_bits = []
        if core.get("depth_chart_order"):
            pos_label = core.get("depth_chart_position") or core["pos"]
            desc_bits.append(f"**{pos_label}{core['depth_chart_order']}** on depth chart")
        if core.get("age"):
            desc_bits.append(f"Age {core['age']}")
        if core.get("years_exp") is not None:
            desc_bits.append("Rookie" if core["years_exp"] == 0 else f"{core['years_exp']}y exp")
        st.caption(" · ".join(desc_bits) if desc_bits else "Active NFL Asset")

        # Injury Tag
        inj = core.get("injury_status")
        if inj:
            inj_note = core.get("injury_notes") or core.get("injury_body_part") or "Reported injury"
            st.error(f"🚑 **{inj.upper()}**: {inj_note}")

    with col_h2:
        tier = q_data.get("tier", "T4_REPLACEMENT")
        tier_colors = {
            "T1_BREAKOUT": "#1baf7a",
            "T2_CONTRIBUTOR": "#2a78d6",
            "T3_SPECULATIVE": "#eb6834",
            "T4_REPLACEMENT": "#7d8799",
        }
        st.markdown(
            f"<div style='border: 1px solid #444; border-radius: 8px; padding: 10px; text-align: right; background-color: rgba(0,0,0,0.15);'>"
            f"<div style='font-size: 0.85em; color: #888;'>STATUS & TIER</div>"
            f"<div style='font-size: 1.1em; font-weight: bold;'>{acq['label']}</div>"
            f"<div style='font-size: 1.0em; color: {tier_colors.get(tier, '#aaa')}; font-weight: 600;'>{tier} · Q={q_data.get('q', 0.0):.2f}</div>"
            f"</div>",
            unsafe_allow_html=True
        )

    # 2. Key Metrics Strip
    m_cols = st.columns(5)
    ros_val = float(row.get("ros") or 0.0)
    raw_val = float(row.get("raw") or 0.0)
    wk_mean = float(model.get("mean") or 0.0)
    cur_wk = week or season.current_week()
    wk_cell = (row.get("by_week") or {}).get(str(cur_wk)) or {}
    ros_wk_pts = float(wk_cell.get("final") or wk_cell.get("pts") or 0.0)
    s2_wk_pts = float(wk_cell.get("s2") or 0.0)
    lead_name = row.get("lead_of")

    m_cols[0].metric("Quality Score (Q)", f"{q_data.get('q', 0.0):.2f}", tier)
    m_cols[1].metric("Rest of Season", f"{ros_val:.1f}", f"raw {raw_val:.1f}")
    if wk_mean > 0:
        m_cols[2].metric("Week Model Mean", f"{wk_mean:.2f} pts")
    elif ros_wk_pts > 0:
        m_cols[2].metric(f"Wk {cur_wk} ROS Value", f"{ros_wk_pts:.2f} pts", f"Inherited S2 ({lead_name})" if s2_wk_pts > 0 else "ROS Value")
    else:
        m_cols[2].metric("Week Model Mean", "—", "Unprojected")

    if drop_m:
        m_cols[3].metric("Cut Cost (Mean)", f"{drop_m.get('mean', 0.0):.2f} pts")
        m_cols[4].metric("Worst World (P10)", f"{drop_m.get('tail', 0.0):.2f} pts")
    else:
        m_cols[3].metric("Rival Bid Mult", f"{q_data.get('rival_bid_mult', 1.0):.2f}x")
        m_cols[4].metric("Option Value", f"+{q_data.get('option_value', 0.0):.1f} pts")

    st.divider()

    # 3. Comprehensive Tabs
    tab_labels = [
        "🎯 Tier Quality (PQI)",
        "📊 Weekly Projections",
        "📈 Rest of Season Pathway",
        "🤖 LLM Scouting & Arbitrations",
        "🔍 Full Traceback"
    ]
    tabs = st.tabs(tab_labels)

    # --- TAB 1: Quality Tier (PQI) ---
    with tabs[0]:
        st.markdown("#### Player Quality Index (PQI) Breakdown")
        st.caption(
            "Transforms talent, weekly mean, ceiling, rest-of-season expectation, buzz, and draft capital into a normalized "
            "score Q in [0.0, 1.0] and discrete quality tiers. Governs market rivalry, asset option value, and drop protection."
        )
        q_val = float(q_data.get("q", 0.0))
        st.progress(q_val, text=f"Composite Quality Score: Q = {q_val:.4f} ({tier})")
        st.info(q_data.get("reason", ""))

        comps = q_data.get("components") or {}
        st.markdown("**Component Weights & Scores:**")
        c1, c2, c3 = st.columns(3)
        c1.metric("Weekly Projection (25%)", f"{comps.get('proj', 0.0):.2f}")
        c1.metric("Rest of Season (20%)", f"{comps.get('ros', 0.0):.2f}")
        c2.metric("Distribution Ceiling (15%)", f"{comps.get('ceil', 0.0):.2f}")
        c2.metric("Buzz Velocity (20%)", f"{comps.get('buzz', 0.0):.2f}")
        c3.metric("Role & Inheritance (15%)", f"{comps.get('role', 0.0):.2f}")
        c3.metric("Draft Pedigree (5%)", f"{comps.get('pedigree', 0.0):.2f}")

        st.markdown("**Drop Rules & Protection Status:**")
        if drop_m:
            from robo import moves
            bar = moves.TICKET_PROTECT_POINTS
            p10 = drop_m.get("tail", 0.0)
            if drop_m.get("starts_median"):
                st.success("✅ **Starter Protection:** Starts in the median world. Cannot be cut for casual churn.")
            elif p10 >= bar:
                st.warning(f"🛡️ **Lottery Ticket Protected:** P10 downside of {p10:.1f} >= {bar:.1f} threshold. Blocked from drop pool.")
            else:
                st.info(f"⚖️ **Droppable Asset:** P10 downside of {p10:.1f} < {bar:.1f} bar. Cut cost = {drop_m.get('mean', 0.0):.2f} pts.")
        elif acq["status"] in ("free_now", "on_waivers"):
            if tier == "T4_REPLACEMENT":
                st.warning("⚠️ **Replacement Tier Floor:** This player is T4 (Q < 0.25). The bot refuses to drop any active rostered asset to add him.")
            else:
                st.success(f"✅ **Acquisition Candidate:** Clears replacement floor ({tier}). Can be targeted as an upgrade.")
        else:
            st.caption("Asset held on an opposing roster.")

    # --- TAB 2: Weekly Modeling & Matchup ---
    with tabs[1]:
        st.markdown("#### Weekly Monte Carlo Distribution")
        st.caption(
            "Scored from 4,000 simulated stat lines under our league's exact 57 scoring keys (including bonuses and sacks). "
            "Compared against Sleeper's raw weekly projection feed."
        )
        if model:
            w1, w2, w3, w4 = st.columns(4)
            w1.metric("Simulated Mean", f"{float(model.get('mean', 0.0)):.2f}")
            p50_val = model.get("p50") if "p50" in model else model.get("median", 0.0)
            w2.metric("Median (P50)", f"{float(p50_val or 0.0):.2f}")
            w3.metric("Floor (P10)", f"{float(model.get('p10', 0.0)):.2f}")
            w4.metric("Ceiling (P90)", f"{float(model.get('p90', 0.0)):.2f}")

            if "p25" in model and "p75" in model:
                st.markdown("**Simulation Quantiles:**")
                q1, q2, q3, q4, q5 = st.columns(5)
                q1.metric("Floor (P10)", f"{float(model.get('p10', 0.0)):.1f}")
                q2.metric("Q1 (P25)", f"{float(model.get('p25', 0.0)):.1f}")
                q3.metric("Median (P50)", f"{float(p50_val or 0.0):.1f}")
                q4.metric("Q3 (P75)", f"{float(model.get('p75', 0.0)):.1f}")
                q5.metric("Ceiling (P90)", f"{float(model.get('p90', 0.0)):.1f}")

            milestones = [t for t in [10, 15, 20, 25] if model.get(f"prob_{t}") is not None]
            if milestones:
                st.markdown("**Probability Milestones:**")
                p_cols = st.columns(len(milestones))
                for i, threshold in enumerate(milestones):
                    p_cols[i].metric(f"P({threshold}+ pts)", f"{float(model[f'prob_{threshold}']):.1%}")

            prov = m_data.get("provenance")
            if prov:
                st.caption(f"Model Provenance: `{prov}`")
        else:
            if s2_wk_pts > 0:
                st.warning(
                    f"⚠️ **Backup on Sleeper Feed with Inherited Starter Volume (S2):**\n\n"
                    f"- **Weekly Lineup Simulator (S1):** `0.0 pts`. Sleeper's raw weekly projection feed has not yet assigned standalone touches to **{core['name']}** (listed as {core.get('depth_chart_position', core['pos'])}{core.get('depth_chart_order', 2)}).\n"
                    f"- **Causal Room Inheritance (S2):** `{s2_wk_pts:.2f} pts` in Week {cur_wk}. Starter **{lead_name}** is designated OUT (P(miss) = 1.0). {core['name']} absorbs {float(row.get('absorbs') or 0):.0%} of vacated starter production.\n\n"
                    f"*The Rest-of-Season engine (Tab 3) factors this succession immediately for roster valuation, while the weekly 4,000-sim lineup engine reflects Sleeper's raw starter projection until updated.*"
                )
            else:
                st.info("No weekly simulation row generated for this player (either unprojected by Sleeper or projected opportunity rounded to zero).")

    # --- TAB 3: Rest of Season Pathway ---
    with tabs[2]:
        st.markdown("#### Rest of Season (ROS) Pathway")
        st.caption("Evaluated as: SUM_{w} [ Availability A(w) × (Current Role S1(w) + Inherited Opportunity S2(w)) × Playoff Weight W(w) ]")

        by = row.get("by_week") or {}
        if by:
            weeks = sorted(int(w) for w in by)
            wdf = pd.DataFrame([{
                "week": w,
                "available": by[str(w)].get("a", 0.0),
                "his role": by[str(w)].get("s1", 0.0),
                "if it opens": by[str(w)].get("s2", 0.0),
                "rank": by[str(w)].get("rank"),
                "weight": by[str(w)].get("weight", 1.0),
                "contributes": round(by[str(w)].get("final", 0.0) * float(by[str(w)].get("weight", 1.0)), 2),
            } for w in weeks])

            if s2_wk_pts > 0 and lead_name:
                st.success(
                    f"⚡ **Starter Succession Active in Week {cur_wk}:** Starter **{lead_name}** is designated OUT (P(miss) = 1.0). "
                    f"**{core['name']}** inherits `{s2_wk_pts:.2f} pts` (shown in `if it opens` / S2) representing {float(row.get('absorbs') or 0):.0%} of the starter role!"
                )

            c_chart, c_table = st.columns([3, 4])
            with c_chart:
                st.markdown("**Contribution by Week:**")
                chart = alt.Chart(wdf).mark_bar(color="#2a78d6").encode(
                    x=alt.X("week:O", title="Week"),
                    y=alt.Y("contributes:Q", title="Points Contributed"),
                    tooltip=["week", "contributes", "his role", "if it opens", "available", "weight"]
                ).properties(height=280)
                st.altair_chart(chart, use_container_width=True)
            with c_table:
                st.markdown("**Weekly Trajectory Breakdown:**")
                st.dataframe(wdf, use_container_width=True, hide_index=True, height=280)

            lead = row.get("lead_of")
            absorbs = row.get("absorbs")
            if lead:
                st.caption(f"🔗 Inherits **{float(absorbs or 0):.0%}** of **{lead}**'s opportunity if that role opens.")
        else:
            st.caption("No weekly ROS breakdown available.")

    # --- TAB 4: LLM Scouting & Arbitrations ---
    with tabs[3]:
        st.markdown("#### LLM Qualitative Intelligence")

        # Section A: Dead-heat Arbitrations
        st.markdown("##### Near-Tie / Dead-Heat Arbitrations")
        st.caption(
            "When quantitative models show near-zero delta (gain <= 1.5 or ROS diff <= 5.0) in the same tier, "
            "local LLM arbitration (qwen3.8:27b-mtp-96k) evaluates qualitative beat reporting to prevent lateral churn."
        )
        if arbitrations:
            for arb in arbitrations:
                with st.expander(f"Week {arb.get('week')} Arbitration vs {arb.get('counterpart_name')} — Verdict: {arb.get('verdict')}", expanded=True):
                    st.markdown(f"**Role:** {arb.get('role')} · **Opponent:** {arb.get('counterpart_name')}")
                    st.markdown(f"**Verdict:** `{arb.get('verdict')}` (Confidence: `{arb.get('confidence')}`)")
                    st.write(arb.get("reason", ""))
                    if arb.get("time"):
                        dt = datetime.fromtimestamp(float(arb["time"]))
                        st.caption(f"Judged at: {dt:%Y-%m-%d %H:%M:%S} via {arb.get('source')}")
        else:
            st.info("No dead-heat arbitrations recorded for this player.")

        st.divider()

        # Section B: Scout Prose Verdict
        st.markdown("##### Weekly Prose Scout Verdict")
        if verdict.get("reason"):
            v_color = "#1baf7a" if verdict.get("verdict") == "positive" else "#c2413b" if verdict.get("verdict") == "negative" else "#aaa"
            st.markdown(
                f"<span style='color:{v_color}; font-weight:bold; font-size:1.1em;'>"
                f"{verdict.get('verdict', '').upper()}</span> (Confidence: {verdict.get('confidence', '—')})",
                unsafe_allow_html=True
            )
            st.write(verdict.get("reason", ""))
            b_cols = st.columns(4)
            b_cols[0].metric("Return Week Min", verdict.get("return_week_min") or "—")
            b_cols[1].metric("Return Week Max", verdict.get("return_week_max") or "—")
            b_cols[2].metric("Return Basis", verdict.get("return_basis") or "—")
            b_cols[3].metric("Out for Season", "Yes" if verdict.get("out_for_season") else "No")
            if verdict.get("timing_sentence"):
                st.caption(f"Timing Sentence: *\"{verdict.get('timing_sentence')}\"*")
        else:
            st.caption("No weekly prose scout verdict recorded.")

    # --- TAB 5: Traceback ---
    with tabs[4]:
        st.markdown("#### Mathematical Derivation Traceback")
        st.caption("Complete file-by-file calculation derivation for this player's rest-of-season numbers.")
        ui.trace_block(st, _load_trace(pid))

        if buzz_data:
            st.markdown("##### Market Velocity & Crowd Agreement")
            b1, b2, b3 = st.columns(3)
            b1.metric("72h Adds", buzz_data.get("adds", 0))
            b2.metric("72h Drops", buzz_data.get("drops", 0))
            b3.metric("Net Buzz Velocity", buzz_data.get("net", 0))


# -------------------------------------------------------- Modal Dialog Wrapper

@st.dialog("Player Dossier", width="large")
def show_player_card(player_id: str, week: int | None = None) -> None:
    """Launch the modal dialog version of the player card."""
    render_player_card(player_id, week=week)


# ------------------------------------------------- Table Selection Integration

def attach_player_selection(
    df: pd.DataFrame,
    selection_event: Any,
    id_col: str = "player_id",
    week: int | None = None
) -> None:
    """Helper to detect row selection in st.dataframe(..., on_select='rerun') and open dialog."""
    if selection_event and hasattr(selection_event, "selection"):
        rows = getattr(selection_event.selection, "rows", [])
        if rows and len(rows) > 0:
            idx = rows[0]
            if 0 <= idx < len(df):
                pid = str(df.iloc[idx].get(id_col, ""))
                if pid:
                    show_player_card(pid, week=week)


def check_query_params_player(week: int | None = None) -> None:
    """Check if ?player=<id> is in query parameters and pop open dialog."""
    wanted = st.query_params.get("player")
    if wanted:
        show_player_card(str(wanted), week=week)


def render_player_search_bar(
    key: str = "global_player_search",
    placeholder: str = "🔍 Search any player to open full dossier...",
    week: int | None = None
) -> None:
    """Renders a fast search box that opens the player card modal."""
    from robo import sleeper_read
    players = sleeper_read.players()
    items = []
    for pid, p in players.items():
        pos = p.get("position")
        if pos in {"QB", "RB", "WR", "TE", "K", "DEF"}:
            name = sleeper_read.player_name(players, pid)
            team = p.get("team") or "FA"
            items.append((str(pid), f"{name} ({pos} · {team})"))
    items.sort(key=lambda x: x[1])
    opts = [""] + [pid for pid, _ in items]
    labels = {"": placeholder, **{pid: label for pid, label in items}}
    selected = st.selectbox(
        "Search Player",
        opts,
        index=0,
        format_func=labels.get,
        key=key,
        label_visibility="collapsed"
    )
    if selected:
        show_player_card(selected, week=week)

