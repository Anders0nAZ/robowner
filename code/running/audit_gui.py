"""Roboner audit — deep-dive the modules and the decisions.

Local only. Run it with AuditGUI.bat, or:

    streamlit run audit_gui.py --server.port 8504

WHY A SECOND APP. admin_gui.py on 8502 tunes policy and its docstring is a
statement about what it will and will not let a human change. This one changes
nothing at all: it reads what the bot computed and explains how. Keeping them
apart keeps both descriptions true.

WHAT IT IS FOR. The rebuilt path has two distinct values: expected.py's
calibrated value for an individual player, then marginal.py's change to our
optimal lineup across simulated seasons. The pages expose both layers and the
move policy that consumes the second one. value.py's gate state is shown from
the code itself rather than described here, so this page cannot drift when the
gate changes.

UNREDACTED. The local counterpart to status.report(): full paths, the complete
model anchor, a scout verdict's reason. An audit tool that hides its inputs
cannot be used to audit them, and status._scrub() exists for the page that gets
published. Nothing here writes to decision-log/ or calls decisions.publish().
"""

import streamlit as st

from robo import ui

st.set_page_config(page_title="Roboner audit", page_icon="🔍", layout="wide")

st.title("🔍 Roboner audit")
st.caption("What the bot computed, and how. Read-only — nothing on these pages "
           "changes a setting or sends anything to Sleeper.")

ui.gate_banner(st)

st.subheader("The artifacts these pages read")
rows = ui.artifacts()
for start in range(0, len(rows), 4):
    cols = st.columns(min(4, len(rows) - start))
    for c, r in zip(cols, rows[start:start + 4]):
        with c:
            st.metric(r["label"], r["age"],
                      delta="stale" if r["stale"] else None,
                      delta_color="inverse" if r["stale"] else "off")
            st.caption(r["detail"] or "—")

st.divider()

left, right = st.columns(2)
with left:
    st.subheader("Player value")
    st.markdown(
        "**Rest of season** — every player's value from this week to the end, "
        "with availability, current role, inherited role, market calibration, "
        "and playoff weighting visible week by week. The page reads the exact "
        "snapshot in `data/expected.json`; it never rebuilds it while you are "
        "auditing it. Kickers and defences remain in `data/ros.json` by design.")
with right:
    st.subheader("Roster decisions")
    st.markdown(
        "**Roster value & moves** — what each player costs to drop in the "
        "contingent worlds where depth matters, how replaceable each position "
        "is from the wire, and every free-agent and waiver option admitted to "
        "the latest decision. `moves.py` saves the exact sampled outcomes and "
        "policy verdicts it used; this page reads that snapshot and never "
        "launches a simulation of its own.")

st.divider()
st.caption("Settings live in the admin panel on port 8502. This app has no "
           "field that changes the bot's behaviour, including the submit gate — "
           "that is a constant in `robo/value.py`, kept out of the settings "
           "registry so turning the bot loose on the roster takes a commit.")
