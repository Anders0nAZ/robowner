"""The slate as it stands: what Roboner would do right now, and on what roster.

IT READS THE LAST RECORDED RUN rather than computing a fresh one. The news
pulse runs every twenty minutes, so "last run" is rarely far behind, and an
audit tool that recomputes its own answer can no longer tell you what the bot
actually did -- which is the only question this app exists to answer. The
staleness is stated at the top instead of being hidden by a recomputation.
"""

import pandas as pd
import streamlit as st

from robo import decision_audit, narrate, ui

st.title("🔍 What Roboner would do now")
ui.gate_banner(st)


@st.cache_data(ttl=20, show_spinner=False)
def latest_run():
    return decision_audit.latest()


@st.cache_data(ttl=20, show_spinner="Reading the pending queue from Sleeper…")
def live_portfolio():
    """Two authenticated reads that also settle anything that has left the
    pending queue, so this runs on a timer rather than on every widget click."""
    from robo import waiver_manager
    return waiver_manager.status()


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
    st.dataframe(
        pd.DataFrame([{k: v for k, v in row.items()
                       if k not in {"raw", "add_id", "drop_id"}} for row in slate]),
        use_container_width=True, hide_index=True,
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
    for row in slate:
        if row["channel"] == "waiver claim":
            st.caption(ui.money(narrate.claim_story(row["raw"])))

st.markdown(f"[Open this decision front to back →](Decisions?run={run['fingerprint']})")

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
    events = live.get("events") or []
    if events:
        with st.expander("What has been submitted, cancelled or rolled back"):
            st.dataframe(pd.DataFrame([{
                "at": ui.fmt_age(e.get("at")), "event": e.get("kind"),
                "source": e.get("source"), "transaction": e.get("transaction_id"),
                "detail": e.get("reason") or e.get("error")
                          or (e.get("result") or {}).get("status") or "",
            } for e in events]), use_container_width=True, hide_index=True)
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

left, right = st.columns(2)
with left:
    st.page_link("pages/1_Decisions.py", icon="🧾",
                 label="**Decisions** — every run, front to back")
    st.caption("One timeline of every re-evaluation, whether the clock or the news "
               "started it. Open one to see what fired, what moved, who was considered "
               "and rejected, what the bid was worth, and whether anything could reach "
               "Sleeper.")
with right:
    st.page_link("pages/2_Players.py", icon="📈",
                 label="**Players** — what a man is worth, and why")
    st.caption("The valuation board with day-over-day and week-over-week movement, and "
               "a per-player traceback from the feeds to the printed total.")
st.page_link("pages/3_Calibration.py", icon="🔧",
             label="**Calibration** — how the machinery was fitted")

st.divider()
st.caption("Settings live in the admin panel on port 8502. This app has no field that "
           "changes the bot's behaviour, including the submit gate — that is a constant "
           "in `robo/value.py`, kept out of the settings registry so turning the bot "
           "loose on the roster takes a commit.")
