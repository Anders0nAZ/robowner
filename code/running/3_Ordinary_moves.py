"""Frozen ordinary add/drop decisions, including exclusions and near-misses."""

from datetime import datetime

import pandas as pd
import streamlit as st

from robo import ui, waiver_audit

st.title("Ordinary moves")
ui.gate_banner(st)
st.caption("The ordered weekly pass: take the best free improvement first, then "
           "reprice waivers against that roster. Everything shown is frozen at decision time.")


@st.cache_data(ttl=10, show_spinner=False)
def load():
    return waiver_audit.events()


def player(row: dict | None) -> str:
    row = row or {}
    return str(row.get("name") or row.get("player_id") or "(open roster spot)")


def plan_rows(doc: dict) -> list[dict]:
    rows = []
    for p in doc.get("free_plans") or []:
        rows.append({"channel": "free now", "add": player(p.get("add")),
                     "drop": player(p.get("drop")), "gain": p.get("gain"),
                     "ceiling": p.get("ceiling"), "why": p.get("why")})
    for slate in doc.get("plans") or []:
        for claim in slate.get("claims") or []:
            rows.append({"channel": "waiver claim", "add": player(claim.get("add")),
                         "drop": player(claim.get("drop") or slate.get("drop")),
                         "gain": claim.get("gain"), "ceiling": claim.get("ceiling"),
                         "why": claim.get("why")})
    return rows


docs = load()
if not docs:
    st.info("No ordinary move evaluation has been recorded since this audit was enabled.")
    st.stop()

labels = {d["fingerprint"]: datetime.fromtimestamp(float(d["at"])).astimezone().strftime(
    "%b %d, %I:%M %p") + f" · week {d.get('week')}" for d in docs}
chosen = st.selectbox("Evaluation", list(labels), format_func=labels.get)
doc = next(d for d in docs if d["fingerprint"] == chosen)
free_audit = doc.get("free_audit") or {}
claims_audit = doc.get("claims_audit") or {}

proposals = plan_rows(doc)
metrics = st.columns(4)
metrics[0].metric("Week", doc.get("week"))
metrics[1].metric("Moves that cleared", len(proposals))
metrics[2].metric("Transaction gate", "closed" if doc.get("gated") else "open")
metrics[3].metric("Valuation", ui.fmt_age(doc.get("valuation_computed")))
if doc.get("sequence_basis"):
    st.caption("Waiver baseline: " + str(doc["sequence_basis"]) + ".")
if doc.get("blackout") or doc.get("control_block"):
    st.warning(doc.get("blackout") or doc.get("control_block"))

st.subheader("What cleared")
if proposals:
    st.dataframe(pd.DataFrame(proposals), use_container_width=True, hide_index=True,
                 column_config={"gain": st.column_config.NumberColumn(format="%+.2f"),
                                "ceiling": st.column_config.NumberColumn(format="%.2f")})
else:
    st.info("No add/drop comparison cleared every control and value bar in this pass.")

if not free_audit and not claims_audit:
    st.info("This is a legacy event from before candidate-level auditing was enabled. "
            "The outcome is intact, but exclusions and near-misses were not recorded.")
else:
    st.subheader("Roster drop screen")
    checks = free_audit.get("drop_checks") or claims_audit.get("drop_checks") or []
    if checks:
        st.dataframe(pd.DataFrame([{
            "player": c.get("name") or c.get("player_id"), "pos": c.get("pos"),
            "eligible": c.get("eligible"), "drop price": c.get("drop_price"),
            "reason": c.get("reason"),
        } for c in checks]), use_container_width=True, hide_index=True)
    else:
        st.caption("The run stopped before a drop pool was needed.")

    control_checks = ((free_audit.get("control_checks") or [])
                      + (claims_audit.get("control_checks") or []))
    if control_checks:
        with st.expander("Roster-construction checks"):
            st.dataframe(pd.DataFrame([{
                "add": player(c.get("add")) if c.get("add") else c.get("add_id"),
                "drop": player(c.get("drop")) if c.get("drop") else c.get("drop_id"),
                "eligible": c.get("eligible"), "reason": c.get("reason"),
                "direct ROS gain": (c.get("direct_ros") or {}).get("gain"),
            } for c in control_checks]).drop_duplicates(),
                use_container_width=True, hide_index=True)

    st.subheader("Candidate board and near-misses")
    phase = st.radio("Phase", ["free now", "waivers"], horizontal=True)
    audit = free_audit if phase == "free now" else claims_audit
    thresholds = audit.get("thresholds") or {}
    if thresholds:
        st.caption("Recorded bars: gain must beat "
                   f"{thresholds.get('noise_multiple')}× simulation error; starting gains "
                   f"must reach {thresholds.get('starting_gain')}; bench ceilings must reach "
                   f"{thresholds.get('bench_ceiling')}; drop price is capped at "
                   f"{thresholds.get('drop_floor')}.")
    options = audit.get("options") or []
    if options:
        st.dataframe(pd.DataFrame([{
            "add": player(o.get("add")), "drop": player(o.get("drop")),
            "gain": o.get("gain"), "error": o.get("se"),
            "ceiling": o.get("ceiling"), "starter": o.get("starter"),
            "noise margin": o.get("noise_margin"),
            "policy margin": o.get("policy_margin"), "verdict": o.get("verdict"),
        } for o in options]), use_container_width=True, hide_index=True,
            column_config={
                "gain": st.column_config.NumberColumn(format="%+.2f"),
                "error": st.column_config.NumberColumn(format="%.2f"),
                "ceiling": st.column_config.NumberColumn(format="%.2f"),
                "noise margin": st.column_config.NumberColumn(format="%+.2f"),
                "policy margin": st.column_config.NumberColumn(format="%+.2f"),
            })
    else:
        st.caption("No candidates reached the simulator in this phase.")

with st.expander("Raw immutable evidence"):
    st.json({k: v for k, v in doc.items() if not k.startswith("_")}, expanded=False)
    st.caption(doc.get("_path"))
