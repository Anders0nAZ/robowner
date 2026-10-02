"""AI & Scout Center — Qualitative LLM Arbitrations & Reporting Intelligence.

Provides first-class auditing for all LLM-driven reasoning:
  1. Scout Queue & Pipeline: Real-time monitoring of the rate-limited prose queue, priority tiers, and GPU drain cadence.
  2. Weekly Scout Verdicts: Unredacted RotoWire/RotoBaller prose scouting and Codex/Qwen evaluations from data/news_verdicts.json.
  3. Dead-Heat Arbitrations: History of near-tie/dead-heat move proposals evaluated by local Ollama (qwen3.8:27b-mtp-80k).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import re

import altair as alt
import pandas as pd
import requests
import streamlit as st

from robo import DATA, ROOT, decision_audit, scout_queue, season, sleeper_read, ui, ui_player_card

st.title("🤖 AI & Scout Center")
ui.gate_banner(st)

# Universal Search & Query Params
col_s1, _ = st.columns([3, 1])
with col_s1:
    ui_player_card.render_player_search_bar(key="ai_scout_search")
ui_player_card.check_query_params_player()

# ------------------------------------------------------------------------------
# Persistent Pipeline Status Ribbon (Global Header)
# ------------------------------------------------------------------------------
try:
    q_stat = scout_queue.status()
except Exception:
    q_stat = {}

q_backlog = int(q_stat.get("queued", 0))
q_attention = int(q_stat.get("attention", 0))
last_b = q_stat.get("last_batch_at")
last_batch_str = ui.fmt_age(last_b) if last_b else "Never"
vram_busy_since = q_stat.get("vram_busy_since")
vram_busy_h = q_stat.get("vram_busy_hours", 0)

# Check VRAM Focus non-blocking
vram_focus = None
try:
    r_gate = requests.get("http://localhost:11434/_gate/focus", timeout=0.2)
    if r_gate.ok:
        f_data = r_gate.json()
        if f_data.get("blocks_llm"):
            vram_focus = f_data.get("away") or "Focus mode active"
except Exception:
    vram_focus = None

r1, r2, r3, r4 = st.columns(4)
r1.metric("Queue Backlog", f"{q_backlog} pending", help="Total pending players in the scout queue")
r2.metric(
    "Attention Required",
    f"{q_attention} failed",
    delta="Action needed" if q_attention > 0 else None,
    delta_color="inverse" if q_attention > 0 else "off",
    help="Players that exceeded max retry attempts",
)
r3.metric(
    "Last Batch Cadence",
    last_batch_str,
    help=f"Last drain batch timestamp. Last batch size: {q_stat.get('last_batch_size', 0)}",
)
if vram_focus:
    r4.metric("VRAM Admission", "Focus Mode Active", delta=vram_focus, delta_color="inverse",
              help="VRAM Gate is blocking LLM requests for external workload")
elif vram_busy_since:
    r4.metric("VRAM Admission", f"Busy ({vram_busy_h}h)", delta="VRAM contention", delta_color="inverse",
              help="Scout drain paused because VRAM was busy")
else:
    r4.metric("VRAM Admission", "Ready / Idle", help="Ollama model resident; gate ready to admit batches")

st.divider()

# Tab hierarchy: Scout Queue (Default Landing) -> Verdicts -> Arbitrations
ai_tabs = st.tabs([
    "🚦 Scout Queue & Pipeline",
    "📰 Weekly Scout Verdicts",
    "⚖️ Dead-Heat Arbitrations",
])


# ==============================================================================
# TAB 1: SCOUT QUEUE & PIPELINE
# ==============================================================================
with ai_tabs[0]:
    st.markdown("### Scout Prose Queue & Processing Backlog")
    st.caption(
        "One durable, rate-limited queue for all automated prose scouting (news pulse, cascades, daily refresh). "
        "Paced by a 30s floor (`MIN_BATCH_INTERVAL = 30s`), each pulse drains up to 10 batches of 8 players "
        "within a 10-minute budget, prioritizing active-week starters and emergency moves while yielding "
        "the GPU to other tasks."
    )

    # --------------------------------------------------------------------------
    # Hardened Queue Depth & Pulse Activity History
    # --------------------------------------------------------------------------
    st.markdown("#### 📈 Queue Depth & Pulse Activity History")

    @st.cache_data(ttl=30, show_spinner=False)
    def _parse_pulse_history(hours: int = 24) -> tuple[pd.DataFrame, list[dict]]:
        log_path = ROOT / "news-watch.log"
        if not log_path.is_file():
            return pd.DataFrame(), []

        pattern = re.compile(r"\[(.*?)\] (.*)")
        cutoff = datetime.now() - timedelta(hours=hours)

        try:
            with open(log_path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            return pd.DataFrame(), []

        # Group log lines by pulse execution window (deduplicating entries within 60 seconds)
        pulses = []
        current_group = []

        for line in lines:
            m = pattern.match(line.strip())
            if not m:
                continue
            ts_str, msg = m.groups()
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

            if ts < cutoff:
                continue

            if not current_group:
                current_group.append((ts, msg))
            else:
                if (ts - current_group[0][0]).total_seconds() <= 60:
                    current_group.append((ts, msg))
                else:
                    pulses.append(current_group)
                    current_group = [(ts, msg)]

        if current_group:
            pulses.append(current_group)

        parsed_pulses = []
        for g in pulses:
            ts = g[-1][0]
            all_msgs = " | ".join(m for _, m in g)
            gpu_busy = any("GPU VRAM busy" in m for _, m in g)
            paused = any("paused" in m.lower() for _, m in g)

            ev_m = re.search(r"(\d+)\s+event\(s\)", all_msgs)
            events = int(ev_m.group(1)) if ev_m else 0

            q_m = re.search(r"(\d+)\s+advisory queued", all_msgs)
            if q_m:
                queued = int(q_m.group(1))
            elif any("quiet" in m or "acted" in m for _, m in g):
                queued = 0
            else:
                queued = None

            if paused:
                status = "Paused"
            elif gpu_busy:
                status = "GPU Busy"
            elif any("acted" in m for _, m in g):
                status = "Acted"
            elif any("quiet" in m for _, m in g):
                status = "Quiet"
            else:
                status = "Other"

            parsed_pulses.append({
                "Time": ts.strftime("%b %d %H:%M"),
                "Timestamp": ts,
                "Pending in Queue": queued,
                "News Events": events,
                "Status": status,
                "Log Message": all_msgs[:120],
            })

        df = pd.DataFrame(parsed_pulses) if parsed_pulses else pd.DataFrame()
        return df, parsed_pulses

    w_col1, _ = st.columns([2, 3])
    with w_col1:
        window_choice = st.selectbox(
            "Timeline Window",
            ["6 hours", "12 hours", "24 hours", "48 hours", "7 days"],
            index=2,
            key="scout_q_window",
        )
    h_map = {"6 hours": 6, "12 hours": 12, "24 hours": 24, "48 hours": 48, "7 days": 168}
    hours = h_map.get(window_choice, 24)

    df_hist, raw_pulses = _parse_pulse_history(hours=hours)

    if not df_hist.empty:
        df_chart = df_hist.copy()
        df_line = df_chart[df_chart["Pending in Queue"].notna()]

        base = alt.Chart(df_chart).encode(
            x=alt.X("Timestamp:T", title="Pulse Time", axis=alt.Axis(format="%b %d %H:%M", labelAngle=-25))
        )

        line = alt.Chart(df_line).mark_line(color="#00A3E0", strokeWidth=2).encode(
            x=alt.X("Timestamp:T"),
            y=alt.Y("Pending in Queue:Q", title="Pending Queue Depth", scale=alt.Scale(zero=True))
        )

        points = base.mark_circle(size=60).encode(
            y=alt.Y("Pending in Queue:Q"),
            color=alt.Color(
                "Status:N",
                title="Pulse Status",
                scale=alt.Scale(
                    domain=["Acted", "Quiet", "GPU Busy", "Paused", "Other"],
                    range=["#10b981", "#0284c7", "#f59e0b", "#9ca3af", "#8b5cf6"],
                ),
            ),
            tooltip=[
                alt.Tooltip("Timestamp:T", title="Pulse Time", format="%b %d, %H:%M"),
                alt.Tooltip("Pending in Queue:Q", title="Queue Depth"),
                alt.Tooltip("News Events:Q", title="News Events"),
                alt.Tooltip("Status:N", title="Pulse Status"),
                alt.Tooltip("Log Message:N", title="Log Summary"),
            ],
        )

        chart = (line + points).properties(height=260).interactive()
        st.altair_chart(chart, use_container_width=True)

        with st.expander(f"🔍 View Pulse Log Breakdown ({window_choice} — {len(df_hist)} pulses)"):
            st.dataframe(
                df_hist[["Time", "Pending in Queue", "News Events", "Status", "Log Message"]],
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info(f"No pulse activity recorded in news-watch.log within the last {window_choice}.")

    # --------------------------------------------------------------------------
    # Queue Items State: Attention, Pending (Positions), and Completed (Outcomes)
    # --------------------------------------------------------------------------
    q_path = DATA / "scout_queue.json"
    if q_path.is_file():
        try:
            q_data = json.loads(q_path.read_text(encoding="utf-8"))
            items = list((q_data.get("items") or {}).values())
            pending = [x for x in items if x.get("disposition") == "pending"]
            attention = [x for x in items if x.get("disposition") == "attention"]
            completed = list(q_data.get("completed") or [])

            if attention:
                st.markdown("#### ⚠️ Attention Required (Exceeded Max Retries)")
                att_rows = []
                for item in attention:
                    att_rows.append({
                        "Player": item.get("name") or str(item.get("player_id")),
                        "Category": item.get("category", "background"),
                        "Attempts": item.get("attempts", 0),
                        "Last Error": item.get("last_error") or "—",
                        "Enqueued": ui.fmt_age(item.get("enqueued_at")),
                    })
                st.dataframe(pd.DataFrame(att_rows), use_container_width=True, hide_index=True)

            if pending:
                st.markdown(f"#### ⏳ Pending Items in Queue ({len(pending)} players)")
                pending_ids = [str(x.get("player_id")) for x in pending]
                pos_map = scout_queue.positions(pending_ids)
                players = sleeper_read.players()

                q_rows = []
                for item in pending:
                    pid = str(item.get("player_id", ""))
                    name = item.get("name") or sleeper_read.player_name(players, pid)
                    pos_info = pos_map.get(pid, {})
                    rank = pos_info.get("rank", 999)
                    batches_ahead = pos_info.get("batches_ahead", 0)
                    wait_s = pos_info.get("not_due_for_s", 0)
                    wait_str = f"Wait {int(wait_s)}s" if wait_s > 0 else "Due now"

                    q_rows.append({
                        "Rank": f"#{rank}",
                        "_rank_num": rank,
                        "Player": name,
                        "Category": item.get("category", "background"),
                        "Priority": item.get("priority", 3),
                        "Batches Ahead": f"{batches_ahead} batch(es)",
                        "Attempts": item.get("attempts", 0),
                        "Queued": ui.fmt_age(item.get("enqueued_at")),
                        "Cadence": wait_str,
                        "Last Error": item.get("last_error") or "—",
                    })

                q_rows.sort(key=lambda r: r["_rank_num"])
                df_pending = pd.DataFrame(q_rows).drop(columns=["_rank_num"])
                st.dataframe(
                    df_pending,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Rank": st.column_config.TextColumn("Rank", width="small", help="Drain sequence position"),
                        "Player": st.column_config.TextColumn("Player", width="medium"),
                        "Category": st.column_config.TextColumn("Category", width="small"),
                        "Priority": st.column_config.NumberColumn("Priority", width="small"),
                        "Batches Ahead": st.column_config.TextColumn("Batches Ahead", width="small"),
                        "Attempts": st.column_config.NumberColumn("Attempts", width="small"),
                        "Queued": st.column_config.TextColumn("Queued", width="small"),
                        "Cadence": st.column_config.TextColumn("Next Attempt", width="small"),
                        "Last Error": st.column_config.TextColumn("Last Error", width="medium"),
                    },
                )
            else:
                st.success("✅ Scout queue is clear. No players pending evaluation.")

            if completed:
                with st.expander(f"📋 Recently Completed Evaluations ({len(completed)} total)"):
                    completed_pids = [str(x.get("player_id")) for x in completed[-50:]]
                    outcomes_map = scout_queue.outcomes(completed_pids)
                    comp_rows = []
                    for item in reversed(completed[-50:]):
                        pid = str(item.get("player_id"))
                        outcome_info = outcomes_map.get(pid, item)
                        comp_rows.append({
                            "Player": outcome_info.get("name") or pid,
                            "Category": outcome_info.get("category", "—"),
                            "Outcome": outcome_info.get("outcome", "—"),
                            "Completed": ui.fmt_age(outcome_info.get("completed_at")),
                        })
                    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)

        except Exception as e:
            st.warning(f"Could not inspect scout queue items: {e}")


# ==============================================================================
# TAB 2: WEEKLY SCOUT VERDICTS
# ==============================================================================
with ai_tabs[1]:
    st.markdown("### Weekly Prose Scout Verdicts")
    st.caption("Extracted from data/news_verdicts.json. Evaluated via Codex/Qwen across RotoWire/RotoBaller prose and beat reporting.")

    @st.cache_data(ttl=60, show_spinner="Loading scout verdicts...")
    def _load_all_verdicts() -> tuple[list[dict], dict]:
        path = DATA / "news_verdicts.json"
        if not path.is_file():
            return [], {}
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            players = sleeper_read.players()
            v_dict = d.get("verdicts") or {}

            # Auxiliary news & weekly feeds from news_watch.json
            nw_path = DATA / "news_watch.json"
            espn = {}
            if nw_path.is_file():
                try:
                    nw = json.loads(nw_path.read_text(encoding="utf-8"))
                    espn = nw.get("espn") or {}
                except Exception:
                    espn = {}

            # Roster ownership context
            my_pids = set()
            all_rostered = set()
            try:
                my_roster = season.mine()
                my_pids = set(str(x) for x in ((my_roster.get("players") or []) + (my_roster.get("reserve") or [])))
                all_rostered = set(str(x) for x in season.rostered_ids())
            except Exception:
                pass

            out = []
            for pid, rec in v_dict.items():
                p = players.get(str(pid)) or {}
                es = espn.get(str(pid)) or {}

                # Ownership
                if str(pid) in my_pids:
                    owner = "🤖 Robowner"
                elif str(pid) in all_rostered:
                    owner = "👥 Rival"
                else:
                    owner = "🆓 Free Agent"

                # Timestamps
                j_ts = float(rec.get("judged_at") or 0.0)
                n_ts = float(p.get("news_updated") or 0.0)
                if n_ts > 1e11:
                    n_ts /= 1000.0

                scouted_dt = datetime.fromtimestamp(j_ts) if j_ts else None
                scouted_age = ui.fmt_age(j_ts) if j_ts else "—"
                news_dt = datetime.fromtimestamp(n_ts) if n_ts else None
                news_age = ui.fmt_age(n_ts) if n_ts else "—"

                # Player Name and Last Name parsing
                raw_name = rec.get("name") or sleeper_read.player_name(players, str(pid))
                first_name, last_name = ui.split_player_name(raw_name, p)

                # Injury / Status badge
                inj_st = p.get("injury_status")
                inj_note = p.get("injury_notes") or p.get("injury_body_part") or es.get("body_part")
                if inj_st == "Out":
                    status_badge = f"🚨 Out ({inj_note})" if inj_note else "🚨 Out"
                elif inj_st in ("IR", "Reserve"):
                    status_badge = f"🏥 IR ({inj_note})" if inj_note else "🏥 IR"
                elif inj_st == "Questionable":
                    status_badge = f"⚠️ Q ({inj_note})" if inj_note else "⚠️ Questionable"
                elif inj_st == "Doubtful":
                    status_badge = f"🛑 Doubtful ({inj_note})" if inj_note else "🛑 Doubtful"
                elif inj_st:
                    status_badge = f"🚑 {inj_st}"
                elif inj_note:
                    status_badge = f"⚠️ Active ({inj_note})"
                else:
                    status_badge = "✅ Active"

                # Consolidated Return Outlook
                if rec.get("out_for_season"):
                    ret = "❌ Out for Season"
                elif rec.get("return_week_min") or rec.get("return_week_max"):
                    wmin = rec.get("return_week_min")
                    wmax = rec.get("return_week_max")
                    ret = f"Wk {wmin}" if wmin == wmax else f"Wk {wmin}-{wmax}"
                    basis = rec.get("return_basis")
                    if basis and basis != "—":
                        ret += f" ({basis[:25]})"
                elif rec.get("return_week"):
                    ret = f"Wk {rec.get('return_week')}"
                else:
                    ret = "—"

                # Verdict display
                v_raw = str(rec.get("verdict") or "").lower()
                if "boost" in v_raw or "positive" in v_raw:
                    v_badge = "🚀 Boost"
                    v_clean = "boost"
                elif "avoid" in v_raw or "negative" in v_raw:
                    v_badge = "🛑 Avoid"
                    v_clean = "avoid"
                else:
                    v_badge = "⚖️ Neutral"
                    v_clean = "neutral"

                out.append({
                    "player_id": str(pid),
                    "player": raw_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "pos": p.get("position") or "—",
                    "team": p.get("team") or "—",
                    "status": status_badge,
                    "verdict": v_clean,
                    "verdict_badge": v_badge,
                    "confidence": float(rec.get("confidence") or 0.0),
                    "reason": rec.get("reason") or "",
                    "latest_news": es.get("short") or p.get("injury_notes") or "",
                    "return_outlook": ret,
                    "ownership": owner,
                    "scouted_dt": scouted_dt,
                    "scouted_age": scouted_age,
                    "news_dt": news_dt,
                    "news_age": news_age,
                    "judged_at": j_ts,
                    "news_updated": n_ts,
                    "timing_sentence": rec.get("timing_sentence") or "",
                })

            out.sort(key=lambda x: x["judged_at"], reverse=True)
            meta = {k: v for k, v in d.items() if k != "verdicts"}
            return out, meta
        except Exception:
            return [], {}

    verdicts, v_meta = _load_all_verdicts()
    if verdicts:
        # Summary KPI Cards
        total_eval = len(verdicts)
        n_boost = sum(1 for v in verdicts if v["verdict"] == "boost")
        n_avoid = sum(1 for v in verdicts if v["verdict"] == "avoid")
        n_inj = sum(1 for v in verdicts if "Active" not in v["status"])
        latest_ts = max((v["judged_at"] for v in verdicts), default=0.0)

        s_col1, s_col2, s_col3, s_col4, s_col5 = st.columns(5)
        s_col1.metric("Evaluated Pool", total_eval, f"Model: {v_meta.get('model', 'Codex/Qwen')}")
        s_col2.metric("🚀 Boosts", n_boost, f"{n_boost / max(1, total_eval):.0%}")
        s_col3.metric("🛑 Avoids", n_avoid, f"{n_avoid / max(1, total_eval):.0%}")
        s_col4.metric("🚑 Injured / Out", n_inj, f"{n_inj / max(1, total_eval):.0%}")
        s_col5.metric("Latest Scout Run", ui.fmt_age(latest_ts) if latest_ts else "Never")

        # Responsive 2x2 Filter Grid (Section 5.1 & Finding F4/F12)
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            all_v_types = ["boost", "avoid", "neutral"]
            v_format_map = {"boost": "🚀 Boost", "avoid": "🛑 Avoid", "neutral": "⚖️ Neutral"}
            pick_v = st.multiselect(
                "Filter Verdict",
                all_v_types,
                default=all_v_types,
                format_func=lambda x: v_format_map.get(x, x),
                key="scout_v_pick",
            )
        with col_f2:
            all_v_pos = sorted(set(str(v["pos"]) for v in verdicts if v.get("pos") and v.get("pos") != "—"))
            pick_v_pos = st.multiselect("Filter Position", all_v_pos, default=all_v_pos, key="scout_pos_pick")

        col_f3, col_f4 = st.columns(2)
        with col_f3:
            all_owners = ["All", "🤖 Robowner", "👥 Rival", "🆓 Free Agent"]
            pick_owner = st.selectbox("Filter Roster", all_owners, index=0, key="scout_owner_pick")
        with col_f4:
            sort_options = [
                "⏱️ Most Recently Scouted (Newest First)",
                "⏳ Oldest Scouted First",
                "📰 Most Recent News Update",
                "🕰️ Oldest News Update",
                "🔤 Player Last Name (A-Z)",
                "🔤 Player Last Name (Z-A)",
                "🎯 Highest Confidence First",
                "🚀 Boosts First",
                "🛑 Avoids First",
                "🚑 Injured First",
            ]
            pick_sort = st.selectbox("Sort By", sort_options, index=0, key="scout_sort_pick")

        v_search = st.text_input("🔍 Search Player, Team, Injury, News, or LLM Reasoning...", "", key="scout_search_input")

        # Apply filtering
        filtered_v = [
            v for v in verdicts
            if v["verdict"] in pick_v and v["pos"] in pick_v_pos
        ]
        if pick_owner != "All":
            filtered_v = [v for v in filtered_v if v["ownership"] == pick_owner]

        if v_search.strip():
            term = v_search.strip().lower()
            filtered_v = [
                v for v in filtered_v
                if term in v["player"].lower()
                or term in v["team"].lower()
                or term in v["status"].lower()
                or term in v["reason"].lower()
                or term in v["latest_news"].lower()
                or term in v["return_outlook"].lower()
            ]

        # Apply sorting (Section 4.2)
        if pick_sort == "⏱️ Most Recently Scouted (Newest First)":
            filtered_v.sort(key=lambda x: x["judged_at"], reverse=True)
        elif pick_sort == "⏳ Oldest Scouted First":
            filtered_v.sort(key=lambda x: x["judged_at"])
        elif pick_sort == "📰 Most Recent News Update":
            filtered_v.sort(key=lambda x: x["news_updated"], reverse=True)
        elif pick_sort == "🕰️ Oldest News Update":
            filtered_v.sort(key=lambda x: x["news_updated"])
        elif pick_sort == "🔤 Player Last Name (A-Z)":
            filtered_v.sort(key=lambda x: (x["last_name"].lower(), x["first_name"].lower()))
        elif pick_sort == "🔤 Player Last Name (Z-A)":
            filtered_v.sort(key=lambda x: (x["last_name"].lower(), x["first_name"].lower()), reverse=True)
        elif pick_sort == "🎯 Highest Confidence First":
            filtered_v.sort(key=lambda x: x["confidence"], reverse=True)
        elif pick_sort == "🚀 Boosts First":
            filtered_v.sort(key=lambda x: (x["verdict"] != "boost", x["verdict"] != "neutral", -x["confidence"]))
        elif pick_sort == "🛑 Avoids First":
            filtered_v.sort(key=lambda x: (x["verdict"] != "avoid", x["verdict"] != "neutral", -x["confidence"]))
        elif pick_sort == "🚑 Injured First":
            filtered_v.sort(key=lambda x: ("Active" in x["status"], -x["judged_at"]))

        st.caption(f"Showing **{len(filtered_v)}** of **{len(verdicts)}** verdicts | Click any row to view complete Player Dossier.")

        df_v = pd.DataFrame(filtered_v)
        if not df_v.empty:
            # Selection Index Safety (Finding F9): reset index before render
            df_v = df_v.reset_index(drop=True)
            display_cols = [
                "scouted_dt", "scouted_age", "player", "pos", "team", "ownership", "status",
                "verdict_badge", "confidence", "reason", "latest_news", "return_outlook", "news_dt", "news_age"
            ]
            ev_v = st.dataframe(
                df_v[display_cols],
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="scout_verdicts_table",
                row_height=65,
                column_config={
                    "scouted_dt": st.column_config.DatetimeColumn(
                        "Scouted", format="MMM DD, HH:mm", width="medium",
                        help="When the LLM scout evaluated this player (click to sort chronologically)"
                    ),
                    "scouted_age": st.column_config.TextColumn("Age", width="small", help="Recency of the scout evaluation"),
                    "player": st.column_config.TextColumn("Player", width="medium"),
                    "pos": st.column_config.TextColumn("Pos", width="small"),
                    "team": st.column_config.TextColumn("Team", width="small"),
                    "ownership": st.column_config.TextColumn("Roster", width="small", help="Roster ownership status"),
                    "status": st.column_config.TextColumn("Status", width="medium", help="Current injury status & notes"),
                    "verdict_badge": st.column_config.TextColumn("Verdict", width="small"),
                    "confidence": st.column_config.NumberColumn("Conf", format="%.2f", width="small"),
                    "reason": st.column_config.TextColumn("Scout Analysis", width="large", help="LLM qualitative reasoning and volume context"),
                    "latest_news": st.column_config.TextColumn("Latest News / Headline", width="large", help="Most recent reporting snippet from ESPN/Sleeper"),
                    "return_outlook": st.column_config.TextColumn("Return Outlook", width="medium", help="Consolidated expected return timeline & basis"),
                    "news_dt": st.column_config.DatetimeColumn(
                        "News Updated", format="MMM DD, HH:mm", width="medium",
                        help="When news provider last updated this player (click to sort chronologically)"
                    ),
                    "news_age": st.column_config.TextColumn("News Age", width="small", help="Recency of provider news update"),
                },
            )
            ui_player_card.attach_player_selection(df_v, ev_v, id_col="player_id")
        else:
            st.info("No scout verdicts match the current filters.")
    else:
        st.info("No scout prose verdicts found in data/news_verdicts.json.")


# ==============================================================================
# TAB 3: DEAD-HEAT ARBITRATIONS
# ==============================================================================
with ai_tabs[2]:
    st.markdown("### Qualitative LLM Dead-Heat Arbitrations")
    st.caption(
        "Proposal 2B: When quantitative modeling shows a negligible delta (lineup gain <= 1.5 pts or ROS diff <= 5.0 pts) "
        "between two players in the same quality tier, local Ollama (qwen3.8:27b-mtp-80k) evaluates qualitative beat reporting. "
        "The incumbent is protected against lateral churn unless reporting demonstrates clear catalyst divergence."
    )

    @st.cache_data(ttl=60, show_spinner=False)
    def _load_all_arbitrations() -> list[dict]:
        path = DATA / "dead_heat_arbitrations.json"
        if not path.is_file():
            return []
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            players = sleeper_read.players()
            out = []
            for key, rec in d.items():
                parts = key.split("_")
                add_id = parts[0] if len(parts) > 0 else "?"
                drop_id = parts[1] if len(parts) > 1 else "?"
                add_name = sleeper_read.player_name(players, add_id)
                drop_name = sleeper_read.player_name(players, drop_id)
                out.append({
                    "key": key,
                    "week": rec.get("week"),
                    "challenger": add_name,
                    "challenger_id": add_id,
                    "incumbent": drop_name,
                    "incumbent_id": drop_id,
                    "verdict": rec.get("verdict"),
                    "confidence": float(rec.get("confidence") or 0.0),
                    "source": rec.get("source"),
                    "time": float(rec.get("time") or 0.0),
                    "reason": rec.get("reason", ""),
                    "fingerprint": rec.get("fingerprint"),
                })
            out.sort(key=lambda x: x["time"], reverse=True)
            return out
        except Exception:
            return []

    @st.cache_data(ttl=120, show_spinner=False)
    def _find_arbitration_event_context(fingerprint: str | None, add_id: str, drop_id: str) -> dict:
        if not fingerprint:
            return {}
        try:
            run = decision_audit.find(fingerprint)
            if not run:
                return {}
            raw = run.get("raw") or {}
            matching_opt = None
            for opt in (raw.get("options") or []):
                o_add = str((opt.get("add") or {}).get("player_id") or "")
                o_drop = str((opt.get("drop") or {}).get("player_id") or "")
                if o_add == str(add_id) and o_drop == str(drop_id):
                    matching_opt = opt
                    break

            sim_gain = None
            ros_diff = None
            add_ros = None
            drop_ros = None
            add_q = None
            drop_q = None
            beat_snippets = []

            if matching_opt:
                sim_gain = matching_opt.get("gain")
                ros_diff = matching_opt.get("ros_diff")
                add_ros = matching_opt.get("add_ros")
                drop_ros = matching_opt.get("drop_ros")
                add_q = matching_opt.get("add_q")
                drop_q = matching_opt.get("drop_q")

            timing = raw.get("timing") or {}
            if timing.get("advisory_reviews"):
                for rev in timing["advisory_reviews"]:
                    if str(rev.get("player_id")) in (str(add_id), str(drop_id)):
                        beat_snippets.append({
                            "player": rev.get("name") or str(rev.get("player_id")),
                            "verdict": rev.get("verdict"),
                            "reason": rev.get("reason"),
                            "return_basis": rev.get("return_basis"),
                        })

            return {
                "found": True,
                "run_kind": run.get("kind"),
                "run_mode": run.get("mode"),
                "gain": sim_gain,
                "ros_diff": ros_diff,
                "add_ros": add_ros,
                "drop_ros": drop_ros,
                "add_q": add_q,
                "drop_q": drop_q,
                "beat_snippets": beat_snippets,
            }
        except Exception:
            return {}

    def _is_legacy_truncated(arb: dict) -> bool:
        if arb.get("key") in {"11603_6806_3", "7528_13296_4"}:
            return True
        reason = (arb.get("reason") or "").strip()
        words = reason.split()
        if len(words) == 50 and not reason.endswith((".", "!", "?", '"', "'")):
            return True
        return False

    arbs = _load_all_arbitrations()
    if arbs:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Arbitrations", len(arbs))
        keeps = sum(1 for a in arbs if a["verdict"] == "KEEP_INCUMBENT")
        swaps = sum(1 for a in arbs if a["verdict"] == "SWAP_FOR_CANDIDATE")
        c2.metric("Kept Incumbent", keeps, f"{keeps / len(arbs):.0%}")
        c3.metric("Approved Swaps", swaps, f"{swaps / len(arbs):.0%}")
        avg_conf = sum(a["confidence"] for a in arbs) / len(arbs)
        c4.metric("Avg Confidence", f"{avg_conf:.2f}")

        st.markdown("#### Recorded Arbitrations Dossier")
        for a in arbs:
            dt = datetime.fromtimestamp(a["time"])
            is_swap = a["verdict"] == "SWAP_FOR_CANDIDATE"
            verdict_badge = "🔄 SWAP FOR CANDIDATE" if is_swap else "🛡️ KEEP INCUMBENT"
            verdict_kind = "cyan" if is_swap else "emerald"
            border_color = "#0284c7" if is_swap else "#10b981"
            is_legacy = _is_legacy_truncated(a)

            # Look up paired event context authentically (Finding F2)
            ctx = _find_arbitration_event_context(a.get("fingerprint"), a["challenger_id"], a["incumbent_id"])

            with st.container():
                chips = [
                    f"<b>Week {a['week']}</b>",
                    f"<b>{dt:%b %d, %Y · %H:%M}</b> ({ui.fmt_age(a['time'])})",
                    ui.status_chip(verdict_badge, verdict_kind),
                    ui.status_chip(f"Confidence: {a['confidence']:.2f}", "neutral"),
                    ui.status_chip("qwen3.8:27b-mtp-80k", "blue"),
                ]
                if is_legacy:
                    chips.append(ui.status_chip("⚠️ Legacy record (evaluated prior to length-limit fix)", "amber"))

                st.markdown(
                    f"<div style='border-left: 4px solid {border_color}; padding: 10px 14px; margin-bottom: 12px; "
                    f"background: rgba(128,128,128,0.06); border-radius: 0 8px 8px 0;'>"
                    f"<div style='display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px;'>"
                    + "".join(chips) +
                    f"</div>"
                    f"<div style='font-size: 1.05rem; margin-top: 4px;'>"
                    f"Challenger: <b>{a['challenger']}</b> vs Incumbent: <b>{a['incumbent']}</b>"
                    f"</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # Context Metrics
                m_cols = st.columns(4)
                gain_val = f"{ctx['gain']:+.2f} pts" if ctx.get("gain") is not None else "—"
                ros_val = f"{ctx['ros_diff']:+.2f} pts" if ctx.get("ros_diff") is not None else "—"
                m_cols[0].metric("Target Add", a["challenger"], help=f"Challenger player ID: {a['challenger_id']}")
                m_cols[1].metric("Incumbent Drop", a["incumbent"], help=f"Incumbent player ID: {a['incumbent_id']}")
                m_cols[2].metric("Recorded Lineup Gain", gain_val, help="Simulated lineup delta recorded in paired event audit")
                m_cols[3].metric("Recorded ROS Delta", ros_val, help="Rest-of-season projection delta recorded at evaluation time")

                # Unabridged Reasoning Block
                st.info(f"**Ollama Qualitative Reasoning:**  \n{a['reason']}")

                # Context Expander: Underlying Beat Reporting
                snippets = ctx.get("beat_snippets") or []
                with st.expander("📰 Underlying Beat Reporting & Evaluation Audit"):
                    if snippets:
                        for snip in snippets:
                            st.markdown(f"**{snip['player']}** ({snip.get('verdict', 'scouted')}):")
                            st.markdown(f"> {snip.get('reason') or 'No reason text'}")
                            if snip.get("return_basis"):
                                st.caption(f"Basis: {snip['return_basis']}")
                    elif a.get("fingerprint"):
                        st.caption(f"Event Fingerprint: `{a['fingerprint']}` (event file not preserved locally; recorded reason above is canonical).")
                    else:
                        st.caption("No underlying event fingerprint recorded for this entry.")

                # Action Buttons (Touch targets >= 42px)
                b_cols = st.columns([1, 1, 3])
                with b_cols[0]:
                    if st.button(f"🔍 Dossier: {a['challenger']}", key=f"arb_add_{a['key']}", use_container_width=True):
                        ui_player_card.show_player_card(a["challenger_id"], week=a["week"])
                with b_cols[1]:
                    if st.button(f"🔍 Dossier: {a['incumbent']}", key=f"arb_drop_{a['key']}", use_container_width=True):
                        ui_player_card.show_player_card(a["incumbent_id"], week=a["week"])

                st.divider()
    else:
        st.info("No dead-heat qualitative arbitrations recorded yet.")
