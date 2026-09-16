"""What a player is worth, what changed, and how the number was arrived at.

Reads data/expected.json on the hot path -- the weekly model the bot acts on.
It used to read ros.json, and that was the wrong surface to review from: this
page is what you look at to decide whether to trust the new valuation, and it
was showing the superseded one. Carson Beck rendered at 0.4 here while the
model that prices the drop put him at 37.5.

MOVERS ARE A SORT, NOT A PAGE. Ranking the largest changes and listing the
board are the same table read two ways, and splitting them meant the movers
view could not answer "what is he worth" and the board could not answer "what
moved". The preset picks the order and the summary; the detail below is the
same either way.

Kickers and team defences are absent, because expected.py models neither --
they refill from the wire every week and robo/streaming.py prices them.

The one thing this page computes live is the inheritance chain, because
roles.py's panel is lru_cached and cheap. It is recorded through the real code
path (`upside_of(..., record=...)`), not reimplemented.
"""

import pandas as pd
import streamlit as st

from robo import evidence, expected, ros, ui, value_history

st.title("Players")
ui.gate_banner(st)


@st.cache_data(ttl=600, show_spinner="Reading the rest-of-season table…")
def board() -> tuple[list, dict]:
    d = expected.load()
    rows = list((d.get("players") or {}).values())
    return rows, {k: v for k, v in d.items() if k != "players"}


@st.cache_data(ttl=600, show_spinner="Reading rosters from Sleeper…")
def ownership() -> dict:
    """player_id -> 'mine' | 'rostered'. Never fatal.

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
    return expected.trace(player_id=pid)


@st.cache_data(ttl=600, show_spinner=False)
def scout_reason(pid: str) -> dict:
    """The verdict's own words. Local app only -- see status._scrub()."""
    try:
        import json as _json
        from robo import scout
        d = _json.loads(scout.VERDICTS.read_text(encoding="utf-8"))
        return (d.get("verdicts") or {}).get(str(pid)) or {}
    except Exception:
        return {}


rows, meta = board()
if not rows:
    st.error("data/expected.json is empty or unreadable. Rebuild with "
             "`python -m robo.expected --rebuild`.")
    st.stop()

own = ownership()
wk = meta.get("week")
current = {**meta, "players": {r["player_id"]: r for r in rows}}

day_base = value_history.baseline(current, 1)
week_base = value_history.baseline(current, 7)
day_change = value_history.compare(current, day_base)
week_change = value_history.compare(current, week_base)

# WHEN THE WEEK BASELINE IS RECONSTRUCTED, IT IS TWO NUMBERS. The archives
# that predate this one cover different halves of a value change: the
# projection archive knows what Sleeper's weekly numbers did and nothing about
# an injury, and the recorded cascades know the opposite. Blending them would
# rank the wrong movers, so they are reported apart.
#
# AND THEY MUST COVER THE SAME WINDOW. The two archives do not start on the
# same day -- projections reach back to 4 Sep, cascades only to the 13th -- so
# asking each for "seven days" returns a seven-day projection change beside a
# news change that does not exist, and calling their sum the total would be
# arithmetic across two different spans. The split is therefore taken at the
# longest window BOTH can honour, and the column headings say what that is.
def _split_baselines(cur: dict):
    snaps = value_history.snapshots()
    now = float(cur.get("computed") or 0.0)
    oldest = {}
    for method in ("projection", "news"):
        rows = [float(d["computed"]) for d in snaps if d.get("method") == method]
        if not rows:
            return None, None, 0.0
        oldest[method] = min(rows)
    span = (now - max(oldest.values()) - 1) / 86400.0
    if span <= 0:
        return None, None, 0.0
    return (value_history.baseline(cur, span, method="projection"),
            value_history.baseline(cur, span, method="news"), span)


week_proj = week_news = None
week_proj_change = week_news_change = {}
split_days = 0.0
if week_base and week_base.get("reconstructed"):
    week_proj, week_news, split_days = _split_baselines(current)
    if week_proj and week_news:
        week_proj_change = value_history.compare(current, week_proj)
        week_news_change = value_history.compare(current, week_news)
    else:
        week_proj = week_news = None

span_label = f"{split_days:.0f}d" if split_days >= 1 else f"{split_days * 24:.0f}h"
proj_col, news_col = f"{span_label} Δ projection", f"{span_label} Δ news"

c = st.columns(4)
c[0].metric("Week", wk)
c[1].metric("Players", len(rows))
c[2].metric("Computed", ui.fmt_age(meta.get("computed")))
c[3].metric("Playoff weeks weighted",
            " / ".join(f"{ros.weight_of(meta, w):.2f}" for w in (15, 16, 17)))
st.caption(
    f"Built from the NFL model's weekly means for weeks {wk}-17, with Sleeper as a "
    "missing-row fallback, fitted role inheritance, and return bounds. No season-total "
    "calibration is used. Kickers and defences are absent by design: they refill from "
    "the wire weekly and robo/streaming.py prices them.")

# ------------------------------------------------------------------- the board
st.subheader("The board")
preset = st.radio("View", ["Everyone", "Risers", "Fallers"], horizontal=True,
                  help="Risers and Fallers are the same table sorted by the change "
                       "column, with the summary above it.")
# The sortable change columns are exactly the ones the table shows. When the
# long comparison is split in two there is no single "week change" to rank on,
# and offering one would sort by a number that is not in any column.
CHANGES = {"day change": day_change}
if week_proj is not None:
    CHANGES[f"{span_label} projection change"] = week_proj_change
    CHANGES[f"{span_label} news change"] = week_news_change
else:
    CHANGES["week change"] = week_change

period = list(CHANGES)[0]
if preset != "Everyone":
    period = st.radio("Over", list(CHANGES), horizontal=True, key="movers_period")

f1, f2, f3 = st.columns([2, 2, 3])
with f1:
    scope = st.radio("Roster", ["everyone", "mine", "rostered", "free agents"],
                     horizontal=True, help="Who holds him right now, read live from Sleeper.")
with f2:
    if preset == "Everyone":
        sort_by = st.selectbox("Sort by", ["ros", "raw"] + list(CHANGES),
                               help="Changes compare the same remaining weeks under "
                                    "today's playoff weights, so completed games do not "
                                    "appear as lost value.")
    else:
        sort_by = period
        st.caption(f"Sorted by {period}.")
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

change_for_sort = CHANGES.get(sort_by)
if change_for_sort is not None:
    # A player absent from the baseline has no change; sorting him to an
    # extreme would put "we have no comparison" at the top of a movers list.
    view = [r for r in view if r["player_id"] in change_for_sort]
    view = sorted(view, key=lambda r: change_for_sort[r["player_id"]]["delta"],
                  reverse=preset != "Fallers")
else:
    view = sorted(view, key=lambda r: -r.get(sort_by, 0))

if preset != "Everyone" and view:
    moved = [r for r in view
             if abs(change_for_sort[r["player_id"]]["delta"]) >= 0.5]
    s = st.columns(3)
    top = view[0]
    s[0].metric(f"Biggest {'rise' if preset == 'Risers' else 'fall'}", top["name"],
                f"{change_for_sort[top['player_id']]['delta']:+.1f}")
    s[1].metric("Players moved", len(moved),
                help="At least half a point after the horizon adjustment.")
    s[2].metric("Comparable", len(change_for_sort),
                help="Players present in both the current table and the baseline.")
    chart = pd.DataFrame([{"player": r["name"],
                           "change": change_for_sort[r["player_id"]]["delta"]}
                          for r in view[:12]]).set_index("player")
    st.bar_chart(chart, horizontal=True, height=340)


def _delta(table, pid):
    return (table.get(pid) or {}).get("delta")


record = []
for r in view:
    pid = r["player_id"]
    row = {"player": r["name"], "pos": r["pos"], "team": r["team"] or "-",
           "owner": own.get(pid, "free"), "ros": r["ros"], "raw": r["raw"],
           "day Δ": _delta(day_change, pid)}
    if week_proj is not None:
        row[proj_col] = _delta(week_proj_change, pid)
        row[news_col] = _delta(week_news_change, pid)
    else:
        row["week Δ"] = _delta(week_change, pid)
    row.update({"rank": r.get("rank"), "share": r.get("share"), "weeks": r["weeks"],
                "source": r.get("value_source", "weekly-model")})
    record.append(row)
df = pd.DataFrame(record)

st.caption(f"{len(df)} of {len(rows)} players")
st.dataframe(
    df, use_container_width=True, hide_index=True, height=420,
    column_config={
        "ros": st.column_config.NumberColumn(
            "ros", format="%.1f",
            help="What he is worth from this week to the end, with the playoff weeks "
                 "scaled by our odds of getting there. Inheritance is INSIDE this "
                 "number, not added on, so there is no separate hold column: what a "
                 "drop costs is this series re-run through the lineup, which "
                 "robo/marginal.py does."),
        "raw": st.column_config.NumberColumn(
            "raw", format="%.1f", help="The unweighted sum of weekly modeled value."),
        "day Δ": st.column_config.NumberColumn(
            "day Δ", format="%+.1f",
            help="Change versus the latest snapshot at least 24 hours old, after "
                 "removing completed weeks and using today's weights."),
        "week Δ": st.column_config.NumberColumn(
            "week Δ", format="%+.1f",
            help="Change versus the latest snapshot at least seven days old, after "
                 "removing completed weeks and using today's weights."),
        proj_col: st.column_config.NumberColumn(
            proj_col, format="%+.1f",
            help="How much of the change is Sleeper's weekly projection moving, "
                 "reconstructed from the projection archive. It knows nothing about an "
                 "injury: Sleeper does not propagate an absence into future weeks."),
        news_col: st.column_config.NumberColumn(
            news_col, format="%+.1f",
            help="How much is news — injuries, roles, availability — replayed from the "
                 "cascades that measured it at the time. Zero means no recorded event "
                 "touched him, not that nothing happened."),
        "share": st.column_config.NumberColumn(
            "share", format="%.3f",
            help="His share of his position room's projected opportunity, THIS week. A "
                 "man barred from playing leaves the room, so the men behind him move "
                 "up for exactly the weeks he is out."),
    })

if week_proj is not None:
    observed = [d for d in value_history.snapshots() if not d.get("reconstructed")]
    began = (value_history.age_label(current, min(observed, key=lambda d: d["computed"]))
             if observed else "recently")
    st.info(
        f"**The long comparison is reconstructed, and it is two numbers over {span_label}.** "
        f"Snapshots of the value table only began {began}, so anything longer is rebuilt "
        "from two older archives that each know one half of a change. **Projection** is "
        "Sleeper's weekly number moving, substituted exactly into today's availability "
        "and role terms. **News** is injuries and role changes, replayed from the "
        f"cascades that measured them at the time. Both are shown over {span_label} — the "
        "longest window both archives cover — so their sum is arithmetic over one span "
        "rather than two. Neither alone is the answer: Sleeper's projection barely moves "
        "for a man ruled out for a month, which is exactly when the news column is the "
        "whole story. A full week-over-week reading takes over once an observed snapshot "
        "is seven days old.")
else:
    available = []
    if day_base:
        available.append(f"day baseline {value_history.age_label(current, day_base)}")
    if week_base:
        available.append(f"week baseline {value_history.age_label(current, week_base)}")
    st.caption(("Baselines: " + " · ".join(available)) if available else
               "No baseline old enough to compare against yet.")

# ------------------------------------------------------------------ the detail
st.divider()
st.subheader("One player, end to end")
names = [r["name"] for r in view] or [r["name"] for r in rows]
# A player picked from a decision arrives as ?player=<id>.
wanted = st.query_params.get("player")
by_id = {r["player_id"]: r for r in rows}
default = 0
if wanted in by_id and by_id[wanted]["name"] in names:
    default = names.index(by_id[wanted]["name"])
pick = st.selectbox("Player", names, index=default, help="Follows the filters above.")
row = next(r for r in rows if r["name"] == pick)
pid = row["player_id"]

m = st.columns(6)
m[0].metric("rest of season", f"{row['ros']:.1f}")
m[1].metric("weekly sum", f"{row['raw']:.1f}")
m[2].metric("day change", f"{day_change[pid]['delta']:+.1f}" if pid in day_change else "—")
if week_proj is not None:
    m[3].metric(f"{span_label}: projection",
                f"{week_proj_change[pid]['delta']:+.1f}" if pid in week_proj_change else "—")
    m[4].metric(f"{span_label}: news",
                f"{week_news_change[pid]['delta']:+.1f}" if pid in week_news_change else "—")
else:
    m[3].metric("week change",
                f"{week_change[pid]['delta']:+.1f}" if pid in week_change else "—")
    m[4].metric("weeks modeled", row["weeks"])
m[5].metric("source", row.get("value_source", "weekly-model"))
st.caption(ui.SOURCE_HELP.get(str(row.get("value_source")), ""))

bits = []
if row.get("lead_of"):
    bits.append(f"inherits {(row.get('absorbs') or 0):.0%} of {row['lead_of']}'s work "
                "if that job opens")
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
                help="P(he is on the field that week). Zero before the week the rules "
                     "allow him back -- that is a rule, not a forecast."),
            "his role": st.column_config.NumberColumn(
                format="%.2f",
                help="What the market projects him in the role he currently holds."),
            "if it opens": st.column_config.NumberColumn(
                format="%.2f",
                help="What he picks up if the job ahead of him comes open, at the fitted "
                     "absorption for his rank THAT week. The rank moves when a man ahead "
                     "of him is barred from playing."),
            "weight": st.column_config.NumberColumn(
                format="%.2f",
                help="1.00 through the regular season. The playoff weeks are scaled by "
                     "our odds of playing them, which is why they taper."),
            "contributes": st.column_config.NumberColumn(format="%.2f"),
        })
    if byes:
        st.caption(f"No game in week {', '.join(map(str, byes))} — absent from the sum, "
                   "never counted as a zero.")
with right:
    st.markdown("**What each week contributes**")
    # The taper on the right of this chart IS the playoff-odds weighting. It is
    # the single most explanatory picture on the page: a contender's tail stays
    # tall, a dead team's collapses.
    st.bar_chart(wdf.set_index("week")["contributes"], height=300)

st.markdown("**How the total is built**")
# WEIGHTED, because row["raw"] is the unweighted weekly sum.
wraw = sum(by[str(w)].get("pts", 0.0) * ros.weight_of(meta, w) for w in weeks)
b = st.columns(2)
b[0].metric("weighted weekly model", f"{wraw:.2f}",
            help="the sum of A(w) x (his role + what he may inherit) over the weeks "
                 "left, each week scaled by our odds of playing it. The unweighted "
                 f"version reads {row['raw']:.2f}.")
b[1].metric("= rest of season", f"{row['ros']:.2f}")

with st.expander("Every stage, from the feeds to the printed total"):
    st.caption("The file each value came from, in order. Same text as "
               f"`python -m robo.expected --explain \"{pick}\"`.")
    ui.trace_block(st, trace_for(pid))

verdict = scout_reason(pid)
if verdict.get("reason"):
    with st.expander(f"What the reporting actually said — {verdict.get('verdict', '?')} "
                     f"(confidence {verdict.get('confidence', '?')})"):
        st.caption("The scout verdict's own words, from data/news_verdicts.json. Local "
                   "only: these quote injury reporting verbatim, which is why the "
                   "published trace carries a `reasons` flag that is off everywhere the "
                   "league can read the output.")
        st.write(verdict["reason"])
        fields = evidence.explain_fields(
            {k: verdict.get(k) for k in
             ("return_week", "return_week_min", "return_week_max", "return_basis",
              "timing_actionable", "timing_sentence", "out_for_season", "judged_at")
             if verdict.get(k) is not None})
        if fields:
            st.dataframe(pd.DataFrame(fields)[["field", "value", "meaning"]],
                         use_container_width=True, hide_index=True,
                         column_config={"meaning":
                                        st.column_config.TextColumn(width="large")})
