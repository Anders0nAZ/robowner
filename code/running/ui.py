"""Shared furniture for the local audit app. No logic lives here.

WHAT THIS IS FOR. audit_gui.py and its pages are readers: they render what the
bot computed and never compute anything themselves. Everything a page needs
twice -- the gate banner, artifact freshness, position colours, a trace block --
lives here so the second page is a page file rather than a project.

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
from datetime import datetime

# Position colours, matched to the Roboner NFL model viewer
# family when they are open side by side.
POS_COLOR = {"QB": "#e45756", "RB": "#4c78a8", "WR": "#54a24b",
             "TE": "#f58518", "K": "#b279a2", "DEF": "#79706e"}

# The source tags ros.py stamps on each week, and what each one means to a
# reader who has not read the module.
SOURCE_HELP = {
    "model": "Roboner's NFL model week -- all 57 scoring keys off 4,000 "
             "stat lines. Only ever the current week.",
    "sleeper": "Sleeper's weekly projection, scored under this league, plus an "
               "estimate of the 22 scoring keys its feed omits.",
    "vegas": "priced off the opponent's implied point total from the betting "
             "market. Defences only.",
    "fallback": "no posted line for that week, so his own season rate over the "
                "games left. Knows nothing about the matchup.",
}


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


def fmt_eta(ts) -> str:
    """'in 40m (09:20)' / 'any moment', from an epoch. The mirror of fmt_age.

    Carries the clock time as well as the wait, because a wait read on a phone
    an hour later is wrong and a clock time is not.
    """
    if not ts:
        return "unknown"
    at = datetime.fromtimestamp(float(ts))
    s = float(ts) - time.time()
    if s <= 60:
        return f"any moment ({at:%H:%M})"
    if s < 5400:
        return f"in {int(s / 60)}m ({at:%H:%M})"
    return f"in {s / 3600:.1f}h ({at:%H:%M})"


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


def artifacts(steps=("expected", "ros", "playoff-odds", "model", "board")) -> list:
    """Freshness for the files a page reads, from the status page's collector.

    Reuses status._source_marker rather than re-reading each file: the budgets
    there are pinned to the code constants they belong to (ros.MAX_AGE_H and
    model_proj.MAX_AGE_H), and a second freshness implementation would be free
    to disagree with the page the league actually sees.
    """
    from robo import status
    out = []
    labels = {s: lbl for s, lbl, _, _ in status.SOURCES}
    budgets = {s: b for s, _, b, _ in status.SOURCES}
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


def money(text: str) -> str:
    """Escape bare dollar signs so Streamlit stops eating them as LaTeX.

    Streamlit's markdown treats `$...$` as a maths span, so any sentence with
    two dollar amounts in it gets the text between them italicised and the
    dollars deleted. The FAAB quote narrative is exactly that shape --
    "P(win) 25% at $1 via opponent model; a dollar is priced at 0.38 lineup
    pts; reservation $12" rendered as "at 1*viaopponentmodel*; ... reservation
    12", which is unreadable and, worse, silently changes the numbers a reader
    is trying to check.
    """
    return (text or "").replace("$", r"\$")


def trace_block(st, text: str) -> None:
    """A monospace trace, wide enough that the source tags stay in their column."""
    st.code(text or "(nothing to trace)", language="text")


def pos_filter(st, rows, key="pos") -> list:
    """The position multiselect every board page wants."""
    opts = sorted({r["pos"] for r in rows if r.get("pos")})
    picked = st.multiselect("Position", opts, default=[], key=f"{key}::pos",
                            help="Empty shows every position.")
    return [r for r in rows if not picked or r["pos"] in picked]


def status_chip(status_text: str, kind: str = "neutral") -> str:
    """Returns an HTML pill badge with high contrast for light and dark modes."""
    kinds = {
        "green": ("rgba(16, 185, 129, 0.18)", "#10b981", "#059669"),
        "emerald": ("rgba(16, 185, 129, 0.18)", "#10b981", "#059669"),
        "success": ("rgba(16, 185, 129, 0.18)", "#10b981", "#059669"),
        "blue": ("rgba(14, 165, 233, 0.18)", "#0284c7", "#0284c7"),
        "cyan": ("rgba(6, 182, 212, 0.18)", "#0891b2", "#0891b2"),
        "info": ("rgba(14, 165, 233, 0.18)", "#0284c7", "#0284c7"),
        "amber": ("rgba(245, 158, 11, 0.18)", "#d97706", "#d97706"),
        "yellow": ("rgba(245, 158, 11, 0.18)", "#d97706", "#d97706"),
        "warning": ("rgba(245, 158, 11, 0.18)", "#d97706", "#d97706"),
        "red": ("rgba(239, 68, 68, 0.18)", "#dc2626", "#dc2626"),
        "danger": ("rgba(239, 68, 68, 0.18)", "#dc2626", "#dc2626"),
        "neutral": ("rgba(156, 163, 175, 0.18)", "#6b7280", "#4b5563"),
        "gray": ("rgba(156, 163, 175, 0.18)", "#6b7280", "#4b5563"),
    }
    bg, border, fg = kinds.get(kind.lower(), kinds["neutral"])
    return (
        f"<span style='display: inline-flex; align-items: center; padding: 2px 10px; "
        f"border-radius: 9999px; font-size: 0.8rem; font-weight: 600; line-height: 1.4; "
        f"background-color: {bg}; border: 1px solid {border}; color: {fg}; white-space: nowrap;'>"
        f"{status_text}</span>"
    )


def metric_card(label: str, value: str | int | float, delta: str | None = None,
                help_text: str | None = None, color: str = "#29B5E8") -> str:
    """Formatted HTML metric card with secondary context."""
    delta_html = f"<div style='font-size: 0.8rem; opacity: 0.85; margin-top: 2px;'>{delta}</div>" if delta else ""
    help_attr = f" title='{help_text}'" if help_text else ""
    return (
        f"<div{help_attr} style='padding: 10px 14px; border-radius: 8px; background: rgba(128,128,128,0.08); "
        f"border-left: 4px solid {color}; margin-bottom: 8px; min-height: 72px;'>"
        f"<div style='font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.75;'>{label}</div>"
        f"<div style='font-size: 1.35rem; font-weight: 700; line-height: 1.2;'>{value}</div>"
        f"{delta_html}"
        f"</div>"
    )


def split_player_name(full_name: str, player_record: dict | None = None) -> tuple[str, str]:
    """Split a full player name into (first_name, last_name).

    Defers to Sleeper player metadata when present, otherwise uses a suffix-aware parser.
    """
    if player_record and player_record.get("last_name"):
        return str(player_record.get("first_name") or ""), str(player_record.get("last_name") or "")
    if not full_name:
        return "", ""
    parts = full_name.strip().split()
    if len(parts) <= 1:
        return full_name.strip(), full_name.strip()
    suffixes = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "v"}
    if len(parts) > 2 and parts[-1].lower() in suffixes:
        return " ".join(parts[:-2]), f"{parts[-2]} {parts[-1]}"
    return " ".join(parts[:-1]), parts[-1]

