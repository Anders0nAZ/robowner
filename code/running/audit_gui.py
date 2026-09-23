"""Roboner audit — follow a decision front to back.

Local only. Run it with AuditGUI.bat, or:

    streamlit run audit_gui.py --server.port 8504

WHY A SECOND APP. admin_gui.py on 8502 tunes policy and its docstring is a
statement about what it will and will not let a human change. This one changes
nothing at all: it reads what the bot computed and explains how. Keeping them
apart keeps both descriptions true.

PAGES FOLLOW THE QUESTION, NOT THE MODULE. The app once had one page per
module, which listed the stages of a single pipeline as though they were
alternatives and split one waiver claim across two pages. Now: what would it
do NOW, how was this week's LINEUP chosen, why did a run DECIDE a move, what
are the PROJECTIONS, what did the model and SCOUT read, how was the machinery
CALIBRATED -- and what actually happened to the roster, TRANSACTION by
transaction, and who instructed each one. That last question is not a run's:
an IR unblock's cut or a construction repair belongs to no decision run, so a
page organised around runs cannot show it. The trigger that started a run --
the clock or the news -- is a badge on the timeline, not a page of its own,
because it does not change what the run did.

THIS FILE IS THE ROUTER. It owns the page registry so the sidebar reads in
plain words; the pages themselves live in pages/.

UNREDACTED. The local counterpart to status.report(): full paths, the complete
model anchor, a scout verdict's reason. An audit tool that hides its inputs
cannot be used to audit them, and status._scrub() exists for the page that gets
published. Nothing here writes to decision-log/ or calls decisions.publish().
"""

import streamlit as st

st.set_page_config(page_title="Roboner audit", page_icon="🔍", layout="wide")

# url_path is pinned rather than derived from the filename, because the Now
# page deep-links into a specific decision with `Decisions?run=<fingerprint>`.
# A path that moved when a file was renumbered would break that link silently.
# The DEFAULT page is the exception: Streamlit serves it at the root and there
# is no second path to it, so naming one here would only be a path that 404s.
st.navigation([
    st.Page("pages/0_Now.py", title="Now", icon="🔍", default=True),
    st.Page("pages/1_Lineups.py", title="Lineups", icon="⚖️", url_path="Lineups"),
    st.Page("pages/2_Moves.py", title="Moves & Waivers", icon="📋", url_path="Moves"),
    st.Page("pages/3_Projections.py", title="Projections Hub", icon="📊", url_path="Projections"),
    st.Page("pages/4_AI_Scout.py", title="AI & Scout", icon="🤖", url_path="AIScout"),
    st.Page("pages/5_Calibration.py", title="Calibration", icon="🔧", url_path="Calibration"),
    st.Page("pages/6_Transactions.py", title="Transactions", icon="📜", url_path="Transactions"),
]).run()

