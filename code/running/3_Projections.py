"""Projections & Distributions Hub — Weekly & Rest-of-Season Analysis.

Combines filterable Weekly Monte Carlo distributions and Rest-of-Season valuation boards:
  - Tab 1: Weekly Distributions:
      * 4,000-sim Monte Carlo quantiles (P10/P25/P50/P75/P90)
      * Full Altair horizontal quantile band graphics with zero reference lines
      * Differences between Model Mean and Sleeper Weekly Feed (edge)
      * Counterfactual rule-outs (target/snap redistribution) & Head-to-Head distribution comparisons
      * Single-click player row selection to open the Universal Player Card modal
  - Tab 2: Rest of Season (ROS) Board:
      * Playoff-weighted ROS vs unweighted Raw sums
      * Model ROS vs Sleeper Projected Season Pace comparison
      * Multi-period movers (Day Δ, Week Δ Projection, Week Δ News)
      * Drop evaluation costs (hold_mean, hold_tail) & PQI Quality Tiers (T1-T4)
      * Single-click player row selection to open the Universal Player Card modal
"""

from __future__ import annotations

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from nflmodel import anchor, leagues
from nflmodel.plot import lattice_step
from nflmodel.simulate import N_SIMS, simulate_week
from nflmodel import viewer_cache
from robo import (
    LEAGUE_ID_2026,
    expected,
    model_proj,
    quality,
    ros,
    season,
    sleeper_read,
    ui,
    ui_player_card,
    value_history,
)

st.title("📊 Projections & Distributions Hub")
ui.gate_banner(st)

# Universal Search & Query Params
col_s1, col_s2 = st.columns([3, 1])
with col_s1:
    ui_player_card.render_player_search_bar(key="proj_hub_search")
ui_player_card.check_query_params_player()

main_tabs = st.tabs(["⚡ Weekly Distributions", "📈 Rest of Season (ROS) Board"])

POS_COLOR = {"QB": "#2a78d6", "RB": "#eb6834", "WR": "#1baf7a",
             "TE": "#4a3aa7", "K": "#e87ba4", "DEF": "#008300"}
MY_OWNER = "Robowner"
OWNER_COLOR = {"My lineup": "#20a464", "My bench": "#52b788",
               "Other roster": "#7d8799", "Waivers / free agents": "#d89118"}
SCOPES = {"lineup": "My lineup", "roster": "My roster", "all": "All rostered",
          "universe": "Everyone Sleeper projects"}
STEP_WORDS = {1.0: "whole points", 0.5: "half points", 0.25: "quarter points"}
SORT_COLS = ["median", "mean", "q_score", "tier", "ceiling (P90)", "floor (P10)", "P25", "P75",
             "edge", "sleeper", "P(10+)", "P(15+)", "P(20+)", "P(25+)",
             "player", "pos", "team", "owner"]


# ==============================================================================
# TAB 1: WEEKLY DISTRIBUTIONS
# ==============================================================================
with main_tabs[0]:
    st.markdown("### Weekly Point Distributions & Quantile Bands")
    st.caption("Monte Carlo simulation distributions from 4,000 game states. Scored across all 57 league scoring keys.")

    # Model Run Caching
    @st.cache_data(show_spinner="Loading weekly model simulation...")
    def _read_artifact(league_id, season_yr, wk, n, sit_out, team_alloc, key):
        scenario = {"league": league_id, "season": season_yr, "week": wk,
                    "simulations": n, "sit_out": list(sit_out), "whole_offences": team_alloc}

        def runner():
            rows, note = simulate_week(league_id, season_yr, wk, "universe", n=n,
                                       sit_out=sit_out, team_alloc=team_alloc)
            rows = [(nm, ps, ow, pts.astype(np.float32), tm, pid)
                    for nm, ps, ow, pts, tm, pid in rows]
            return rows, note

        return viewer_cache.load_or_run(key, scenario, runner)

    def _run_sim(league_id, season_yr, wk, n, sit_out, team_alloc):
        key = viewer_cache.artifact_key(league_id, season_yr, wk, n, sit_out, team_alloc)
        return _read_artifact(league_id, season_yr, wk, n, sit_out, team_alloc, key)

    @st.cache_data(ttl=60, show_spinner=False)
    def _get_starters(league_id: str) -> set[str]:
        try:
            m = season.mine()
            if m and m.get("starters"):
                return set(str(pid) for pid in m["starters"])
        except Exception:
            pass
        return set()

    def _scope_rows(rows, scp, starters=None):
        if scp == "universe":
            return rows
        if scp == "lineup":
            if starters is None:
                starters = _get_starters(LEAGUE_ID_2026)
            if starters:
                matched = [r for r in rows if r[2] == MY_OWNER and str(r[5]) in starters]
                if matched:
                    return matched
            return [r for r in rows if r[2] == MY_OWNER]
        if scp in ("roster", "mine"):
            return [r for r in rows if r[2] == MY_OWNER]
        return [r for r in rows if r[2] is not None]

    @st.cache_data(ttl=1800, show_spinner=False)
    def _sleeper_proj(season_yr, wk, league_id):
        return anchor.sleeper_points(season_yr, wk, league_id)

    @st.cache_data(ttl=600, show_spinner=False)
    def _load_quality_batch(pids: tuple, wk: int) -> dict[str, dict[str, Any]]:
        try:
            table = expected.load()
        except Exception:
            table = {}
        out = {}
        for pid in pids:
            try:
                res = quality.score(str(pid), week=wk, table=table)
                out[str(pid)] = {
                    "q_score": float(res.get("q") or 0.0),
                    "tier": res.get("tier", "T4_REPLACEMENT"),
                }
            except Exception:
                out[str(pid)] = {"q_score": 0.0, "tier": "T4_REPLACEMENT"}
        return out

    def _table_df(rows, proj, starters=None, wk: int | None = None) -> pd.DataFrame:
        if starters is None:
            starters = _get_starters(LEAGUE_ID_2026)
        pids = tuple(str(r[5]) for r in rows)
        q_map = _load_quality_batch(pids, int(wk or season.current_week()))
        out = []
        for name, pos, owner, pts, team, pid in rows:
            q = np.percentile(pts, [10, 25, 50, 75, 90])
            sp = proj.get(str(pid))
            if owner == MY_OWNER:
                availability = "My lineup" if (not starters or str(pid) in starters) else "My bench"
            elif owner is None:
                availability = "Waivers / free agents"
            else:
                availability = "Other roster"
            mean = float(pts.mean())
            q_info = q_map.get(str(pid), {"q_score": 0.0, "tier": "T4_REPLACEMENT"})
            out.append({
                "player_id": str(pid),
                "player": name, "pos": pos, "team": team,
                "tier": q_info["tier"],
                "q_score": q_info["q_score"],
                "owner": owner or "FREE",
                "availability": availability,
                "chart label": f"{team}|DEF" if pos == "DEF" else f"{name}|{team} · {pos}",
                "sleeper": sp, "median": q[2],
                "edge": (mean - sp) if sp is not None else None,
                "floor (P10)": q[0], "P25": q[1], "P75": q[3], "ceiling (P90)": q[4],
                "mean": mean,
                "P(10+)": 100 * float(np.mean(pts >= 10)),
                "P(15+)": 100 * float(np.mean(pts >= 15)),
                "P(20+)": 100 * float(np.mean(pts >= 20)),
                "P(25+)": 100 * float(np.mean(pts >= 25)),
            })
        return pd.DataFrame(out).sort_values("median", ascending=False)

    # Controls Bar
    c_w1, c_w2, c_w3, c_w4 = st.columns([1, 1, 2, 2])
    with c_w1:
        wk_val = st.number_input("Model Week", 1, 18, season.current_week(), key="wk_model_input")
    with c_w2:
        scope_pick = st.selectbox("Player Scope", list(SCOPES), format_func=SCOPES.get, key="scope_model_input")
    with c_w3:
        n_sims = st.select_slider("Simulations", [1000, 2000, 4000, 8000], 4000, key="sims_model_input")
    with c_w4:
        team_alloc = st.toggle("Simulate Whole Offenses", value=False,
                               help="Correlates teammates (QB with pass catchers). Costs marginal accuracy.")

    league_id = leagues.RURFFL
    season_yr = int(season.SEASON)

    try:
        base_rows, note, artifact = _run_sim(league_id, season_yr, wk_val, n_sims, (), team_alloc)
    except Exception as e:
        st.error(f"Could not load simulation artifact: {e}")
        base_rows, note, artifact = [], "unavailable", {}

    if base_rows:
        starters = _get_starters(league_id)
        visible_base = _scope_rows(base_rows, scope_pick, starters=starters)
        avail_names = sorted(r[0] for r in visible_base)

        with st.expander("Counterfactual Rule-Out Simulation"):
            sit_out = tuple(st.multiselect(
                "Simulate forcing player(s) out (vacated opportunity redistributed):",
                avail_names,
                key="sit_out_select"
            ))

        if sit_out:
            rows, note, artifact = _run_sim(league_id, season_yr, wk_val, n_sims, sit_out, team_alloc or bool(sit_out))
        else:
            rows = base_rows

        rows = _scope_rows(rows, scope_pick, starters=starters)
        proj = _sleeper_proj(season_yr, wk_val, league_id)
        df_weekly = _table_df(rows, proj, starters=starters, wk=wk_val)

        # Filters
        f_c1, f_c2, f_c3, f_c4, f_c5 = st.columns([2, 2, 2, 2, 2])
        all_pos = [p for p in POS_COLOR if p in set(df_weekly["pos"])]
        all_own = sorted(set(df_weekly["owner"].astype(str)))
        with f_c1:
            pick_pos = st.multiselect("Positions", all_pos, default=all_pos, key="w_pos_pick")
        with f_c2:
            pick_own = st.multiselect("Owners", all_own, default=all_own, key="w_own_pick")
        with f_c3:
            w_tier_opts = ["ALL", "T1_BREAKOUT", "T2_CONTRIBUTOR", "T3_SPECULATIVE", "T4_REPLACEMENT"]
            pick_w_tier = st.selectbox("Quality Tier", w_tier_opts, index=0, key="w_tier_pick")
        with f_c4:
            w_search = st.text_input("Filter Name", "", key="w_name_search")
        with f_c5:
            floor_min = st.slider("Min Median Floor", 0.0, 25.0, 0.0, 0.5, key="w_floor_min")

        view_w = df_weekly[
            df_weekly["pos"].isin(pick_pos or all_pos)
            & df_weekly["owner"].astype(str).isin(pick_own or all_own)
            & (df_weekly["median"] >= floor_min)
        ]
        if pick_w_tier != "ALL":
            view_w = view_w[view_w["tier"] == pick_w_tier]
        if w_search.strip():
            view_w = view_w[view_w["player"].str.contains(w_search.strip(), case=False, na=False)]

        st.caption(f"Showing {len(view_w)} of {len(df_weekly)} simulated | Sort below controls both the bands chart and table.")

        # Sorting & Band Graphics
        c_sort, c_dir = st.columns([2, 3])
        sort_by_w = c_sort.selectbox("Sort By", SORT_COLS, key="w_sort_by")
        desc_w = c_dir.radio("Order", ["High → low", "Low → high"], horizontal=True, key="w_desc") == "High → low"
        view_w = view_w.sort_values(sort_by_w, ascending=not desc_w, na_position="last", kind="mergesort")

        top_w = view_w.head(40)
        if len(top_w):
            x_low = min(0.0, float(top_w["floor (P10)"].min()))
            x_high = max(0.0, float(top_w["ceiling (P90)"].max()))
            x_pad = max(1.0, 0.04 * (x_high - x_low))
            x_domain = [x_low - x_pad, x_high + x_pad]
            xs = alt.Scale(domain=x_domain, nice=False)
            base_chart = alt.Chart(top_w).encode(
                y=alt.Y("chart label:N", sort=list(top_w["chart label"]), title=None,
                        axis=alt.Axis(labelLimit=150, labelExpr="split(datum.label, '|')",
                                      labelFontSize=10, labelLineHeight=11, labelBaseline="middle", labelOverlap=False))
            )
            colour = alt.Color("availability:N",
                               scale=alt.Scale(domain=list(OWNER_COLOR), range=list(OWNER_COLOR.values())),
                               legend=alt.Legend(orient="top", title="Availability"))
            band = base_chart.mark_bar(height=9, opacity=0.30, cornerRadius=3).encode(
                x=alt.X("floor (P10):Q", title="Fantasy Points", scale=xs),
                x2="ceiling (P90):Q", color=colour,
                tooltip=["player:N", "pos:N", "team:N", "owner:N", "availability:N",
                         alt.Tooltip("floor (P10):Q", format=".1f"),
                         alt.Tooltip("median:Q", format=".1f"),
                         alt.Tooltip("ceiling (P90):Q", format=".1f")]
            )
            inner = base_chart.mark_bar(height=9, opacity=0.75, cornerRadius=3).encode(
                x="P25:Q", x2="P75:Q",
                color=alt.Color("availability:N", scale=alt.Scale(domain=list(OWNER_COLOR), range=list(OWNER_COLOR.values())), legend=None)
            )
            mid = base_chart.mark_tick(thickness=2, size=20, color="#888").encode(x="median:Q")
            bg = alt.Chart(top_w).transform_aggregate(n="count()")
            negative = bg.mark_rect(color="#c2413b", opacity=0.055).encode(
                x=alt.X(datum=x_domain[0], type="quantitative", scale=xs, axis=None),
                x2=alt.X2(datum=0)
            )
            zero = bg.mark_rule(color="#8b4a45", opacity=0.90, strokeDash=[4, 3], strokeWidth=1.5).encode(
                x=alt.X(datum=0, type="quantitative", scale=xs, axis=None)
            )
            st.altair_chart((negative + band + inner + mid + zero).properties(height=max(260, 30 * len(top_w))), use_container_width=True)

        # Weekly Table
        st.markdown("#### Weekly Distributions Table")
        st.caption("Click any player row to launch the complete interactive Player Dossier.")
        if not view_w.empty:
            ev_w_table = st.dataframe(
                view_w[[
                    "player", "pos", "team", "tier", "q_score", "owner", "availability", "median", "mean",
                    "sleeper", "edge", "floor (P10)", "ceiling (P90)", "P25", "P75",
                    "P(10+)", "P(15+)", "P(20+)", "P(25+)"
                ]],
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="weekly_sim_table",
                column_config={
                    "tier": st.column_config.TextColumn("Quality Tier", help="PQI Quality Tier (T1-T4)"),
                    "q_score": st.column_config.NumberColumn("Q-Score", format="%.2f", help="Player Quality Index score in [0.0, 1.0]"),
                    "median": st.column_config.NumberColumn("Median", format="%.2f"),
                    "mean": st.column_config.NumberColumn("Model Mean", format="%.2f"),
                    "sleeper": st.column_config.NumberColumn("Sleeper", format="%.1f"),
                    "edge": st.column_config.NumberColumn("Model Edge", format="%+.2f", help="Model Mean minus Sleeper weekly projection"),
                    "floor (P10)": st.column_config.NumberColumn("P10 Floor", format="%.1f"),
                    "ceiling (P90)": st.column_config.NumberColumn("P90 Ceiling", format="%.1f"),
                    "P(10+)": st.column_config.NumberColumn("P(10+)", format="%.0f%%"),
                    "P(15+)": st.column_config.NumberColumn("P(15+)", format="%.0f%%"),
                    "P(20+)": st.column_config.NumberColumn("P(20+)", format="%.0f%%"),
                    "P(25+)": st.column_config.NumberColumn("P(25+)", format="%.0f%%"),
                }
            )
            ui_player_card.attach_player_selection(view_w, ev_w_table, id_col="player_id", week=wk_val)
        else:
            st.info("No players match the current filters.")


# ==============================================================================
# TAB 2: REST OF SEASON BOARD & PATHWAYS
# ==============================================================================
with main_tabs[1]:
    st.markdown("### Rest of Season (ROS) Valuation Board")
    st.caption("Playoff-odds weighted weekly expectations from this week to week 17 with role absorption and return dates.")

    @st.cache_data(ttl=600, show_spinner="Loading rest of season table...")
    def _load_ros_board():
        d = expected.load()
        rows = list((d.get("players") or {}).values())
        return rows, {k: v for k, v in d.items() if k != "players"}

    @st.cache_data(ttl=600, show_spinner=False)
    def _load_ros_availability(ids: tuple):
        labels = {"free_now": "free now", "weekly_waiver": "on waivers",
                  "drop_waiver": "on waivers", "unavailable": "unavailable"}
        try:
            mine = set(season.mine().get("players") or [])
            held = set(season.rostered_ids())
        except Exception:
            return {}
        out = {pid: {"status": "mine" if pid in mine else "rostered", "why": ""} for pid in held}
        try:
            unrostered = [p for p in ids if p not in held]
            for pid, st_ in season.transaction_states(unrostered).items():
                acq = st_.get("acquisition")
                why = st_.get("reason") or st_.get("unlock_basis") or ""
                out[pid] = {"status": labels.get(acq, str(acq)), "why": why}
        except Exception:
            for pid in ids:
                out.setdefault(pid, {"status": "unrostered", "why": ""})
        return out

    @st.cache_data(ttl=600, show_spinner=False)
    def _load_ros_inputs(ids: tuple, wk: int):
        from robo import moves, value
        table = expected.load()
        mine = {str(p) for p in (season.mine().get("players") or [])}
        out = {}
        for pid in ids:
            pid = str(pid)
            row = (table.get("players") or {}).get(pid) or {"player_id": pid}
            complete, why = moves._ros_complete(table, pid)
            rec = {"complete": complete, "why_incomplete": "" if complete else why}
            try:
                rec["value_of"], rec["value_real"] = value.value_of(row, wk, table=table)
            except Exception:
                rec["value_of"], rec["value_real"] = None, False
            try:
                rec["near_value"] = value.near_value(pid, wk, table=table)
            except Exception:
                rec["near_value"] = None
            if pid in mine:
                sh = None
                try:
                    sh = value.hold_shape(row, wk)
                except Exception:
                    pass
                if sh:
                    rec.update({"hold_mean": sh.get("mean"), "hold_tail": sh.get("tail"),
                                "p_matters": sh.get("p_matters"), "hold_se": sh.get("se"),
                                "starts_median": sh.get("starter")})
            # Add Quality Tier
            try:
                q_res = quality.score(pid, week=wk, table=table)
                rec["q_score"] = q_res.get("q", 0.0)
                rec["q_tier"] = q_res.get("tier", "T4_REPLACEMENT")
            except Exception:
                rec["q_score"] = 0.0
                rec["q_tier"] = "T4_REPLACEMENT"
            out[pid] = rec
        return out

    ros_rows, ros_meta = _load_ros_board()
    if ros_rows:
        ros_wk = int(ros_meta.get("week") or season.current_week())
        ros_ids = tuple(r["player_id"] for r in ros_rows)
        ros_avail = _load_ros_availability(ros_ids)
        ros_inputs = _load_ros_inputs(ros_ids, ros_wk)
        ros_current = {**ros_meta, "players": {r["player_id"]: r for r in ros_rows}}

        day_base = value_history.baseline(ros_current, 1)
        week_base = value_history.baseline(ros_current, 7)
        day_change = value_history.compare(ros_current, day_base)
        week_change = value_history.compare(ros_current, week_base)

        # Filters Bar
        rf_c1, rf_c2, rf_c3, rf_c4 = st.columns([2, 2, 2, 2])
        with rf_c1:
            ros_scope = st.radio("Availability", ["everyone", "mine", "rostered", "free now", "on waivers"], horizontal=True, key="ros_scope")
        with rf_c2:
            tier_opts = ["ALL", "T1_BREAKOUT", "T2_CONTRIBUTOR", "T3_SPECULATIVE", "T4_REPLACEMENT"]
            tier_pick = st.selectbox("Quality Tier", tier_opts, index=0, key="ros_tier_pick")
        with rf_c3:
            ros_sort = st.selectbox("Sort By", ["ros", "raw", "q_score", "tier", "day Δ", "week Δ"], key="ros_sort_pick")
        with rf_c4:
            ros_q = st.text_input("Search Name", "", key="ros_search_input")

        # Build View
        filtered_ros = ros_rows
        if ros_scope == "mine":
            filtered_ros = [r for r in filtered_ros if (ros_avail.get(r["player_id"]) or {}).get("status") == "mine"]
        elif ros_scope == "rostered":
            filtered_ros = [r for r in filtered_ros if (ros_avail.get(r["player_id"]) or {}).get("status") in {"mine", "rostered"}]
        elif ros_scope in {"free now", "on waivers"}:
            filtered_ros = [r for r in filtered_ros if (ros_avail.get(r["player_id"]) or {}).get("status") == ros_scope]

        if tier_pick != "ALL":
            filtered_ros = [r for r in filtered_ros if (ros_inputs.get(r["player_id"]) or {}).get("q_tier") == tier_pick]

        if ros_q.strip():
            filtered_ros = [r for r in filtered_ros if ros_q.strip().lower() in r["name"].lower()]

        # Compile DataFrame
        ros_records = []
        for r in filtered_ros:
            pid = r["player_id"]
            d = ros_inputs.get(pid) or {}
            d_ch = (day_change.get(pid) or {}).get("delta")
            w_ch = (week_change.get(pid) or {}).get("delta")
            ros_records.append({
                "player_id": pid,
                "player": r["name"],
                "pos": r["pos"],
                "team": r["team"] or "—",
                "tier": d.get("q_tier", "T4_REPLACEMENT"),
                "q_score": d.get("q_score", 0.0),
                "availability": (ros_avail.get(pid) or {}).get("status", "unrostered"),
                "ros": float(r.get("ros") or 0.0),
                "raw": float(r.get("raw") or 0.0),
                "day Δ": d_ch,
                "week Δ": w_ch,
                "cut cost": d.get("hold_mean"),
                "worst world": d.get("hold_tail"),
                "p matters": d.get("p_matters"),
                "shortlist": d.get("value_of"),
                "priced?": d.get("complete"),
            })

        ros_display_cols = [
            "player", "pos", "team", "tier", "q_score", "availability", "ros", "raw",
            "day Δ", "week Δ", "cut cost", "worst world", "p matters", "shortlist", "priced?"
        ]
        df_ros = pd.DataFrame(ros_records, columns=["player_id"] + ros_display_cols)
        if ros_sort in df_ros:
            df_ros = df_ros.sort_values(ros_sort, ascending=False)

        # Risers & Fallers Chart
        st.markdown("#### Top Risers & Fallers (24h Change)")
        valid_movers = [r for r in ros_records if r.get("day Δ") is not None and abs(r["day Δ"]) >= 0.5]
        if valid_movers:
            sorted_movers = sorted(valid_movers, key=lambda x: -abs(x["day Δ"]))[:12]
            chart_m = pd.DataFrame(sorted_movers)[["player", "day Δ"]].set_index("player")
            st.bar_chart(chart_m, horizontal=True, height=260)
        else:
            st.caption("No significant 24h ROS movements (>= 0.5 pts) in filtered pool.")

        # ROS Table
        st.markdown("#### Rest of Season Board")
        st.caption("Click any player row to launch their complete interactive Player Dossier.")
        if not df_ros.empty:
            ev_ros_table = st.dataframe(
                df_ros[ros_display_cols],
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="ros_board_table",
                column_config={
                    "tier": st.column_config.TextColumn("Quality Tier", help="PQI Quality Tier (T1-T4)"),
                    "q_score": st.column_config.NumberColumn("Q-Score", format="%.2f", help="Player Quality Index score in [0.0, 1.0]"),
                    "ros": st.column_config.NumberColumn("ROS (Weighted)", format="%.1f"),
                    "raw": st.column_config.NumberColumn("Raw Sum", format="%.1f"),
                    "day Δ": st.column_config.NumberColumn("Day Δ", format="%+.1f"),
                    "week Δ": st.column_config.NumberColumn("Week Δ", format="%+.1f"),
                    "cut cost": st.column_config.NumberColumn("Cut Cost", format="%.2f"),
                    "worst world": st.column_config.NumberColumn("Worst World (P10)", format="%.2f"),
                    "p matters": st.column_config.NumberColumn("P Matters", format="%.3f"),
                    "shortlist": st.column_config.NumberColumn("Shortlist", format="%.1f"),
                }
            )
            ui_player_card.attach_player_selection(df_ros, ev_ros_table, id_col="player_id", week=ros_wk)
        else:
            if ros_scope == "free now":
                st.info("ℹ️ No players currently 'free now' — Tuesday waivers are locked until Wednesday morning processing.")
            else:
                st.info(f"No players match the current filters (Scope: {ros_scope}, Tier: {tier_pick}).")
