"""Rest-of-season value, and how each number was arrived at.

Reads data/ros.json and nothing else on the hot path. That file is what the bot
acted on; recomputing here would show a number nobody used, and the reader would
have no way to tell the two apart.

The one thing this page computes live is the inheritance chain, because
roles.py's panel is lru_cached and cheap. It is recorded through the real code
path (`upside_of(..., record=...)`), not reimplemented.
"""

import pandas as pd
import streamlit as st

from robo import ros, ui

st.title("Rest of season")
ui.gate_banner(st)


@st.cache_data(ttl=600, show_spinner="Reading the rest-of-season table…")
def board() -> tuple[list, dict]:
    d = ros.load()
    rows = list((d.get("players") or {}).values())
    meta = {k: v for k, v in d.items() if k != "players"}
    return rows, meta


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
def trace_for(pid: str) -> str:
    # By ID, never by name. "Josh Allen" is a quarterback and a linebacker, and
    # re-resolving the name here picked the linebacker while the table above
    # showed the quarterback.
    #
    # reasons=True because this app is local and unredacted by design -- it is
    # where the sentence behind a scout verdict is supposed to be readable. The
    # default is off for skills.py, which answers the same question in the
    # league chat.
    return ros.trace(player_id=pid, reasons=True)


rows, meta = board()
if not rows:
    st.error("data/ros.json is empty or unreadable. Rebuild with "
             "`python -m robo.ros --refresh`.")
    st.stop()

own = ownership()
wk = meta.get("week")

c = st.columns(4)
c[0].metric("Week", wk)
c[1].metric("Players", len(rows))
c[2].metric("Computed", ui.fmt_age(meta.get("computed")))
c[3].metric("Playoff weeks weighted",
            " / ".join(f"{ros.weight_of(meta, w):.2f}" for w in (15, 16, 17)))
st.caption(meta.get("provenance", ""))

# ------------------------------------------------------------------- the board

st.subheader("The board")
f1, f2, f3 = st.columns([2, 2, 3])
with f1:
    scope = st.radio("Roster", ["everyone", "mine", "rostered", "free agents"],
                     horizontal=True,
                     help="Who holds him right now, read live from Sleeper.")
with f2:
    sort_by = st.selectbox("Sort by", ["hold", "mean", "upside", "now"],
                           help="`mean` prices an ADD, `hold` prices a DROP. "
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
    "mean": r["mean"], "upside": r["upside"], "hold": r["hold"],
    "this week": r["now"], "weeks": r["weeks"],
    "news": r["news_mult"], "sources": r["source"],
} for r in view])

st.caption(f"{len(df)} of {len(rows)} players")
st.dataframe(
    df, use_container_width=True, hide_index=True, height=420,
    column_config={
        "mean": st.column_config.NumberColumn(
            "mean", format="%.1f",
            help="What he is worth to us from this week to the end, with the "
                 "playoff weeks scaled by our odds of getting there. This is "
                 "the number an ADD is judged on."),
        "upside": st.column_config.NumberColumn(
            "upside", format="%.1f",
            help="Expected points from inheriting the job ahead of him if that "
                 "man goes down. Measured: a fitted absorption curve over 1,242 "
                 "real vacancies, times how often that position misses a week."),
        "hold": st.column_config.NumberColumn(
            "hold", format="%.1f",
            help="mean + upside. What it COSTS to lose him, which is the number "
                 "a DROP is judged on. The gap between this and `mean` is the "
                 "whole reason the bot does not cut a rookie in October."),
        "this week": st.column_config.NumberColumn(format="%.1f"),
        "news": st.column_config.NumberColumn(
            "news", format="%.3f",
            help="Scout verdict multiplier, applied ONCE to the future weeks "
                 "only — never to the current week, where the projection has "
                 "already seen the news."),
        "sources": st.column_config.TextColumn(
            "sources",
            help="Which engine priced each week, and how many weeks each. A "
                 "defence reading 'fallbackx9' is on a season-average rate for "
                 "nine of its weeks because no line is posted that far out."),
    })

# ------------------------------------------------------------------- the detail

st.divider()
st.subheader("Player detail")
names = [r["name"] for r in view] or [r["name"] for r in rows]
pick = st.selectbox("Player", names, help="Follows the filters above.")
row = next(r for r in rows if r["name"] == pick)

m = st.columns(4)
m[0].metric("mean", f"{row['mean']:.1f}")
m[1].metric("upside", f"{row['upside']:.1f}")
m[2].metric("hold", f"{row['hold']:.1f}")
m[3].metric("this week", f"{row['now']:.1f}")
if row.get("upside_why"):
    st.caption(f"**upside:** {row['upside_why']}")

by = row.get("by_week") or {}
weeks = sorted(int(w) for w in by)
horizon = list(range(wk, (max(weeks) if weeks else wk) + 1))
byes = [w for w in horizon if w not in weeks]

wdf = pd.DataFrame([{
    "week": w,
    "rate": by[str(w)].get("pts", 0.0),
    "source": by[str(w)].get("src", "?"),
    "opponent": by[str(w)].get("opp") or "-",
    "weight": ros.weight_of(meta, w),
    "contributes": round(by[str(w)].get("pts", 0.0) * ros.weight_of(meta, w), 2),
} for w in weeks])

left, right = st.columns([3, 2])
with left:
    st.markdown("**Week by week**")
    st.dataframe(
        wdf, use_container_width=True, hide_index=True, height=330,
        column_config={
            "rate": st.column_config.NumberColumn(format="%.2f"),
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

srcs = sorted({by[str(w)].get("src", "?") for w in weeks})
for s in srcs:
    if s in ui.SOURCE_HELP:
        n = sum(1 for w in weeks if by[str(w)].get("src") == s)
        st.caption(f"**{s}** ({n} wk): {ui.SOURCE_HELP[s]}")

st.markdown("**How the total is built**")
b = st.columns(3)
b[0].metric("this week", f"{row['now_term']:.2f}",
            help="the current week's rate times its weight")
b[1].metric("future weeks", f"{row['future_term'] * row['news_mult']:.2f}",
            help=f"{row['future_term']:.2f} times the news multiplier "
                 f"{row['news_mult']:.3f}")
b[2].metric("= mean", f"{row['mean']:.2f}")

with st.expander("Show the full trace",
                 expanded=False):
    st.caption("Every stage from the feeds to the printed total, with the file "
               "each value came from. Same text as "
               f"`python -m robo.ros --explain \"{pick}\"`.")
    ui.trace_block(st, trace_for(row["player_id"]))
