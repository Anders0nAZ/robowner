"""The fitted inheritance model and the position rooms it prices."""

import json

import pandas as pd
import streamlit as st

from robo import expected, roles, ui

st.title("Roles")
ui.gate_banner(st)
st.caption("The historical absorption fit and the current position rooms saved in "
           "the authoritative rest-of-season table. Rank comes from projected "
           "opportunity; Sleeper's depth chart is used for one thing only — "
           "placing a man the season forecast omits entirely — and never to "
           "decide who holds the lead role. Those rows are marked.")


@st.cache_data(ttl=600, show_spinner=False)
def load() -> tuple[dict, dict]:
    # Do not call roles.load_fit() here: its deliberate production fallback is
    # to refit and write the artifact when the file is bad. This app is a
    # reader, so an absent/corrupt fit must render as an error, never repair it.
    try:
        fit = json.loads(roles.FIT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        fit = {"error": f"{type(exc).__name__}: {exc}"}
    return fit, expected.load()


fit, table = load()
if fit.get("error") or not fit.get("curve"):
    st.error("The fitted role artifact is unavailable: " + str(fit.get("error") or "empty fit"))
    st.stop()

metrics = st.columns(4)
metrics[0].metric("Vacancy events", fit.get("events"))
metrics[1].metric("Seasons", "–".join(map(str, fit.get("seasons") or [])))
metrics[2].metric("Established role", f"{float(fit.get('min_established_share') or 0):.0%}")
metrics[3].metric("Fit computed", ui.fmt_age(fit.get("fitted")))
st.caption(f"A role is established over a {fit.get('window')}-week window. "
           "Miss rate is vacancy events divided by weeks an established role was held; "
           "absorption is the share of the vacated lead role picked up by each rank.")

st.subheader("Absorption curve")
curve_rows = []
for pos, cells in (fit.get("curve") or {}).items():
    for rank, cell in cells.items():
        effective, source = roles.absorption(pos, int(rank))
        curve_rows.append({"pos": pos, "rank": int(rank), "events": cell.get("n"),
                           "fitted mean": cell.get("mean"), "median": cell.get("median"),
                           "sd": cell.get("sd"), "used by model": effective,
                           "source": source})
curve_rows.sort(key=lambda r: (r["pos"], r["rank"]))
position = st.radio("Position", sorted({r["pos"] for r in curve_rows}), horizontal=True)
position_rows = [r for r in curve_rows if r["pos"] == position]
left, right = st.columns([3, 2])
with left:
    st.dataframe(pd.DataFrame(position_rows), use_container_width=True, hide_index=True,
                 column_config={
                     "fitted mean": st.column_config.NumberColumn(format="%.3f"),
                     "median": st.column_config.NumberColumn(format="%.3f"),
                     "sd": st.column_config.NumberColumn(format="%.3f"),
                     "used by model": st.column_config.NumberColumn(format="%.3f"),
                 })
with right:
    st.bar_chart(pd.DataFrame(position_rows).set_index("rank")[["used by model"]],
                 height=280)
st.caption(f"{position} lead-role miss rate: "
           f"{float((fit.get('miss_rate') or {}).get(position, 0)):.2%} per week · "
           f"{(fit.get('events_by_pos') or {}).get(position, 0)} observed vacancies.")

with st.expander("Long-term role takeovers"):
    takeover_rows = []
    for pos, groups in (fit.get("takeover") or {}).items():
        for cohort, cell in groups.items():
            takeover_rows.append({"pos": pos, "cohort": cohort,
                                  "players": cell.get("n"),
                                  "takeover rate": f"{float(cell.get('rate') or 0):.1%}"})
    st.caption("A separate event: a player who began as a backup holds the lead role by week 10.")
    st.dataframe(pd.DataFrame(takeover_rows), use_container_width=True, hide_index=True)

st.divider()
st.subheader("Current team position room")
players = list((table.get("players") or {}).values())
room_players = [p for p in players if p.get("team") and p.get("pos") in roles.PROJ_OPPORTUNITY]
if not room_players:
    st.error("The current rest-of-season table contains no position-room records.")
    st.stop()

selectors = st.columns(2)
team = selectors[0].selectbox("Team", sorted({p["team"] for p in room_players}))
positions = sorted({p["pos"] for p in room_players if p["team"] == team})
room_pos = selectors[1].selectbox("Position room", positions)
room = sorted((p for p in room_players if p["team"] == team and p["pos"] == room_pos),
              key=lambda p: (p.get("rank") is None, p.get("rank") or 999, p["name"]))
st.caption(f"Saved in data/expected.json {ui.fmt_age(table.get('computed'))}; "
           f"week {table.get('week')} eligibility is already reflected in these ranks.")
st.dataframe(pd.DataFrame([{
    "rank": p.get("rank"), "player": p.get("name"),
    "share": f"{float(p.get('share') or 0):.1%}",
    # A 0.0% share next to a real `absorbs` looks like a bug unless the reader
    # is told the slot is a depth-chart placement rather than a forecast.
    # A man with no rank is not IN the room -- the season file forecasts him
    # nothing and Sleeper lists him nowhere on the chart. Rendering him as
    # "projected opportunity / holds lead role" claimed the two strongest
    # things on the row about the one player the model knows least about.
    "slot from": ("not in the room" if p.get("rank") is None else
                  f"depth chart (listed {p['from_depth_chart']})"
                  if p.get("from_depth_chart") else "projected opportunity"),
    "lead": ("—" if p.get("rank") is None
             else p.get("lead_of") or "holds lead role"),
    "absorbs": f"{float(p.get('absorbs') or 0):.1%}",
    "ROS": p.get("ros"), "eligible week": p.get("eligible_week"),
} for p in room]), use_container_width=True, hide_index=True,
    column_config={
        "ROS": st.column_config.NumberColumn(format="%.1f"),
    })
st.caption("`absorbs` is a share of the LEAD's vacated work, not of the man one "
           "rung up — from rank 3 down those are different people.")

with st.expander("Raw fitted evidence"):
    st.json(fit, expanded=False)
    st.caption(str(roles.FIT_FILE))
