"""Weekly Lineup & Start/Sit Decision Audit.

Follows weekly start/sit decisions front to back:
  1. Input Projections: Roboner's NFL model 4,000-sim mean vs Sleeper weekly baseline across all 57 scoring keys.
  2. Constraints & Legality: Kickoff locks (bench_lock=1), injury exclusions (Out/IR/PUP), bye weeks.
  3. Injured Reserve (IR): Stashed assets, current Sleeper status, week availability, and return prognosis.
  4. Shadow Engine Comparison: Model-chosen 10 starters vs Sleeper-projection 10 starters, reporting point edge.
  5. Submission & Historical Decisions: Churn filter (MIN_GAIN_TO_CHANGE = 0.5), live lock checks, and decision logs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from robo import LEAGUE_ID_2026, injuries, lineup, model_proj, season, sleeper_read, ui, ui_player_card

st.title("⚖️ Lineups & Start/Sit Audit")
ui.gate_banner(st)

# Universal Search & Query Params
col_s1, col_s2 = st.columns([3, 1])
with col_s1:
    ui_player_card.render_player_search_bar(key="lineup_player_search")
ui_player_card.check_query_params_player()

# Week Selection
curr_wk = season.current_week()
with col_s2:
    selected_week = st.number_input("Week", min_value=1, max_value=18, value=curr_wk, step=1)


@st.cache_data(ttl=60, show_spinner="Solving optimal lineup...")
def _solve_lineup(week: int) -> dict[str, Any]:
    players_map = sleeper_read.players(max_age_h=sleeper_read.FRESH_STATUS_MAX_AGE_H)
    roster = season.mine(LEAGUE_ID_2026)
    reserve = set(str(x) for x in (roster.get("reserve") or []))
    all_players = [str(x) for x in (roster.get("players") or [])]
    active = [x for x in all_players if x not in reserve]

    cands, provenance = lineup.project_roster(active, season.SEASON, week, players_map, LEAGUE_ID_2026)
    optimal_slots, total = lineup.optimize(cands)

    current_starters = [str(x) for x in (roster.get("starters") or [])]
    cur_total, bad, pinned = lineup.score_current(current_starters, cands)

    ir_cands, _ = lineup.project_roster(sorted(reserve), season.SEASON, week, players_map, LEAGUE_ID_2026) if reserve else ([], "")

    return {
        "cands": cands,
        "optimal_slots": optimal_slots,
        "total": round(total, 2),
        "current_starters": current_starters,
        "current_total": round(cur_total, 2),
        "gain": round(total - cur_total, 2),
        "bad": bad,
        "pinned": sorted(pinned),
        "provenance": provenance,
        "players_map": players_map,
        "reserve": reserve,
        "ir_cands": ir_cands,
    }


@st.cache_data(ttl=300, show_spinner=False)
def _load_scout_verdict(pid: str) -> dict[str, Any]:
    path = Path("data/news_verdicts.json")
    if not path.is_file():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return (d.get("verdicts") or {}).get(str(pid)) or {}
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner="Running shadow comparison...")
def _shadow_comparison(week: int) -> dict[str, Any]:
    return lineup.compare(week=week, season_yr=season.SEASON, league_id=LEAGUE_ID_2026)


@st.cache_data(ttl=300, show_spinner=False)
def _historical_lineup_decisions() -> list[dict[str, Any]]:
    path = Path("decision-log/data/decisions.json")
    if not path.is_file():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return [x for x in d if x.get("kind") == "lineup"]
    except Exception:
        return []


data = _solve_lineup(selected_week)
cands = data["cands"]
optimal_slots = data["optimal_slots"]
current_starters = data["current_starters"]

# ------------------------------------------------------------- 1. Executive Summary
st.caption(f"Weekly lineup optimization under league scoring · Week {selected_week}")
m = st.columns(5)
m[0].metric("Optimal Lineup", f"{data['total']:.2f} pts")
m[1].metric("Current Projected", f"{data['current_total']:.2f} pts")
m[2].metric("Delta Gain", f"{data['gain']:+.2f} pts", delta_color="normal" if data["gain"] > 0 else "off")
modelled_count = sum(1 for c in cands if c.get("pts_source") == "model")
m[3].metric("Model Coverage", f"{modelled_count} / {len(cands)}")
m[4].metric("Lock / Bye Issues", len(data["bad"]))

if data["bad"]:
    st.error(f"🚨 **Illegal Starter Alert:** Current lineup starts inactive/bye player(s): {', '.join(data['bad'])}")
elif data["gain"] >= lineup.MIN_GAIN_TO_CHANGE:
    st.success(f"🚀 **Actionable Gain:** Starting lineup reshuffle yields {data['gain']:+.2f} pts (>= {lineup.MIN_GAIN_TO_CHANGE} threshold).")
else:
    st.info(f"✅ **Lineup Stabilized:** Gain is {data['gain']:+.2f} pts (< {lineup.MIN_GAIN_TO_CHANGE} threshold) — no reshuffle churn.")

# ------------------------------------------------------------- 2. Starters Trace
st.subheader("1 · Optimal Starters Trace")
st.caption("Click any player row to inspect their full bio, PQI quality tier, and simulation distribution.")

starters_rows = []
for i, slot in enumerate(lineup.SLOTS):
    p = optimal_slots[i]
    if p:
        pid = p["player_id"]
        is_currently_starting = pid in current_starters[:len(lineup.SLOTS)]
        sleeper_pts = p.get("sleeper_pts", 0.0)
        edge = p["pts"] - sleeper_pts
        starters_rows.append({
            "slot": slot,
            "player": p["name"],
            "pos": p["pos"],
            "team": p["team"] or "—",
            "model_pts": p["pts"],
            "sleeper_pts": sleeper_pts,
            "edge": edge,
            "p10": p.get("p10"),
            "p90": p.get("p90"),
            "status": p.get("injury") or "Healthy",
            "opponent": p.get("opponent") or "—",
            "locked": "🔒 Locked" if p.get("locked") else "Open",
            "currently_starting": "✅ Yes" if is_currently_starting else "🔄 Bench Swap",
            "source": p.get("pts_source"),
            "player_id": pid,
        })
    else:
        starters_rows.append({
            "slot": slot, "player": "⚠️ UNFILLED HOLE", "pos": "—", "team": "—",
            "model_pts": 0.0, "sleeper_pts": 0.0, "edge": 0.0, "p10": None, "p90": None,
            "status": "Hole", "opponent": "—", "locked": "—", "currently_starting": "No",
            "source": "none", "player_id": "",
        })

df_starters = pd.DataFrame(starters_rows)
ev_starters = st.dataframe(
    df_starters[[
        "slot", "player", "pos", "team", "model_pts", "sleeper_pts", "edge",
        "p10", "p90", "status", "opponent", "locked", "currently_starting"
    ]],
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="starters_table",
    column_config={
        "slot": st.column_config.TextColumn("Slot", width="small"),
        "player": st.column_config.TextColumn("Player", width="medium"),
        "model_pts": st.column_config.NumberColumn("Model Pts", format="%.2f", help="Roboner NFL Model 4,000-sim mean across 57 scoring keys"),
        "sleeper_pts": st.column_config.NumberColumn("Sleeper Pts", format="%.1f", help="Sleeper's weekly feed (23 keys only)"),
        "edge": st.column_config.NumberColumn("Model Edge", format="%+.2f", help="Model points minus Sleeper points"),
        "p10": st.column_config.NumberColumn("Floor (P10)", format="%.1f"),
        "p90": st.column_config.NumberColumn("Ceiling (P90)", format="%.1f"),
    }
)
ui_player_card.attach_player_selection(df_starters, ev_starters, id_col="player_id", week=selected_week)

# ------------------------------------------------------------- 3. Bench Comparison
st.subheader("2 · Bench Assets & Flex Alternatives")
opt_ids = {p["player_id"] for p in optimal_slots if p}
bench_cands = [c for c in cands if c["player_id"] not in opt_ids]

if bench_cands:
    bench_rows = []
    for b in sorted(bench_cands, key=lambda x: -x["pts"]):
        pid = b["player_id"]
        sleeper_pts = b.get("sleeper_pts", 0.0)
        bench_rows.append({
            "player": b["name"],
            "pos": b["pos"],
            "team": b["team"] or "—",
            "model_pts": b["pts"],
            "sleeper_pts": sleeper_pts,
            "edge": b["pts"] - sleeper_pts,
            "p10": b.get("p10"),
            "p90": b.get("p90"),
            "status": b.get("injury") or "Healthy",
            "opponent": b.get("opponent") or "—",
            "locked": "🔒 Locked" if b.get("locked") else "Open",
            "player_id": pid,
        })
    df_bench = pd.DataFrame(bench_rows)
    ev_bench = st.dataframe(
        df_bench[[
            "player", "pos", "team", "model_pts", "sleeper_pts", "edge",
            "p10", "p90", "status", "opponent", "locked"
        ]],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="bench_table",
        column_config={
            "model_pts": st.column_config.NumberColumn("Model Pts", format="%.2f"),
            "sleeper_pts": st.column_config.NumberColumn("Sleeper Pts", format="%.1f"),
            "edge": st.column_config.NumberColumn("Edge", format="%+.2f"),
        }
    )
    ui_player_card.attach_player_selection(df_bench, ev_bench, id_col="player_id", week=selected_week)
else:
    st.caption("No bench assets available.")

# ------------------------------------------------------------- 3. Injured Reserve (IR) Stash
st.subheader("3 · Injured Reserve (IR) Stash")
st.caption("Players currently parked on Roboner's injured reserve, their return timelines, and projected availability.")

ir_cands = data.get("ir_cands") or []
reserve_ids = data.get("reserve") or set()

if reserve_ids:
    ir_rows = []
    cand_by_id = {c["player_id"]: c for c in ir_cands}
    for pid in sorted(reserve_ids):
        p = data["players_map"].get(pid) or {}
        p_name = sleeper_read.player_name(data["players_map"], pid)
        pos = p.get("position") or "DEF"
        team = p.get("team") or "—"

        # Current Sleeper status
        inj_status = p.get("injury_status") or "IR"
        body_part = p.get("injury_body_part")
        sleeper_status = f"{inj_status} ({body_part})" if body_part else inj_status
        if p.get("injury_notes"):
            sleeper_status += f" · {p.get('injury_notes')}"

        # Projected availability for the upcoming selected week
        c = cand_by_id.get(pid) or {}
        pts = c.get("pts", 0.0)
        has_game = c.get("has_game", False)

        verdict = _load_scout_verdict(pid)
        floor_wk = injuries.floor_week(pid)
        out_of_season = injuries.out_for_season(pid) or verdict.get("out_for_season")
        ret_wk = verdict.get("return_week")
        ret_min = verdict.get("return_week_min")
        ret_max = verdict.get("return_week_max")

        if out_of_season:
            avail_status = "❌ Out for Season"
        elif floor_wk and selected_week < floor_wk:
            avail_status = f"❌ Ineligible (IR floor: Wk {floor_wk})"
        elif ret_wk and selected_week < ret_wk:
            avail_status = f"❌ Projected Out (Target: Wk {ret_wk})"
        elif not has_game:
            avail_status = "❌ Bye Week"
        elif p.get("injury_status") in lineup.NEVER_START:
            avail_status = f"⚠️ Doubtful/Out ({p.get('injury_status')})"
        elif pts > 0:
            avail_status = f"✅ Projected Available ({pts:.1f} pts)"
        else:
            avail_status = "❓ Projected Inactive (0.0 pts)"

        # Reporting & Prognosis Snippet
        timing_sent = verdict.get("timing_sentence")
        scout_reason = verdict.get("reason")
        espn_r = injuries.row(pid) or {}
        espn_short = espn_r.get("short")
        espn_long = espn_r.get("long")

        if timing_sent:
            prognosis_snippet = f"Scout: {timing_sent}"
        elif ret_wk:
            prognosis_snippet = f"Scout: Return targeted Week {ret_wk}"
        elif ret_min and ret_max:
            prognosis_snippet = f"Scout: Return targeted Weeks {ret_min}–{ret_max}"
        elif espn_short:
            prognosis_snippet = f"ESPN: {espn_short}"
        elif scout_reason:
            prognosis_snippet = f"Scout: {scout_reason[:120]}…"
        elif p.get("injury_notes"):
            prognosis_snippet = f"Sleeper: {p.get('injury_notes')}"
        else:
            prognosis_snippet = "No return timetable published"

        ir_rows.append({
            "player": p_name,
            "pos": pos,
            "team": team,
            "sleeper_status": sleeper_status,
            "week_availability": avail_status,
            "model_pts": pts,
            "prognosis": prognosis_snippet,
            "player_id": pid,
            "verdict": verdict,
            "espn_row": espn_r,
            "injury_notes": p.get("injury_notes"),
        })

    df_ir = pd.DataFrame(ir_rows)
    ev_ir = st.dataframe(
        df_ir[[
            "player", "pos", "team", "sleeper_status", "week_availability", "model_pts", "prognosis"
        ]],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="ir_table",
        column_config={
            "player": st.column_config.TextColumn("Player", width="medium"),
            "sleeper_status": st.column_config.TextColumn("Current Sleeper Status", width="medium"),
            "week_availability": st.column_config.TextColumn(f"Week {selected_week} Availability", width="medium"),
            "model_pts": st.column_config.NumberColumn(f"Wk {selected_week} Pts", format="%.2f"),
            "prognosis": st.column_config.TextColumn("Return Prognosis & Reporting", width="large"),
        }
    )
    ui_player_card.attach_player_selection(df_ir, ev_ir, id_col="player_id", week=selected_week)

    for r in ir_rows:
        with st.expander(f"📋 Medical & Return Dossier: {r['player']} ({r['pos']} · {r['team']})", expanded=False):
            col_d1, col_d2 = st.columns(2)
            with col_d1:
                st.markdown("**🤖 AI Scout Decision & Return Timeline**")
                v = r["verdict"]
                if v.get("reason"):
                    v_color = "#1baf7a" if v.get("verdict") == "positive" else "#c2413b" if v.get("verdict") == "avoid" else "#d89118"
                    st.markdown(
                        f"<span style='color:{v_color}; font-weight:bold; font-size:1.05em;'>"
                        f"{v.get('verdict', '').upper()}</span> (Confidence: {v.get('confidence', '—')})",
                        unsafe_allow_html=True
                    )
                    st.write(v.get("reason"))
                    t_cols = st.columns(3)
                    t_cols[0].metric("Target Week", v.get("return_week") or "—")
                    t_cols[1].metric("Week Window", f"Wk {v.get('return_week_min')}–{v.get('return_week_max')}" if v.get("return_week_min") else "—")
                    t_cols[2].metric("Out for Season", "Yes" if v.get("out_for_season") else "No")
                else:
                    st.caption("No scout verdict recorded in news_verdicts.json.")
            with col_d2:
                st.markdown("**📰 Recent News Snippets & Reporting**")
                espn = r["espn_row"]
                if espn.get("short") or espn.get("long"):
                    if espn.get("short"):
                        st.markdown(f"**Transaction / Beat Note:** {espn.get('short')}")
                    if espn.get("long"):
                        st.markdown(f"**Analyst Prognosis:** {espn.get('long')}")
                    if espn.get("return_date"):
                        st.caption(f"ESPN Projected Return Date: `{espn.get('return_date')}`")
                elif r.get("injury_notes"):
                    st.write(f"**Sleeper Note:** {r.get('injury_notes')}")
                else:
                    st.caption("No recent news wire snippets on file.")
else:
    st.info("No players currently parked on Injured Reserve (IR). Active roster has all players available or on the active bench.")

# ------------------------------------------------------------- 4. Shadow Engine Comparison
st.subheader("4 · Shadow Comparison: Model vs Sleeper Feed")
st.caption(
    "Compares the 10 starters chosen by Roboner's NFL Model against the 10 starters that would be chosen "
    "by raw Sleeper weekly projections under identical constraints."
)
cmp_data = _shadow_comparison(selected_week)
if cmp_data.get("same"):
    st.success("🎯 **Unanimous Lineup:** Both the NFL Model and Sleeper's projections select the exact same 10 starters.")
else:
    st.warning("⚡ **Engines Diverge:** The NFL Model selects a different starting lineup than Sleeper.")
    m_ids = cmp_data["model"]
    s_ids = cmp_data["sleeper"]
    diff_rows = []
    for i, slot in enumerate(lineup.SLOTS):
        m_pid, s_pid = m_ids[i], s_ids[i]
        if m_pid != s_pid:
            m_name = sleeper_read.player_name(data["players_map"], m_pid) if m_pid != "0" else "EMPTY"
            s_name = sleeper_read.player_name(data["players_map"], s_pid) if s_pid != "0" else "EMPTY"
            diff_rows.append({
                "slot": slot,
                "model_starts": m_name,
                "sleeper_starts": s_name,
                "model_id": m_pid,
                "sleeper_id": s_pid,
            })
    if diff_rows:
        st.dataframe(pd.DataFrame(diff_rows)[["slot", "model_starts", "sleeper_starts"]], use_container_width=True, hide_index=True)

with st.expander("Why the engines disagree (Missing Scoring Keys)"):
    st.markdown(
        """
        Sleeper's weekly feed carries **23 of our 57 scoring keys**, omitting:
        - **QB Sacks (-0.5 pts each)**: Significantly shifts QB values for high-sack offenses.
        - **Yardage Bonuses (+2.0 pts)**: 300+ pass yds, 100+ rush/rec yds.
        - **Long TD Bonuses (+1.0 to +2.0 pts)**: 40+ yard pass/rush/rec touchdowns.
        - **Return Yardage & Return Touchdowns**.
        Roboner's NFL Model simulates 4,000 game states and scores all 57 keys directly, capturing real upside and downside tails.
        """
    )

# ------------------------------------------------------------- 5. Historical Lineup Submissions
st.subheader("5 · Lineup Submission History")
history = _historical_lineup_decisions()
if history:
    h_rows = []
    for h in reversed(history[-10:]):
        h_data = h.get("data") or {}
        h_rows.append({
            "when": h.get("ts"),
            "title": h.get("title"),
            "projected": h_data.get("projected"),
            "modelled": f"{h_data.get('modelled', '?')} players",
            "decision": h.get("decision"),
            "rationale": h.get("rationale"),
        })
    st.dataframe(
        pd.DataFrame(h_rows)[["when", "title", "projected", "modelled", "rationale"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "when": st.column_config.TextColumn("Submitted At", width="medium"),
            "rationale": st.column_config.TextColumn("Published Rationale", width="large"),
        }
    )
else:
    st.info("No lineup submission records found in the decision log.")
