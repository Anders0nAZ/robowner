"""Shared furniture for the local audit app. No logic lives here.

WHAT THIS IS FOR. audit_gui.py and its pages are read-only: they never change a
setting or send a transaction. The player page renders the exact cached
valuation; the roster page renders the marginal simulation persisted by the
production decision run. Everything a page needs twice -- the gate banner,
artifact freshness, position colours, a trace block -- lives here so each page
file stays about the thing it audits.

WHY A READER AND NOT A CONTROL PANEL. admin_gui.py tunes policy and says so;
this app explains decisions. The split matters most for the one thing it must
never offer: robo/value.py's submit gate is a constant in code, deliberately
outside the settings registry, so that turning the bot loose on the roster takes
a commit. Nothing here may present a widget that changes it. Showing its state
is the whole point; changing it is somebody else's job, on purpose.

UNREDACTED, AND NOT PUBLISHED. This is the local counterpart to
status.report() -- full paths, the complete model anchor, a scout verdict's
reason text. An audit tool that hides its inputs cannot be used to audit them,
and status._scrub() exists for the page that IS published. Nothing in this app
writes to decision-log/ or calls decisions.publish().
"""

import time

# Position colours, matched to the NFL Model viewer so the two apps read as one
# family when they are open side by side.
POS_COLOR = {"QB": "#e45756", "RB": "#4c78a8", "WR": "#54a24b",
             "TE": "#f58518", "K": "#b279a2", "DEF": "#79706e"}


def fmt_age(ts) -> str:
    """'3h ago' / 'never', from an epoch."""
    if not ts:
        return "never"
    s = max(0.0, time.time() - float(ts))
    if s < 90:
        return f"{int(s)}s ago"
    if s < 5400:
        return f"{int(s / 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago"
    return f"{int(s / 86400)}d ago"


def gate_banner(st) -> None:
    """State the submit gate at the top of every page, in value.py's own words.

    Reused verbatim rather than paraphrased: GATE_MESSAGE is the sanctioned
    wording for "this is a dry run", and a second phrasing of it is a second
    thing that can drift out of agreement with the code.
    """
    from robo import value
    if value.may_submit():
        st.error("**Submitting is LIVE.** Adds, drops and waiver claims will be "
                 "sent to Sleeper. `robo/value.py: SUBMIT_ENABLED = True`.")
    elif value.ready():
        st.info(f"**Read-only.** {value.GATE_MESSAGE}")
    else:
        st.warning("**No real valuation.** `VALUATION_READY` is off, so every "
                   "number here is the provisional preseason board.")


def artifacts(steps=("marginal", "expected", "ros", "playoff-odds", "model", "injuries",
                     "scout", "usage")) -> list:
    """Freshness for the files a page reads, from the status page's collector.

    Reuses status._source_marker rather than re-reading each file: the budgets
    there are pinned to the code constants they belong to, and a second
    freshness implementation would be free to disagree with the page the
    league actually sees. `expected` is the current skill-player engine;
    `ros` remains because value.py intentionally uses it for K and DEF.
    """
    from robo import status
    out = []
    labels = {s: lbl for s, lbl, _, _ in status.SOURCES}
    budgets = {s: b for s, _, b, _ in status.SOURCES}
    labels["marginal"] = "Latest decision simulation"
    # The full pass is scheduled weekly. Exact age is always shown; this only
    # marks it stale after it has missed the next weekly decision window.
    budgets["marginal"] = 8 * 86400
    for step in steps:
        try:
            ts, detail = status._source_marker(step)
        except Exception as e:
            ts, detail = None, f"unreadable ({type(e).__name__})"
        out.append({"step": step, "label": labels.get(step, step),
                    "ts": ts, "age": fmt_age(ts), "detail": detail,
                    "stale": bool(ts and budgets.get(step)
                                  and time.time() - ts > budgets[step])})
    return out


def trace_block(st, text: str) -> None:
    """A monospace trace, wide enough that the source tags stay in their column."""
    st.code(text or "(nothing to trace)", language="text")


def pos_filter(st, rows, key="pos") -> list:
    """The position multiselect every board page wants."""
    opts = sorted({r["pos"] for r in rows if r.get("pos")})
    picked = st.multiselect("Position", opts, default=[], key=f"{key}::pos",
                            help="Empty shows every position.")
    return [r for r in rows if not picked or r["pos"] in picked]
