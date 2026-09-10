"""Rest-of-season value, and how each number was arrived at.

Reads data/expected.json on the hot path -- the CALIBRATED model, which is what
the bot now acts on. It used to read ros.json, and that was the wrong surface to
review from: this page is what you look at to decide whether to trust the new
valuation, and it was showing the superseded one. Carson Beck rendered at 0.4
here while the model that prices the drop put him at 37.5, and `hold` equalled
`mean` for 583 players because ros.upside runs on a draft-capital cold start.
Reviewing the old numbers to sign off on the new ones is the same inversion
value.py warns about for the gate itself.

Kickers and team defences are absent, because expected.py models neither -- they
refill from the wire every week and are priced by robo/streaming.py instead.

The page reads the file directly rather than through expected.load(). That
loader may rebuild and overwrite a stale artifact; an audit must keep one
snapshot under the table and its trace, and this app promises not to change it.
"""

import json

import pandas as pd
import streamlit as st

from robo import expected, ros, ui

st.title("Rest of season")
ui.gate_banner(st)


@st.cache_data(ttl=600, show_spinner="Reading the rest-of-season table…")
def board() -> tuple[list, dict, dict]:
    d = json.loads(expected.CACHE.read_text(encoding="utf-8"))
    rows = list((d.get("players") or {}).values())
    meta = {k: v for k, v in d.items() if k != "players"}
    return rows, meta, d


@st.cache_data(ttl=600, show_spinner="Reading rosters from Sleeper…")
def ownership() -> dict:
    """player_id -> 'mine' | 'rostered' | 'free'. Never fatal.

    A dead Sleeper must not take the page down -- the valuation is on disk and
    is the thing being audited; who owns whom is a filter.
    """
    try:
        from robo import season
        mine = set(season.mine().get("players") or [])
        held = season.rostered_ids()
    except Exception:
        return {}
    return {pid: ("mine" if pid in mine else "rostered") for pid in held}


@st.cache_data(ttl=600, show_spinner="Walking the calculation…")
def trace_for(pid: str, table: dict) -> str:
    # By ID, never by name. "Josh Allen" is a quarterback and a linebacker, and
    # re-resolving the name here picked the linebacker while the table above
    # showed the quarterback.
    return expected.trace(player_id=pid, table=table)


try:
    rows, meta, snapshot = board()
except Exception as e:
    st.error(f"data/expected.json is unreadable: {type(e).__name__}: {e}")
    st.stop()
if not rows:
    st.error("data/expected.json is empty. Rebuild with "
             "`python -m robo.expected --rebuild`.")
    st.stop()
if meta.get("schema") != expected.SCHEMA:
    st.warning(f"This is schema {meta.get('schema', '?')}; the running engine "
               f"expects schema {expected.SCHEMA}. The page is showing the "
               "snapshot as-is and will not rebuild it.")

own = ownership()
wk = meta.get("week")

c = st.columns(4)
c[0].metric("Week", wk)
c[1].metric("Players", len(rows))
c[2].metric("Computed", ui.fmt_age(meta.get("computed")))
c[3].metric("Playoff weeks weighted",
            "·".join(f"{ros.weight_of(meta, w):.2f}" for w in (15, 16, 17)))
st.caption(
    f"Built for week {wk} onward from Sleeper's weekly feed, each week's role read "
    f"from robo/roles.py and each week's availability from the ESPN floor, then "
    f"calibrated to the season projection. Kickers and defences are absent by "
    f"design: they refill from the wire weekly and robo/streaming.py prices them.")

# ------------------------------------------------------------------- the board

st.subheader("The board")
f1, f2, f3 = st.columns([2, 2, 3])
with f1:
    scope = st.radio("Roster", ["everyone", "mine", "rostered", "free agents"],
                     horizontal=True,
                     help="Who holds him right now, read live from Sleeper.")
with f2:
    sort_by = st.selectbox("Sort by", ["ros", "k", "raw", "target"],
                           help="`ros` is the calibrated number. "
                                "They differ by what a man stands to inherit.")
with f3:
    q = st.text_input("Search", "", placeholder="name, or part of one")

view = rows
if scope == "mine":
    view = [r for r in view if own.get(r["player_id"]) == "mine"]
elif scope == "rostered":
    view = [r for r in view if r["player_id"] in own]
elif scope == "free agents":
    view = [r for r in view if r["player_id"] not in own]
view = ui.pos_filter(st, view)
if q.strip():
    view = [r for r in view if q.strip().lower() in r["name"].lower()]
view = sorted(view, key=lambda r: -r.get(sort_by, 0))

df = pd.DataFrame([{
    "player": r["name"], "pos": r["pos"], "team": r["team"] or "-",
    "owner": own.get(r["player_id"], "free"),
    "ros": r["ros"], "k": r.get("k"), "raw": r["raw"], "target": r["target"],
    "rank": r.get("rank"), "share": r.get("share"), "weeks": r["weeks"],
    "why k": r.get("k_why", ""),
} for r in view])

st.caption(f"{len(df)} of {len(rows)} players")
st.dataframe(
    df, use_container_width=True, hide_index=True, height=420,
    column_config={
        "ros": st.column_config.NumberColumn(
            "ros", format="%.1f",
            help="What he is worth from this week to the end, with the playoff "
                 "weeks scaled by our odds of getting there. Inheritance is "
                 "INSIDE this number, not added on, so there is no separate "
                 "hold column: what a drop costs is this series re-run through "
                 "the lineup, which robo/marginal.py does."),
        "k": st.column_config.NumberColumn(
            "k", format="%.2f",
            help="The calibration residual: how many times more the market pays "
                 "for him than our model of his role explains. Near 1 means "
                 "injury luck accounts for him. Three or four means the market "
                 "is pricing a job he does not hold yet."),
        "raw": st.column_config.NumberColumn(
            "raw", format="%.1f",
            help="The structural model before calibration. A thin raw total "
                 "makes k arithmetic rather than evidence -- see is_puzzle()."),
        "target": st.column_config.NumberColumn(
            "target", format="%.1f",
            help="What the market says he is worth over the games left: the "
                 "season projection prorated, released later if reporting gives "
                 "a return date the feeds do not carry."),
        "share": st.column_config.NumberColumn(
            "share", format="%.3f",
            help="His share of his position room's projected opportunity, THIS "
                 "week. A man barred from playing leaves the room, so the men "
                 "behind him move up for exactly the weeks he is out."),
        "why k": st.column_config.TextColumn(
            "why k",
            help="Whether the level was pinned to the market or released from "
                 "it, and on what."),
    })

# ------------------------------------------------------------------- the detail

st.divider()
st.subheader("Player detail")
names = [r["name"] for r in view] or [r["name"] for r in rows]
pick = st.selectbox("Player", names, help="Follows the filters above.")
row = next(r for r in rows if r["name"] == pick)

m = st.columns(4)
m[0].metric("rest of season", f"{row['ros']:.1f}")
m[1].metric("k", "-" if row.get("k") is None else f"{row['k']:.2f}")
m[2].metric("raw", f"{row['raw']:.1f}")
m[3].metric("market target", f"{row['target']:.1f}")
if row.get("k_why"):
    st.caption(f"**level:** {row['k_why']}")
bits = []
if row.get("lead_of"):
    bits.append(f"inherits {(row.get('absorbs') or 0):.0%} of {row['lead_of']}'s "
                f"work if that job opens")
if row.get("eligible_week"):
    bits.append(f"barred from playing until week {row['eligible_week']}"
                + (f" ({row['floor_source']})" if row.get("floor_source") else ""))
if row.get("scout_return"):
    bits.append(f"reporting says week {row['scout_return']}")
if bits:
    st.caption(" · ".join(bits))

by = row.get("by_week") or {}
weeks = sorted(int(w) for w in by)
horizon = list(range(wk, (max(weeks) if weeks else wk) + 1))
byes = [w for w in horizon if w not in weeks]

wdf = pd.DataFrame([{
    "week": w,
    "available": by[str(w)].get("a", 0.0),
    "his role": by[str(w)].get("s1", 0.0),
    "if it opens": by[str(w)].get("s2", 0.0),
    "rank": by[str(w)].get("rank"),
    "weight": ros.weight_of(meta, w),
    "contributes": round(by[str(w)].get("final", 0.0) * ros.weight_of(meta, w), 2),
} for w in weeks])

left, right = st.columns([3, 2])
with left:
    st.markdown("**Week by week**")
    st.dataframe(
        wdf, use_container_width=True, hide_index=True, height=330,
        column_config={
            "available": st.column_config.NumberColumn(
                format="%.2f",
                help="P(he is on the field that week). Zero before the week the "
                     "rules allow him back -- that is a rule, not a forecast."),
            "his role": st.column_config.NumberColumn(
                format="%.2f", help="What the market projects him in the role "
                                    "he currently holds."),
            "if it opens": st.column_config.NumberColumn(
                format="%.2f",
                help="What he picks up if the job ahead of him comes open, at "
                     "the fitted absorption for his rank THAT week. The rank "
                     "moves when a man ahead of him is barred from playing."),
            "weight": st.column_config.NumberColumn(
                format="%.2f",
                help="1.00 through the regular season. The playoff weeks are "
                     "scaled by our odds of playing them, which is why they "
                     "taper."),
            "contributes": st.column_config.NumberColumn(format="%.2f"),
        })
    if byes:
        st.caption(f"No game in week {', '.join(map(str, byes))} — absent from "
                   f"the sum, never counted as a zero.")
with right:
    st.markdown("**What each week contributes**")
    # The taper on the right of this chart IS the playoff-odds weighting. It is
    # the single most explanatory picture on the page: a contender's tail stays
    # tall, a dead team's collapses.
    st.bar_chart(wdf.set_index("week")["contributes"], height=300)

st.markdown("**How the total is built**")
# WEIGHTED, because row["raw"] is not. That field is the unweighted sum the
# calibration divides into, so printing it next to k and ros gives three numbers
# that do not multiply out -- and this page exists to be checked by hand.
wraw = sum(by[str(w)].get("pts", 0.0) * ros.weight_of(meta, w) for w in weeks)
b = st.columns(3)
b[0].metric("structural model", f"{wraw:.2f}",
            help="the sum of A(w) x (his role + what he may inherit) over the "
                 "weeks left, each week scaled by our odds of playing it, and "
                 "before the market has any say. The unweighted version of this "
                 "number is what k was fitted against; it reads "
                 f"{row['raw']:.2f}.")
b[1].metric("x k", "-" if row.get("k") is None else f"{row['k']:.3f}",
            help="the residual that pins the total to what the market says he "
                 "is worth. Everything the structural model does not explain "
                 "lands here, which is why reading it is the point.")
b[2].metric("= rest of season", f"{row['ros']:.2f}")
if row.get("k_source") == "season-only":
    st.caption("No shape to scale: the structural model gives him almost "
               "nothing, so the market's number is spent evenly over the games "
               "left. Flat is wrong about WHEN and right about HOW MUCH.")

with st.expander("Show the full trace", expanded=False):
    st.caption("Every stage from the feeds to the printed total, with the file "
               "each value came from. Same text as "
               f"`python -m robo.expected --explain \"{pick}\"`.")
    ui.trace_block(st, trace_for(row["player_id"], snapshot))
