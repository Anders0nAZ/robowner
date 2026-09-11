"""Roboner audit — deep-dive the modules and the decisions.

Local only. Run it with AuditGUI.bat, or:

    streamlit run audit_gui.py --server.port 8504

WHY A SECOND APP. admin_gui.py on 8502 tunes policy and its docstring is a
statement about what it will and will not let a human change. This one changes
nothing at all: it reads what the bot computed and explains how. Keeping them
apart keeps both descriptions true.

WHAT IT IS FOR. robo/value.py currently has VALUATION_READY on and
SUBMIT_ENABLED off -- the numbers are real and nothing is being sent -- pending
a module-by-module review. Before this, the only way to interrogate a number was
`python -m robo.ros --explain`. This is that review's tool.

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
cols = st.columns(len(rows))
for c, r in zip(cols, rows):
    with c:
        st.metric(r["label"], r["age"],
                  delta="stale" if r["stale"] else None,
                  delta_color="inverse" if r["stale"] else "off")
        st.caption(r["detail"] or "—")

st.divider()

left, right = st.columns(2)
with left:
    st.subheader("Here now")
    st.markdown(
        "**Rest of season** — every player's value from this week to the end, "
        "and a per-player traceback that walks the calculation forward: what "
        "each remaining week is worth and why, where each week's rate came "
        "from, how news is applied, and the full inheritance chain behind the "
        "`upside` term.\n\n"
        "The number a page shows is the one from `data/ros.json` — what the bot "
        "actually acted on — not a fresh computation that might disagree with "
        "it.")
with right:
    st.subheader("Not built yet")
    st.markdown(
        "- **Moves** — why it proposed a given add or drop, which roster "
        "players were excluded and on what grounds, and the near-misses that "
        "just failed the bar.\n"
        "- **FAAB** — the whole objective curve behind a bid: P(win) at every "
        "dollar, the rival distribution it is built on, and the paced price of "
        "a dollar.\n"
        "- **Roles** — the fitted absorption curve with its sample sizes, and "
        "any team's position room.\n\n"
        "Each needs the same `record` treatment on its own module that "
        "`robo/ros.py` now has.")

st.divider()
st.caption("Settings live in the admin panel on port 8502. This app has no "
           "field that changes the bot's behaviour, including the submit gate — "
           "that is a constant in `robo/value.py`, kept out of the settings "
           "registry so turning the bot loose on the roster takes a commit.")
