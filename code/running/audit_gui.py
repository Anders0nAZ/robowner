"""Roboner audit — follow a decision front to back.

Local only. Run it with AuditGUI.bat, or:

    streamlit run audit_gui.py --server.port 8504

WHY A SECOND APP. admin_gui.py on 8502 tunes policy and its docstring is a
statement about what it will and will not let a human change. This one changes
nothing at all: it reads what the bot computed and explains how. Keeping them
apart keeps both descriptions true.

WHY FOUR PAGES AND NOT SIX. The app used to have one page per MODULE, which
meant the sidebar listed the stages of a single pipeline as though they were
alternatives, and one waiver claim was split across two of them -- the claim
and its drop on one page, the bid that priced it on another, reading the same
record through the same dropdown. The pages now follow the question instead:
what would it do NOW, why did it DECIDE that, what is a PLAYER worth, and how
was the machinery CALIBRATED. The trigger that started a run -- the clock or
the news -- is a badge on the timeline, not a page of its own, because it does
not change what the run did.

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
st.navigation([
    st.Page("pages/0_Now.py", title="Now", icon="🔍",
            url_path="Now", default=True),
    st.Page("pages/1_Decisions.py", title="Decisions", icon="🧾",
            url_path="Decisions"),
    st.Page("pages/2_Players.py", title="Players", icon="📈",
            url_path="Players"),
    st.Page("pages/3_Calibration.py", title="Calibration", icon="🔧",
            url_path="Calibration"),
]).run()
