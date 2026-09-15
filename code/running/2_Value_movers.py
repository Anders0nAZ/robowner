"""Largest time-adjusted changes in expected rest-of-season value."""

import pandas as pd
import streamlit as st

from robo import expected, ui, value_history

st.title("Value movers")
ui.gate_banner(st)
st.caption("Changes compare only weeks that remained in both snapshots and use "
           "today's playoff weights. A completed game therefore disappears from "
           "both sides instead of making every player look like a faller.")


@st.cache_data(ttl=600, show_spinner="Reading value history…")
def data() -> tuple[dict, dict]:
    current = expected.load()
    return current, {1: value_history.baseline(current, 1),
                     7: value_history.baseline(current, 7)}


@st.cache_data(ttl=600, show_spinner="Reading rosters from Sleeper…")
def ownership() -> dict:
    try:
        from robo import season
        mine = set(season.mine().get("players") or [])
        held = season.rostered_ids()
    except Exception:
        return {}
    return {pid: ("mine" if pid in mine else "rostered") for pid in held}


current, bases = data()
period = st.radio("Comparison", ["Day over day", "Week over week"], horizontal=True)
days = 1 if period == "Day over day" else 7
prior = bases[days]

if not prior:
    st.info(f"No snapshot at least {days} day{'s' if days > 1 else ''} old yet. "
            "History is now being saved automatically; this page will populate "
            "after that comparison window exists.")
    st.stop()

changes = value_history.compare(current, prior)
own = ownership()
players = current.get("players") or {}
records = []
for pid, change in changes.items():
    row = players.get(pid) or {}
    records.append({
        "player_id": pid,
        "player": row.get("name") or pid,
        "pos": row.get("pos"),
        "team": row.get("team") or "-",
        "owner": own.get(pid, "free"),
        "current ROS": row.get("ros"),
        "prior adjusted": change["prior_adjusted"],
        "change": change["delta"],
        "change %": change["pct"],
        "weeks compared": len(change["weeks"]),
    })

st.caption(f"Baseline: {value_history.age_label(current, prior)} · "
           f"week {prior.get('week')} snapshot · {len(records)} comparable players")

f1, f2, f3 = st.columns([2, 2, 2])
with f1:
    scope = st.radio("Roster", ["everyone", "mine", "rostered", "free agents"],
                     horizontal=True)
with f2:
    positions = st.multiselect("Position", sorted({r["pos"] for r in records
                                                    if r["pos"]}))
with f3:
    minimum = st.number_input("Minimum current ROS", min_value=0.0, value=1.0,
                              step=5.0,
                              help="Suppresses zero-value depth players whose "
                                   "percentage changes are not decision-useful.")

view = records
if scope == "mine":
    view = [r for r in view if r["owner"] == "mine"]
elif scope == "rostered":
    view = [r for r in view if r["owner"] in {"mine", "rostered"}]
elif scope == "free agents":
    view = [r for r in view if r["owner"] == "free"]
if positions:
    view = [r for r in view if r["pos"] in positions]
view = [r for r in view if float(r["current ROS"] or 0) >= minimum]

if not view:
    st.warning("No comparable players match those filters.")
    st.stop()

risers = sorted(view, key=lambda r: -r["change"])
fallers = sorted(view, key=lambda r: r["change"])
biggest = sorted(view, key=lambda r: -abs(r["change"]))

metric = st.columns(3)
metric[0].metric("Biggest rise", risers[0]["player"], f"{risers[0]['change']:+.1f}")
metric[1].metric("Biggest fall", fallers[0]["player"], f"{fallers[0]['change']:+.1f}")
metric[2].metric("Players moved", sum(abs(r["change"]) >= 0.5 for r in view),
                 help="At least half a point after horizon adjustment.")

chart = pd.DataFrame(biggest[:12]).set_index("player")[["change"]]
st.bar_chart(chart, horizontal=True, height=380)

config = {
    "current ROS": st.column_config.NumberColumn(format="%.1f"),
    "prior adjusted": st.column_config.NumberColumn(
        format="%.1f",
        help="The old weekly outlook re-summed over today's remaining horizon "
             "with today's playoff weights."),
    "change": st.column_config.NumberColumn(format="%+.1f"),
    "change %": st.column_config.NumberColumn(format="%+.1f%%"),
}
tab1, tab2, tab3 = st.tabs(["Risers", "Fallers", "All movers"])
columns = ["player", "pos", "team", "owner", "current ROS",
           "prior adjusted", "change", "change %", "weeks compared"]
with tab1:
    st.dataframe(pd.DataFrame(risers[:25])[columns], use_container_width=True,
                 hide_index=True, column_config=config)
with tab2:
    st.dataframe(pd.DataFrame(fallers[:25])[columns], use_container_width=True,
                 hide_index=True, column_config=config)
with tab3:
    st.dataframe(pd.DataFrame(biggest)[columns], use_container_width=True,
                 hide_index=True, height=520, column_config=config)
