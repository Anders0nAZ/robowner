"""The slate as it stands: what Roboner would do right now, and on what roster.

IT READS THE LAST RECORDED RUN rather than computing a fresh one. The news
pulse runs every twenty minutes, so "last run" is rarely far behind, and an
audit tool that recomputes its own answer can no longer tell you what the bot
actually did -- which is the only question this app exists to answer. The
staleness is stated at the top instead of being hidden by a recomputation.
"""

import pandas as pd
import streamlit as st

from robo import decision_audit, narrate, ui, ui_player_card

st.title("🔍 What Roboner would do now")
ui.gate_banner(st)

col_s1, col_s2 = st.columns([3, 1])
with col_s1:
    ui_player_card.render_player_search_bar(key="now_player_search")
ui_player_card.check_query_params_player()



@st.cache_data(ttl=20, show_spinner=False)
def latest_run():
    return decision_audit.latest()


@st.cache_data(ttl=20, show_spinner="Reading the pending queue from Sleeper…")
def live_portfolio():
    """Two authenticated reads that also settle anything that has left the
    pending queue, so this runs on a timer rather than on every widget click."""
    from robo import waiver_manager
    return waiver_manager.status()


@st.cache_data(ttl=300, show_spinner="Reading the wire…")
def wire_split() -> dict:
    """How much of the wire can be ADDED versus has to be CLAIMED.

    Two very different pools and the slate treats them differently, so the
    counts belong beside it. A free agent is an outright add for nothing; a man
    on waivers costs FAAB and does not resolve until the league's waiver run,
    which is why the bot builds a ladder for one and a plain add for the other.
    """
    from robo import expected, season
    ids = list((expected.load().get("players") or {}))
    held = set(season.rostered_ids())
    states = season.transaction_states([p for p in ids if p not in held])
    free = sum(1 for v in states.values() if v.get("acquisition") == "free_now")
    wv = sum(1 for v in states.values()
             if v.get("acquisition") in {"weekly_waiver", "drop_waiver"})
    return {"free": free, "waivers": wv, "rostered": len(held)}


@st.cache_data(ttl=600, show_spinner="Pricing this week's defences…")
def defence_stream() -> dict:
    """This week's streaming decision, priced the way the bot prices it.

    DEFENCES ARE THE ONE THING THE VALUATION CANNOT ANSWER. expected.py models
    neither kickers nor defences, and Sleeper's weekly feed drops every
    `pts_allow` tier plus the return and turnover touchdowns, so a defence
    scored off that feed comes back near nothing and carries no matchup at all
    -- which for a defence is the entire question. ros.py therefore REPLACES a
    defence's number with the market. That makes this the only place in the app
    where "what should we do about our defence" can be asked, and it is a
    weekly recurring decision rather than a reaction to news, so it belongs
    beside the slate rather than in a valuation table it is absent from.

    Everything here is read through streaming.swap(), which is the same call
    the waiver planner makes -- not a second opinion assembled for display.
    """
    from robo import season, sleeper_read, streaming
    week = season.current_week()
    board = streaming.rank_week(week)
    if not board:
        return {"week": week, "board": [], "unpriced": True}
    players = sleeper_read.players()
    mine = {str(p) for p in (season.mine().get("players") or [])}
    held = {str(p) for p in season.rostered_ids()}
    ours = [p for p in mine if (players.get(p) or {}).get("position") == "DEF"]
    swaps = [{**streaming.swap(week, d), "ours": d} for d in sorted(ours)]
    return {"week": week, "board": board, "ours": ours, "swaps": swaps,
            "held": held, "mine": mine, "bar": streaming.MIN_STREAM_GAIN,
            "unpriced": False}


run = latest_run()
if not run:
    st.warning("No decision run has been recorded yet. Nothing to show until the news "
               "pulse or a scheduled pass writes its first record.")
    st.stop()

age = ui.fmt_age(run.get("at"))
trigger = ("news pulse" if run["kind"] == decision_audit.NEWS
           else f"scheduled `{run['mode']}` pass")
st.caption(f"As of the {trigger} **{age}** · week {run.get('week')} · {run['outcome']}. "
           "The pulse runs every twenty minutes; this page reports what that run decided "
           "rather than recomputing an answer of its own.")

# ---------------------------------------------------------------- the slate
st.subheader("The slate")
slate = decision_audit.slate(run)
if not slate:
    st.info(narrate.slate_absence(run))
else:
    df_now_slate = pd.DataFrame([{
        "channel": row.get("channel"),
        "rung": row.get("rung"),
        "add": row.get("add"),
        "drop": row.get("drop") or "(open spot)",
        "bid": row.get("bid"),
        "gain": row.get("gain"),
        "ceiling": row.get("ceiling"),
        "player_id": row.get("add_id") or (row.get("raw") or {}).get("add", {}).get("player_id"),
    } for row in slate])
    ev_now_slate = st.dataframe(
        df_now_slate[["channel", "rung", "add", "drop", "bid", "gain", "ceiling"]],
        use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key="now_slate_table",
        column_config={
            "channel": st.column_config.TextColumn(
                help="A free agent can be added outright. A waiver claim has to be bid "
                     "for and settles on the league's waiver run."),
            "rung": st.column_config.NumberColumn(
                help="Position on the ladder. Sleeper reaches claims in order and the "
                     "first winner takes the slot."),
            "bid": st.column_config.NumberColumn(format="$%d"),
            "gain": st.column_config.NumberColumn(
                format="%+.2f",
                help="Change in our optimal starting lineup, averaged over simulated "
                     "seasons."),
            "ceiling": st.column_config.NumberColumn(format="%.2f"),
        })
    ui_player_card.attach_player_selection(df_now_slate, ev_now_slate, id_col="player_id", week=run.get("week"))
    for row in slate:
        if row["channel"] == "waiver claim":
            st.caption(ui.money(narrate.claim_story(row["raw"])))

# THE POOL AS THAT RUN SAW IT, not as it stands now. A live count beside a
# recorded slate is how the page came to show "free now" for a man the slate was
# still bidding on -- two vintages, no seam, and the reader left to guess which
# one the bot acted on. The recorded figure is preferred and the live one is
# labelled as a different question when it has to be used.
pool = decision_audit.claim_pool(run)
if pool:
    teams = ", ".join(pool.get("waiver_teams") or []) or "no team"
    st.caption(ui.money(
        f"The wire as that run saw it: **{pool['waiver_pool_size']} on waivers** "
        f"({teams} — this week's completed games), who have to be bid for and do "
        f"not resolve until the league's waiver run, against "
        f"**{pool['free_pool_size']} free now**, addable outright for nothing. A "
        f"claim in the slate above is one of the first kind; a free-agent row is "
        f"one of the second."))
    horizon = decision_audit.claim_horizon(run)
    if horizon and horizon.get("settles_label"):
        st.caption(ui.money(
            f"A claim built in that run settles **{horizon['settles_label']}**, so it "
            f"was priced from **week {horizon['week']}** — the first week it could "
            f"actually be played in. Everything before that is a week the claim "
            f"cannot reach."))
    floor = decision_audit.wire_floor(run)
    if floor:
        worst = max(floor, key=lambda r: r.get("pct") or 0)
        who = worst.get("supplier")
        held_out = worst.get("excluded") or 0
        st.caption(ui.money(
            f"How much of each slot the wire already supplies, measured against "
            f"the same pool that priced the gains above"
            + (f" — {held_out} candidates this run was considering are held out "
               f"of it, so they are not their own baseline" if held_out else "")
            + ". A high percentage means depth there is a wasted roster spot. "
            + " · ".join(f"**{r['pos']} {r['pct']:.0%}**" for r in floor)
            + (f". The best {worst['pos']} on offer is {who}." if who else "")))
else:
    try:
        w = wire_split()
        st.caption(
            f"That run recorded no pool, so this is the wire **as it stands now**, "
            f"which is a different question from what it was when the slate was "
            f"built: **{w['free']} free now**, **{w['waivers']} on waivers**, "
            f"{w['rostered']} on somebody's roster.")
    except Exception as e:
        st.caption(f"Wire split unavailable: {type(e).__name__}")

st.markdown(f"[Open this decision front to back →](Moves?run={run['fingerprint']})")


# --------------------------------------------------------- defence stream
st.subheader("The defence")
try:
    d = defence_stream()
    if d.get("unpriced"):
        st.info(f"No lines are posted for week {d['week']} yet, so there is no ranking — "
                "only the absence of one. A defence is priced off the opponent's implied "
                "point total and nothing else, so an unpriced week cannot be ranked.")
    elif not d.get("ours"):
        st.warning("No defence on the roster. A starting slot with nobody in it is a "
                   "patch, which outranks every ordinary upgrade.")
    else:
        for s in d["swaps"]:
            mine_row, best = s.get("mine") or {}, s.get("best") or {}
            gain, bar = float(s.get("gain") or 0.0), d["bar"]
            streams = gain >= bar and not s.get("locked")
            c = st.columns(4)
            c[0].metric("We hold", s["ours"],
                        f"{mine_row.get('pts', 0):.2f} pts" if mine_row else "unpriced",
                        delta_color="off",
                        help="Expected points off the opponent's implied total, on this "
                             "league's own scoring.")
            c[1].metric("Best free", best.get("team", "—"),
                        f"{best.get('pts', 0):.2f} pts" if best else None,
                        delta_color="off",
                        help="Best defence actually acquirable — not the best on the "
                             "board. Ranking all thirty-two would propose a move Sleeper "
                             "cannot execute.")
            c[2].metric("Gain", f"{gain:+.2f}")
            c[3].metric("Verdict", "stream" if streams else "hold")
            if s.get("locked"):
                st.caption(f"Hold {s['ours']}: its game has already locked.")
            else:
                st.caption(
                    f"{s.get('why', '')}. Streaming needs **{bar:+.2f}**; this is "
                    f"{gain:+.2f}, so the bot "
                    + ("**streams**." if streams else "**holds**.")
                    + " A defence refills from the wire every week, which is why the bar "
                      "is a fixed gain rather than a comparison of season totals.")

    board = d.get("board") or []
    if board:
        with st.expander(f"Every defence, week {d['week']} — best matchup first"):
            st.caption("Ranked purely by the opponent's implied point total, fitted on "
                       "2,174 of this league's own defence weeks: 11.39 points against "
                       "the weakest offences down to 4.72 against the strongest, monotone "
                       "across all eight buckets. The same fit refuses to rank kickers — "
                       "on 1,478 kicker weeks it runs flat and non-monotone, so there is "
                       "no kicker equivalent of this table and that is deliberate.")
            st.dataframe(pd.DataFrame([{
                "defence": r["team"],
                "owner": ("ours" if r["team"] in d["mine"] else
                          "rostered" if r["team"] in d["held"] else "free"),
                "opponent": ("vs " if r["home"] else "@ ") + r["opponent"],
                "opponent implied total": r["implied"],
                "expected points": r["pts"],
            } for r in board]), use_container_width=True, hide_index=True, height=380,
                column_config={
                    "opponent implied total": st.column_config.NumberColumn(
                        format="%.2f",
                        help="What the betting market expects the opposing offence to "
                             "score. Lower is better for the defence."),
                    "expected points": st.column_config.NumberColumn(format="%.2f"),
                })
except Exception as e:  # nflverse lines or Sleeper unavailable
    st.warning(f"Defence streaming unavailable: {type(e).__name__}. A defence is priced "
               "off the betting market, so an unreadable schedule means no ranking.")

# ------------------------------------------------------- roster it acts on
st.subheader("The roster it would act on")
state = run.get("roster_state") or {}
cols = st.columns(5)
cols[0].metric("Active", state.get("active") or "—",
               help="Players on the active roster when the run began.")
cols[1].metric("Open spots", state.get("open") if state.get("open") is not None else "—",
               help="Every open spot is a claim that needs no drop. An unswept injured-"
                    "reserve list is a roster cap that silently blocks pickups.")
cols[2].metric("FAAB left", f"${run.get('faab_left', 0)}")
cols[3].metric("Worst case", f"${run.get('worst_case_faab', 0)}",
               help="Most the whole slate could cost if every claim won.")
hours = run.get("hours_to_kickoff")
cols[4].metric("To kickoff", f"{float(hours):.1f}h" if hours is not None else "—",
               help="Ordinary upgrades refuse inside the blackout window and say so.")

# ------------------------------------------------------- live pending queue
st.subheader("Already in Sleeper's queue")
try:
    live = live_portfolio()
    p = st.columns(4)
    p[0].metric("Pending now", len(live.get("pending") or []))
    p[1].metric("Bot-owned", len(live.get("owned") or []))
    p[2].metric("Foreign", len(live.get("foreign") or []))
    p[3].metric("Worst-case exposure", f"${live.get('worst_case_exposure', 0)}")
    if live.get("foreign"):
        st.error("Automation is blocked: at least one pending claim is not in the bot "
                 "ownership ledger. A claim the bot did not create is preserved, never "
                 "cancelled, and it stops the controller until somebody looks.")
    elif not live.get("pending"):
        st.caption("Nothing is pending. Claims appear here once the transaction gate "
                   "opens and a slate is submitted.")
    # 1. Live Active Pending Claims Table
    owned_claims = live.get("owned") or []
    if owned_claims:
        st.markdown("##### Confirmed Active Claims on Sleeper")
        active_by_id = {
            str(txid): item
            for txid, item in (live.get("state", {}).get("active") or {}).items()
        }
        owned_rows = []
        for s in owned_claims:
            txid = next(
                (tx for tx, item in active_by_id.items()
                 if (item.get("spec") or {}).get("add_id") == s.get("add_id")
                 and (item.get("spec") or {}).get("drop_id") == s.get("drop_id")),
                "—"
            )
            item = active_by_id.get(txid) or {}
            sub_at = item.get("submitted_at")
            rung = s.get("priority", s.get("submit_order", 0))
            gain = s.get("gain")
            gid = s.get("group_id") or "—"
            cap = s.get("capacity", 1)
            owned_rows.append({
                "rung": f"#{rung}",
                "add": s.get("add_name") or s.get("add_id"),
                "drop": s.get("drop_name") or s.get("drop_id") or "(open spot)",
                "bid": int(s.get("bid") or 0),
                "gain": float(gain) if gain is not None else None,
                "group": f"{gid} (max {cap})",
                "tx_id": txid,
                "submitted": ui.fmt_age(sub_at) if sub_at else "—",
                "player_id": s.get("add_id"),
            })
        owned_rows.sort(key=lambda r: int(r["rung"].replace("#", "")))
        df_owned = pd.DataFrame(owned_rows)
        ev_owned = st.dataframe(
            df_owned[["rung", "add", "drop", "bid", "gain", "group", "tx_id", "submitted"]],
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="now_active_claims_table",
            column_config={
                "rung": st.column_config.TextColumn("Rung", width="small"),
                "add": st.column_config.TextColumn("Target Add", width="medium"),
                "drop": st.column_config.TextColumn("Incumbent Drop", width="medium"),
                "bid": st.column_config.NumberColumn("FAAB Bid", format="$%d"),
                "gain": st.column_config.NumberColumn("Lineup Gain", format="%+.2f pts"),
                "group": st.column_config.TextColumn("Portfolio Group", width="medium"),
                "tx_id": st.column_config.TextColumn("Sleeper Tx ID", width="medium"),
                "submitted": st.column_config.TextColumn("Submitted", width="small"),
            }
        )
        ui_player_card.attach_player_selection(df_owned, ev_owned, id_col="player_id", week=run.get("week"))

    # 2. Lifecycle Audit History Table (Submissions, Cancellations, Reconciliations)
    events = live.get("events") or []
    if events:
        with st.expander("Waiver Lifecycle & Audit Trail: Submissions, Cancellations, Reconciliations", expanded=True):
            hist_rows = []
            for e in events:
                kind = (e.get("kind") or "event").upper()
                spec = e.get("spec") or {}
                reason = e.get("reason") or e.get("error") or (e.get("result") or {}).get("status") or ""
                txid = str(e.get("transaction_id") or "")
                
                if kind == "RECONCILED":
                    n_des = len(e.get("desired") or [])
                    n_canc = len(e.get("cancelled") or [])
                    n_sub = len(e.get("submitted") or [])
                    add = f"Portfolio ({n_des} rungs)"
                    drop = "—"
                    bid = None
                    priority = "—"
                    gain = None
                    detail = f"Reconciled portfolio: {n_canc} cancelled, {n_sub} submitted"
                elif kind in ("SUBMITTED", "CANCELLED"):
                    add = spec.get("add_name") or spec.get("add_id") or "—"
                    drop = spec.get("drop_name") or spec.get("drop_id") or "(open spot)"
                    bid = int(spec.get("bid")) if spec.get("bid") is not None else None
                    p_val = spec.get("priority", spec.get("submit_order"))
                    priority = f"#{p_val}" if p_val is not None else "—"
                    gain = float(spec.get("gain")) if spec.get("gain") is not None else None
                    src = e.get("source") or "live"
                    detail = f"Cancelled ({reason})" if kind == "CANCELLED" else f"Submitted to Sleeper ({src})"
                elif kind == "SETTLED":
                    add = spec.get("add_name") or spec.get("add_id") or "—"
                    drop = spec.get("drop_name") or spec.get("drop_id") or "(open spot)"
                    bid = int(spec.get("bid")) if spec.get("bid") is not None else None
                    priority = f"#{spec.get('priority', 0)}"
                    gain = float(spec.get("gain")) if spec.get("gain") is not None else None
                    res = (e.get("result") or {}).get("status") or "settled"
                    detail = f"Settled: {res}"
                else:
                    add = spec.get("add_name") or spec.get("add_id") or "—"
                    drop = spec.get("drop_name") or spec.get("drop_id") or "—"
                    bid = int(spec.get("bid")) if spec.get("bid") is not None else None
                    priority = str(spec.get("priority", "—"))
                    gain = float(spec.get("gain")) if spec.get("gain") is not None else None
                    detail = reason or str(e.get("result") or "")

                hist_rows.append({
                    "when": ui.fmt_age(e.get("at")),
                    "action": kind,
                    "add": add,
                    "drop": drop,
                    "bid": bid,
                    "rung": priority,
                    "gain": gain,
                    "detail": detail,
                    "tx_id": f"...{txid[-8:]}" if len(txid) > 8 else txid,
                    "player_id": spec.get("add_id"),
                })
            df_hist = pd.DataFrame(hist_rows)
            ev_hist = st.dataframe(
                df_hist[["when", "action", "add", "drop", "bid", "rung", "gain", "detail", "tx_id"]],
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="now_lifecycle_events_table",
                column_config={
                    "when": st.column_config.TextColumn("When", width="small"),
                    "action": st.column_config.TextColumn("Action", width="small"),
                    "add": st.column_config.TextColumn("Player Target", width="medium"),
                    "drop": st.column_config.TextColumn("Player Dropped", width="medium"),
                    "bid": st.column_config.NumberColumn("Bid", format="$%d"),
                    "rung": st.column_config.TextColumn("Rung", width="small"),
                    "gain": st.column_config.NumberColumn("Gain", format="%+.2f pts"),
                    "detail": st.column_config.TextColumn("Detail / Rationale", width="large"),
                    "tx_id": st.column_config.TextColumn("Tx ID", width="small"),
                }
            )
            ui_player_card.attach_player_selection(df_hist, ev_hist, id_col="player_id", week=run.get("week"))
except Exception as e:  # a dead Sleeper must not take the page with it
    st.warning(f"Live pending queue unavailable: {type(e).__name__}")

# ------------------------------------------------------------- the plumbing
st.divider()
st.subheader("What these pages are reading")
rows = ui.artifacts()
cols = st.columns(len(rows))
for c, r in zip(cols, rows):
    with c:
        st.metric(r["label"], r["age"],
                  delta="stale" if r["stale"] else None,
                  delta_color="inverse" if r["stale"] else "off")
        st.caption(r["detail"] or "—")

c1, c2, c3 = st.columns(3)
with c1:
    st.page_link("pages/1_Lineups.py", icon="⚖️", label="**Lineups** — weekly start/sit trace")
    st.caption("Starters, bench, missing keys, and Sleeper shadow comparison.")
with c2:
    st.page_link("pages/2_Moves.py", icon="📋", label="**Moves** — waivers pipeline")
    st.caption("8-stage pipeline trace, tier quality, and LLM arbitrations.")
with c3:
    st.page_link("pages/3_Projections.py", icon="📊", label="**Projections Hub** — weekly & ROS")
    st.caption("Quantile bands, edge vs Sleeper, and rest of season board.")

p1, p2 = st.columns(2)
with p1:
    st.page_link("pages/4_AI_Scout.py", icon="🤖", label="**AI & Scout Center**")
    st.caption("Dead-heat LLM arbitrations, news prose verdicts, and queue.")
with p2:
    st.page_link("pages/5_Calibration.py", icon="🔧", label="**Calibration** — fitted baselines")
    st.caption("Role absorption, defence streaming fit, and return curves.")


st.divider()
st.caption("Settings live in the admin panel on port 8502. This app has no field that "
           "changes the bot's behaviour, including the submit gate — that is a constant "
           "in `robo/value.py`, kept out of the settings registry so turning the bot "
           "loose on the roster takes a commit.")
