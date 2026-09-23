"""AI & Scout Center — Qualitative LLM Arbitrations & Reporting Intelligence.

Provides first-class auditing for all LLM-driven reasoning:
  1. Dead-Heat Arbitrations: History of near-tie/dead-heat move proposals evaluated by local Ollama (qwen3.8:27b-mtp-96k).
  2. Scout Prose Verdicts: Unredacted RotoWire/RotoBaller prose scouting and Codex/Qwen evaluations from data/news_verdicts.json.
  3. Scout Queue Backlog: Real-time monitoring of the rate-limited prose queue, priority tiers, and GPU drain cadence.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import re

import pandas as pd
import streamlit as st

from robo import DATA, ROOT, scout_queue, season, sleeper_read, ui, ui_player_card

st.title("🤖 AI & Scout Center")
ui.gate_banner(st)

# Universal Search & Query Params
col_s1, col_s2 = st.columns([3, 1])
with col_s1:
    ui_player_card.render_player_search_bar(key="ai_scout_search")
ui_player_card.check_query_params_player()

ai_tabs = st.tabs([
    "⚖️ Dead-Heat Arbitrations",
    "📰 Weekly Scout Verdicts",
    "🚦 Scout Queue Monitor"
])


# ==============================================================================
# TAB 1: DEAD-HEAT ARBITRATIONS
# ==============================================================================
with ai_tabs[0]:
    st.markdown("### Qualitative LLM Dead-Heat Arbitrations")
    st.caption(
        "Proposal 2B: When quantitative modeling shows a negligible delta (lineup gain <= 1.5 pts or ROS diff <= 5.0 pts) "
        "between two players in the same quality tier, local Ollama (qwen3.8:27b-mtp-96k) evaluates qualitative beat reporting. "
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

        st.markdown("#### Recorded Arbitrations Log")
        for a in arbs:
            dt = datetime.fromtimestamp(a["time"])
            verdict_badge = "🛡️ KEEP INCUMBENT" if a["verdict"] == "KEEP_INCUMBENT" else "🔄 SWAP FOR CANDIDATE"
            box_border = "green" if a["verdict"] == "SWAP_FOR_CANDIDATE" else "blue"

            with st.container():
                st.markdown(
                    f"<div style='border-left: 4px solid {box_border}; padding-left: 12px; margin-bottom: 14px;'>"
                    f"<b>Week {a['week']} · {dt:%b %d, %H:%M}</b> — "
                    f"Challenger <b>{a['challenger']}</b> vs Incumbent <b>{a['incumbent']}</b><br/>"
                    f"<b>Verdict:</b> <code>{a['verdict']}</code> (Confidence: <code>{a['confidence']:.2f}</code>) via <code>{a['source']}</code>"
                    f"</div>",
                    unsafe_allow_html=True
                )
                st.info(f"**Ollama Reasoning:** {a['reason']}")
                b_cols = st.columns([1, 1, 4])
                with b_cols[0]:
                    if st.button(f"🔍 {a['challenger']}", key=f"arb_add_{a['key']}"):
                        ui_player_card.show_player_card(a["challenger_id"], week=a["week"])
                with b_cols[1]:
                    if st.button(f"🔍 {a['incumbent']}", key=f"arb_drop_{a['key']}"):
                        ui_player_card.show_player_card(a["incumbent_id"], week=a["week"])
                st.divider()
    else:
        st.info("No dead-heat qualitative arbitrations recorded yet.")


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

            # Load auxiliary news & weekly feeds from news_watch.json
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

                # Formatted dates
                scouted_str = f"{datetime.fromtimestamp(j_ts).strftime('%b %d %H:%M')} ({ui.fmt_age(j_ts)})" if j_ts else "—"
                news_str = f"{datetime.fromtimestamp(n_ts).strftime('%b %d %H:%M')} ({ui.fmt_age(n_ts)})" if n_ts else "—"

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
                    "player": rec.get("name") or sleeper_read.player_name(players, str(pid)),
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
                    "scouted": scouted_str,
                    "last_news": news_str,
                    "judged_at": j_ts,
                    "news_updated": n_ts,
                    "timing_sentence": rec.get("timing_sentence") or "",
                })

            # Default sort: most recently scouted first
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

        # Filters & Sorting Controls
        vf1, vf2, vf3, vf4 = st.columns([2, 2, 2, 3])
        with vf1:
            all_v_types = ["boost", "avoid", "neutral"]
            v_format_map = {"boost": "🚀 Boost", "avoid": "🛑 Avoid", "neutral": "⚖️ Neutral"}
            pick_v = st.multiselect(
                "Filter Verdict",
                all_v_types,
                default=all_v_types,
                format_func=lambda x: v_format_map.get(x, x),
                key="scout_v_pick",
            )
        with vf2:
            all_v_pos = sorted(set(str(v["pos"]) for v in verdicts if v.get("pos") and v.get("pos") != "—"))
            pick_v_pos = st.multiselect("Filter Position", all_v_pos, default=all_v_pos, key="scout_pos_pick")
        with vf3:
            all_owners = ["All", "🤖 Robowner", "👥 Rival", "🆓 Free Agent"]
            pick_owner = st.selectbox("Filter Roster", all_owners, index=0, key="scout_owner_pick")
        with vf4:
            sort_options = [
                "⏱️ Most Recently Scouted (Default)",
                "📰 Most Recent News Update",
                "🎯 Highest Confidence",
                "🚀 Boosts First",
                "🛑 Avoids First",
                "🚑 Injured / Out First",
                "🔤 Player Name (A-Z)",
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

        # Apply sorting
        if pick_sort == "⏱️ Most Recently Scouted (Default)":
            filtered_v.sort(key=lambda x: x["judged_at"], reverse=True)
        elif pick_sort == "📰 Most Recent News Update":
            filtered_v.sort(key=lambda x: x["news_updated"], reverse=True)
        elif pick_sort == "🎯 Highest Confidence":
            filtered_v.sort(key=lambda x: x["confidence"], reverse=True)
        elif pick_sort == "🚀 Boosts First":
            filtered_v.sort(key=lambda x: (x["verdict"] != "boost", x["verdict"] != "neutral", -x["confidence"]))
        elif pick_sort == "🛑 Avoids First":
            filtered_v.sort(key=lambda x: (x["verdict"] != "avoid", x["verdict"] != "neutral", -x["confidence"]))
        elif pick_sort == "🚑 Injured / Out First":
            filtered_v.sort(key=lambda x: ("Active" in x["status"], -x["judged_at"]))
        elif pick_sort == "🔤 Player Name (A-Z)":
            filtered_v.sort(key=lambda x: x["player"])

        st.caption(f"Showing **{len(filtered_v)}** of **{len(verdicts)}** verdicts | Click any row to view complete Player Dossier.")

        df_v = pd.DataFrame(filtered_v)
        if not df_v.empty:
            df_v = df_v.reset_index(drop=True)
            display_cols = [
                "scouted", "player", "pos", "team", "ownership", "status",
                "verdict_badge", "confidence", "reason", "latest_news", "return_outlook", "last_news"
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
                    "scouted": st.column_config.TextColumn("Scouted", width="medium", help="When the LLM scout evaluated this player"),
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
                    "last_news": st.column_config.TextColumn("News Provider Updated", width="medium", help="When news provider last updated this player"),
                }
            )
            ui_player_card.attach_player_selection(df_v, ev_v, id_col="player_id")
        else:
            st.info("No scout verdicts match the current filters.")
    else:
        st.info("No scout prose verdicts found in data/news_verdicts.json.")


# ==============================================================================
# TAB 3: SCOUT QUEUE MONITOR
# ==============================================================================
with ai_tabs[2]:
    st.markdown("### Scout Prose Queue & Processing Backlog")
    st.caption(
        "One durable queue for all automated prose scouting (news pulse, cascades, daily refresh). "
        "Each pulse drains up to ten batches of eight players within a ten-minute budget, "
        "as background work that yields the GPU to image generation."
    )

    try:
        q_stat = scout_queue.status()
        qc1, qc2, qc3 = st.columns(3)
        qc1.metric("Pending in Queue", q_stat.get("queued", 0))
        qc2.metric("Attention Required", q_stat.get("attention", 0))
        last_b = q_stat.get("last_batch_at")
        qc3.metric("Last Batch Processed", ui.fmt_age(last_b) if last_b else "Never")

        # Graphical Representation of Queue & Pulse History
        st.markdown("#### 📈 Queue Depth & Pulse Activity History")
        log_path = ROOT / "news-watch.log"
        if log_path.is_file():
            w_col1, _ = st.columns([2, 3])
            with w_col1:
                window_choice = st.selectbox(
                    "Timeline Window",
                    ["6 hours", "12 hours", "24 hours", "48 hours"],
                    index=1,
                    key="scout_q_window",
                )
            hours = int(window_choice.split()[0])
            cutoff = datetime.now() - pd.Timedelta(hours=hours)

            pattern = re.compile(r"\[(.*?)\] (.*)")
            log_rows = []
            try:
                with open(log_path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        m = pattern.match(line.strip())
                        if not m:
                            continue
                        ts_str, msg = m.groups()
                        try:
                            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            continue
                        if ts >= cutoff:
                            q_m = re.search(r"(\d+)\s+advisory queued", msg)
                            ev_m = re.search(r"(\d+)\s+event\(s\)", msg)
                            events = int(ev_m.group(1)) if ev_m else 0
                            queued = int(q_m.group(1)) if q_m else None
                            if queued is None and ("acted" in msg or "quiet" in msg):
                                queued = 0
                            status = (
                                "Paused" if "paused:" in msg
                                else ("Acted" if "acted" in msg
                                else ("Quiet" if "quiet" in msg
                                else "Burst/Other"))
                            )
                            log_rows.append({
                                "Time": ts.strftime("%H:%M"),
                                "Timestamp": ts,
                                "Pending in Queue": queued,
                                "News Events": events,
                                "Status": status,
                                "Log Message": msg[:100],
                            })
            except Exception as e:
                st.warning(f"Could not read news-watch log: {e}")

            if log_rows:
                df_hist = pd.DataFrame(log_rows)
                df_hist["Pending in Queue"] = df_hist["Pending in Queue"].ffill().fillna(0).astype(int)
                # Indexed on the datetime, not the "HH:MM" label: a string axis
                # is sorted alphabetically, which put yesterday evening's pulses
                # to the right of this morning's in any window crossing midnight.
                st.area_chart(
                    df_hist.set_index("Timestamp")[["Pending in Queue"]],
                    use_container_width=True,
                    height=230,
                    color="#29B5E8",
                )
                with st.expander(f"🔍 View Pulse Log Breakdown ({window_choice})"):
                    st.dataframe(
                        df_hist[["Time", "Pending in Queue", "News Events", "Status", "Log Message"]],
                        use_container_width=True,
                        hide_index=True,
                    )

        q_path = DATA / "scout_queue.json"
        if q_path.is_file():
            q_data = json.loads(q_path.read_text(encoding="utf-8"))
            items = list((q_data.get("items") or {}).values())
            pending = [x for x in items if x.get("disposition") == "pending"]
            attention = [x for x in items if x.get("disposition") == "attention"]
            completed = list(q_data.get("completed") or [])

            if attention:
                st.markdown("#### ⚠️ Attention Required (Failed Retries)")
                att_rows = []
                for item in attention:
                    att_rows.append({
                        "player": item.get("name") or str(item.get("player_id")),
                        "category": item.get("category", "background"),
                        "attempts": item.get("attempts", 0),
                        "last_error": item.get("last_error") or "—",
                        "enqueued_at": ui.fmt_age(item.get("enqueued_at")),
                    })
                st.dataframe(pd.DataFrame(att_rows), use_container_width=True, hide_index=True)

            if pending:
                st.markdown("#### ⏳ Pending Items in Queue")
                pending.sort(key=lambda x: (int(x.get("priority", 99)),
                                            float(x.get("enqueued_at") or 0),
                                            str(x.get("player_id", ""))))
                players = sleeper_read.players()
                q_rows = []
                for item in pending:
                    pid = str(item.get("player_id", ""))
                    name = item.get("name") or sleeper_read.player_name(players, pid)
                    q_rows.append({
                        "player": name,
                        "category": item.get("category", "background"),
                        "priority": item.get("priority", 3),
                        "attempts": item.get("attempts", 0),
                        "queued_at": ui.fmt_age(item.get("enqueued_at")),
                        "last_error": item.get("last_error") or "—",
                    })
                st.dataframe(pd.DataFrame(q_rows)[["player", "category", "priority", "attempts", "queued_at", "last_error"]], use_container_width=True, hide_index=True)
            else:
                st.success("✅ Scout queue is clear. No players pending evaluation.")

            if completed:
                with st.expander(f"📋 Recently Completed Evaluations ({len(completed)} total)"):
                    comp_rows = []
                    for item in reversed(completed[-50:]):
                        comp_rows.append({
                            "player": item.get("name") or str(item.get("player_id")),
                            "category": item.get("category", "—"),
                            "outcome": item.get("outcome", "—"),
                            "completed": ui.fmt_age(item.get("completed_at")),
                        })
                    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)

    except Exception as e:
        st.warning(f"Could not inspect scout queue: {e}")
