"""Read the exact marginal simulation saved by the latest decision run.

The expensive work belongs to robo.moves. When that engine prices a ROS, fill,
or waiver pass it writes data/marginal_decision.json from the Board and options
already in memory. This page only reads that file. It cannot create a newer
world, refresh an input, plan a move, or send a transaction.
"""

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from robo import DATA, expected, ui

SNAPSHOT = DATA / "marginal_decision.json"
SCHEMA = 1

st.title("Roster value & moves")
ui.gate_banner(st)
st.caption(
    "The latest simulation the move engine actually used. Every value, policy "
    "verdict, and sampled outcome below was persisted by that decision run; "
    "opening this page does no computation and changes no project file.")


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _stamp(ts) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _option_rows(options: list[dict]) -> list[dict]:
    return [{
        "selected": o.get("selected", False),
        "passes": o.get("clears_policy", False),
        "channel": o.get("channel"),
        "add": (o.get("add") or {}).get("player"),
        "pos": (o.get("add") or {}).get("pos"),
        "drop": (o.get("drop") or {}).get("player"),
        "mean gain": o.get("mean_gain"),
        "+/-": o.get("se"),
        "p90 ceiling": o.get("p90_ceiling"),
        "P(matters)": o.get("p_matters"),
        "starts": o.get("starts"),
        "first start": o.get("first_start"),
        "weighted starts": o.get("start_weeks"),
        "over best free": o.get("over_best_free"),
        "best free": o.get("best_free"),
        "bid": o.get("bid"),
        "sequence": o.get("sequence"),
        "verdict": o.get("why"),
    } for o in options]


def _histogram(values: list[float], bins: int = 20) -> pd.DataFrame:
    if not values:
        return pd.DataFrame(columns=["change", "seasons"])
    lo, hi = min(values), max(values)
    if lo == hi:
        return pd.DataFrame({"change": [f"{lo:+.1f}"], "seasons": [len(values)]})
    width = (hi - lo) / bins
    counts = [0] * bins
    for value in values:
        counts[min(bins - 1, int((value - lo) / width))] += 1
    return pd.DataFrame({
        "change": [f"{lo + i * width:+.1f} to {lo + (i + 1) * width:+.1f}"
                   for i in range(bins)],
        "seasons": counts,
    })


if not SNAPSHOT.exists():
    st.error("No saved marginal decision exists yet. The next ROS, fill, or "
             "waiver run will write one; this audit page will never run it itself.")
    st.stop()

try:
    snap = _read(SNAPSHOT)
except Exception as exc:
    st.error(f"Could not read the saved decision: {type(exc).__name__}: {exc}")
    st.stop()

if snap.get("schema") != SCHEMA:
    st.warning(f"This snapshot is schema {snap.get('schema', '?')}; this page "
               f"expects schema {SCHEMA} and is displaying what it can.")

inputs = snap.get("inputs") or {}
sim = snap.get("simulation") or {}
result = snap.get("result") or {}
options = snap.get("options") or []

head = st.columns(6)
head[0].metric("Decision run", ui.fmt_age(snap.get("computed")))
head[1].metric("Pass", f"{snap.get('mode', '?')} / {snap.get('channel', '?')}")
head[2].metric("Week", snap.get("week", "?"))
head[3].metric("Simulated seasons", sim.get("sims", "?"))
head[4].metric("P(playoffs)", f"{inputs.get('p_playoffs', 0):.0%}")
head[5].metric("Objective", sim.get("objective", "?"))
st.caption(f"{snap.get('origin', 'decision run').title()} · saved "
           f"{_stamp(snap.get('computed'))} · common random seed "
           f"{sim.get('seed', '?')} · expected.json generation "
           f"{_stamp(inputs.get('expected_computed'))}")

try:
    current_expected = _read(expected.CACHE).get("computed")
except Exception:
    current_expected = None
if current_expected and inputs.get("expected_computed") != current_expected:
    st.warning("expected.json has changed since this decision. The page is still "
               "showing the values the saved run used, which is the auditable record.")

if result.get("blackout"):
    st.warning(f"Policy stopped this pass: {result['blackout']}")
elif not result.get("gate_open"):
    st.info("The submit gate was closed. These are the proposals the engine "
            "would have used; none could be sent.")
elif result.get("applied"):
    st.success(f"Applied: {len(result.get('submitted') or [])} transaction(s) reached Sleeper.")
else:
    st.info("The engine completed this pass without applying a transaction.")

decision_tab, board_tab, roster_tab, wire_tab, input_tab = st.tabs([
    "Decision", "Candidate board", "Roster", "Wire floor", "Inputs & policy"])

with decision_tab:
    # A PASS ONLY ACTS ON ITS OWN CHANNEL. moves.py prices free agents and the
    # wire against the same Board in one go, so the saved options span both --
    # but a `free` run can never select a claim, and reporting "nothing cleared"
    # because the two that did were waiver claims would be a false negative on
    # the exact question this page exists to answer.
    channel = snap.get("channel")
    picked = [o for o in options if o.get("selected")]
    ours = [o for o in options if o.get("channel") == channel]
    passing = [o for o in ours if o.get("clears_policy")]
    elsewhere = [o for o in options
                 if o.get("channel") != channel and o.get("clears_policy")]
    dcols = st.columns(4)
    dcols[0].metric("Selected options", len(picked))
    dcols[1].metric(f"Passing {channel} options", len(passing))
    dcols[2].metric("Options simulated", len(options))
    dcols[3].metric("Baseline skill points", f"{sim.get('baseline_score', 0):.1f}")
    if picked:
        st.dataframe(pd.DataFrame(_option_rows(picked)), use_container_width=True,
                     hide_index=True, column_config={"selected": None, "passes": None})
    elif passing:
        st.info(f"{len(passing)} {channel} option(s) cleared policy, but the "
                "planner selected none of them — the candidate board shows the "
                "gate each one met and what it was ranked behind.")
    else:
        st.info(f"No {channel} option survived every gate in this pass.")
    if elsewhere:
        other = sorted({o.get("channel") for o in elsewhere})
        st.caption(f"{len(elsewhere)} option(s) on the "
                   f"{', '.join(other)} channel cleared their own gates on this "
                   f"same simulated board. A {channel} pass never acts on them; "
                   "they are on the candidate board, and the pass that owns that "
                   "channel is what would claim them.")
    failed = result.get("failed") or []
    if failed:
        st.error(f"{len(failed)} attempted transaction(s) failed.")
        st.dataframe(pd.DataFrame(failed), use_container_width=True, hide_index=True)

with board_tab:
    c1, c2, c3 = st.columns([2, 2, 3])
    channels = sorted({o.get("channel") for o in options if o.get("channel")})
    with c1:
        chosen_channels = st.multiselect("Channel", channels, default=channels)
    with c2:
        view = st.selectbox("Verdict", ["All", "Passes policy", "Selected"])
    with c3:
        query = st.text_input("Player search", placeholder="Add or drop name")
    shown = [o for o in options if o.get("channel") in chosen_channels]
    if view == "Passes policy":
        shown = [o for o in shown if o.get("clears_policy")]
    elif view == "Selected":
        shown = [o for o in shown if o.get("selected")]
    if query:
        q = query.casefold()
        shown = [o for o in shown if q in ((o.get("add") or {}).get("player") or "").casefold()
                 or q in ((o.get("drop") or {}).get("player") or "").casefold()]
    st.dataframe(
        pd.DataFrame(_option_rows(shown)), use_container_width=True, hide_index=True,
        height=520, column_config={
            "selected": st.column_config.CheckboxColumn(),
            "passes": st.column_config.CheckboxColumn(),
            "mean gain": st.column_config.NumberColumn(format="%+.2f"),
            "+/-": st.column_config.NumberColumn(format="%.2f"),
            "p90 ceiling": st.column_config.NumberColumn(format="%+.2f"),
            "P(matters)": st.column_config.ProgressColumn(
                format="percent", min_value=0.0, max_value=1.0),
            "over best free": st.column_config.NumberColumn(format="%+.2f"),
            "bid": st.column_config.NumberColumn(format="$%d"),
        })

    if shown:
        labels = [f"{o['channel']} · {(o.get('add') or {}).get('player')} for "
                  f"{(o.get('drop') or {}).get('player')} · {o.get('mean_gain', 0):+.2f}"
                  for o in shown]
        choice = st.selectbox("Inspect one simulated option", range(len(shown)),
                              format_func=lambda i: labels[i])
        o = shown[choice]
        st.write(o.get("why") or "No policy explanation was saved.")
        metrics = st.columns(5)
        metrics[0].metric("Mean gain", f"{o.get('mean_gain', 0):+.2f}")
        metrics[1].metric("Standard error", f"{o.get('se', 0):.2f}")
        metrics[2].metric("P90", f"{o.get('p90_ceiling', 0):+.2f}")
        metrics[3].metric("P(matters)", f"{o.get('p_matters', 0):.0%}")
        metrics[4].metric("First median start", o.get("first_start") or "never")
        st.bar_chart(_histogram(o.get("outcomes") or []), x="change", y="seasons")
        st.caption("Paired change in optimal skill-lineup points in every common "
                   "random season used by the decision. The same vacancy, "
                   "availability, and role-share world is used before and after.")

with roster_tab:
    st.dataframe(pd.DataFrame(snap.get("roster") or []), use_container_width=True,
                 hide_index=True, column_config={
                     "player_id": None,
                     "standalone_ros": st.column_config.NumberColumn(
                         "standalone ROS", format="%.1f"),
                     "cost_to_drop": st.column_config.NumberColumn(
                         "cost to drop", format="%.2f"),
                     "eligible": st.column_config.CheckboxColumn(),
                 })
    st.caption("Protected starters and reserve players are excluded before drop "
               "pricing, so the saved run correctly shows no invented drop cost for them.")

with wire_tab:
    st.subheader("Positional replaceability")
    st.dataframe(pd.DataFrame(snap.get("replacement") or []),
                 use_container_width=True, hide_index=True,
                 column_config={
                     "our_starters": st.column_config.NumberColumn(format="%.2f"),
                     "wire_first": st.column_config.NumberColumn(format="%.2f"),
                     "wire_second": st.column_config.NumberColumn(format="%.2f"),
                     "wire_ratio": st.column_config.ProgressColumn(
                         format="percent", min_value=0.0, max_value=1.0),
                 })
    floors = pd.DataFrame(snap.get("replacement_by_week") or [])
    if not floors.empty:
        st.subheader("Exact weekly replacement floor")
        st.dataframe(floors.pivot(index="week", columns="pos", values="points"),
                     use_container_width=True)
    st.caption("Candidates under review are removed before this floor is built, "
               "so a player is never measured against himself as the fallback.")

with input_tab:
    base = sim.get("baseline") or {}
    shape = st.columns(6)
    for col, key in zip(shape, ("mean", "p10", "p50", "p90", "min", "max")):
        col.metric(key.upper(), f"{base.get(key, 0):.1f}")
    st.bar_chart(_histogram(base.get("outcomes") or []), x="change", y="seasons")
    st.caption("Distribution of the unchanged roster's optimal skill-lineup total.")

    st.subheader("Weeks and playoff weights")
    st.dataframe(pd.DataFrame([{"week": int(w), "weight": weight}
                               for w, weight in (sim.get("weights") or {}).items()]),
                 use_container_width=True, hide_index=True)
    st.subheader("Policy constants used")
    st.dataframe(pd.DataFrame([{"setting": k, "value": v}
                               for k, v in (snap.get("policy") or {}).items()]),
                 use_container_width=True, hide_index=True)
    st.subheader("Input identity")
    st.json({**inputs,
             "expected_computed_local": _stamp(inputs.get("expected_computed")),
             "playoff_computed_local": _stamp(inputs.get("playoff_computed"))})
    with st.expander("Shortlists admitted to simulation"):
        for channel, rows in (snap.get("shortlists") or {}).items():
            st.markdown(f"**{channel.title()}**")
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
