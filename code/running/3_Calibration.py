"""How the machinery was fitted, and on how much.

The other pages show what the bot decided. This one shows the curves those
decisions were read off, and how many observations sit behind each cell -- the
question to ask when a number on the Decisions page looks wrong is usually not
"was the arithmetic right" but "was that cell fitted on four events".

READER ONLY, and more carefully here than elsewhere: several of these modules
fall back to REFITTING and rewriting their artifact when the file on disk is
bad. That is correct in production and wrong in an audit tool, which must
render a broken fit as an error rather than quietly repair it and then report
that everything is fine. Each loader below reads the file directly for that
reason.
"""

import json

import pandas as pd
import streamlit as st

from robo import DATA, evidence, expected, roles, ui

st.title("Calibration")
ui.gate_banner(st)


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@st.cache_data(ttl=600, show_spinner=False)
def load():
    return (_read(roles.FIT_FILE), expected.load(),
            _read(DATA / "streaming_fit.json"), _read(DATA / "returns_fit.json"))


fit, table, streaming_fit, returns_fit = load()

roles_tab, stream_tab, returns_tab = st.tabs(
    ["Who inherits a job", "Streaming a defence", "When a man comes back"])

# ------------------------------------------------------------------ roles
with roles_tab:
    if fit.get("error") or not fit.get("curve"):
        st.error("The fitted role artifact is unavailable: "
                 + str(fit.get("error") or "empty fit"))
    else:
        st.caption("Fitted on real vacancies, not assumed. A quarterback's snaps go to "
                   "one man, so QB2 absorbs almost all of them; targets spread across a "
                   "route tree, so WR2 absorbs far less and ranks 3 and 4 absorb more "
                   "than you would guess. Rank comes from projected opportunity — "
                   "Sleeper's depth chart is used for one thing only, placing a man the "
                   "season forecast omits entirely, and never to decide who holds the "
                   "lead role. Those rows are marked.")
        m = st.columns(4)
        m[0].metric("Vacancy events", fit.get("events"))
        m[1].metric("Seasons", "–".join(map(str, fit.get("seasons") or [])))
        m[2].metric("Established role",
                    f"{float(fit.get('min_established_share') or 0):.0%}")
        m[3].metric("Fit computed", ui.fmt_age(fit.get("fitted")))
        st.caption(f"A role is established over a {fit.get('window')}-week window. Miss "
                   "rate is vacancy events divided by weeks an established role was "
                   "held; absorption is the share of the vacated lead role picked up by "
                   "each rank. Both are counted, not assumed.")

        st.subheader("Absorption curve")
        curve_rows = []
        for pos, cells in (fit.get("curve") or {}).items():
            for rank, cell in cells.items():
                effective, source = roles.absorption(pos, int(rank))
                curve_rows.append({"pos": pos, "rank": int(rank), "events": cell.get("n"),
                                   "fitted mean": cell.get("mean"),
                                   "median": cell.get("median"), "sd": cell.get("sd"),
                                   "used by model": effective, "source": source})
        curve_rows.sort(key=lambda r: (r["pos"], r["rank"]))
        position = st.radio("Position", sorted({r["pos"] for r in curve_rows}),
                            horizontal=True)
        prows = [r for r in curve_rows if r["pos"] == position]
        left, right = st.columns([3, 2])
        with left:
            st.dataframe(pd.DataFrame(prows), use_container_width=True, hide_index=True,
                         column_config={
                             "events": st.column_config.NumberColumn(
                                 help="Observations behind this cell. A thin cell falls "
                                      "back to a pooled value and the source column "
                                      "says so."),
                             "fitted mean": st.column_config.NumberColumn(format="%.3f"),
                             "median": st.column_config.NumberColumn(format="%.3f"),
                             "sd": st.column_config.NumberColumn(format="%.3f"),
                             "used by model": st.column_config.NumberColumn(
                                 format="%.3f",
                                 help="What expected.py actually uses, which is not the "
                                      "fitted mean when the cell is thin."),
                         })
        with right:
            st.bar_chart(pd.DataFrame(prows).set_index("rank")[["used by model"]],
                         height=280)
        st.caption(f"{position} lead-role miss rate: "
                   f"{float((fit.get('miss_rate') or {}).get(position, 0)):.2%} per week · "
                   f"{(fit.get('events_by_pos') or {}).get(position, 0)} observed vacancies.")

        with st.expander("Long-term role takeovers"):
            st.caption("A separate event: a player who began as a backup holds the lead "
                       "role by week 10.")
            st.dataframe(pd.DataFrame([
                {"pos": pos, "cohort": cohort, "players": cell.get("n"),
                 "takeover rate": f"{float(cell.get('rate') or 0):.1%}"}
                for pos, groups in (fit.get("takeover") or {}).items()
                for cohort, cell in groups.items()]),
                use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("A team's position room right now")
        players = list((table.get("players") or {}).values())
        room_players = [p for p in players
                        if p.get("team") and p.get("pos") in roles.PROJ_OPPORTUNITY]
        if not room_players:
            st.error("The current rest-of-season table contains no position-room records.")
        else:
            sel = st.columns(2)
            team = sel[0].selectbox("Team", sorted({p["team"] for p in room_players}))
            positions = sorted({p["pos"] for p in room_players if p["team"] == team})
            room_pos = sel[1].selectbox("Position room", positions)
            room = sorted((p for p in room_players
                           if p["team"] == team and p["pos"] == room_pos),
                          key=lambda p: (p.get("rank") is None, p.get("rank") or 999,
                                         p["name"]))
            st.caption(f"Saved in data/expected.json {ui.fmt_age(table.get('computed'))}; "
                       f"week {table.get('week')} eligibility is already reflected in "
                       "these ranks.")
            st.dataframe(pd.DataFrame([{
                "rank": p.get("rank"), "player": p.get("name"),
                "share": f"{float(p.get('share') or 0):.1%}",
                # A 0.0% share next to a real `absorbs` looks like a bug unless
                # the reader is told the slot is a depth-chart placement rather
                # than a forecast. A man with no rank is not IN the room.
                "slot from": ("not in the room" if p.get("rank") is None else
                              f"depth chart (listed {p['from_depth_chart']})"
                              if p.get("from_depth_chart") else "projected opportunity"),
                "lead": ("—" if p.get("rank") is None
                         else p.get("lead_of") or "holds lead role"),
                "absorbs": f"{float(p.get('absorbs') or 0):.1%}",
                "ROS": p.get("ros"), "eligible week": p.get("eligible_week"),
            } for p in room]), use_container_width=True, hide_index=True,
                column_config={"ROS": st.column_config.NumberColumn(format="%.1f")})
            st.caption("`absorbs` is a share of the LEAD's vacated work, not of the man "
                       "one rung up — from rank 3 down those are different people.")

        with st.expander("The fitted artifact, field by field"):
            st.dataframe(pd.DataFrame(evidence.explain_fields(fit))[
                             ["path", "value", "meaning"]],
                         use_container_width=True, hide_index=True, height=300,
                         column_config={"meaning":
                                        st.column_config.TextColumn(width="large")})
            st.caption(str(roles.FIT_FILE))

# -------------------------------------------------------------- streaming
with stream_tab:
    if streaming_fit.get("error") or not streaming_fit.get("curve"):
        st.error("The streaming fit is unavailable: "
                 + str(streaming_fit.get("error") or "empty fit"))
    else:
        st.caption("What a defence is worth against the opponent's implied point total, "
                   "fitted on this league's own scoring. **It refuses to rank kickers**, "
                   "and that refusal is the finding: the same fit on kicker weeks runs "
                   "flat, barely correlated and not monotone, so a kicker streaming "
                   "recommendation would be a confident number with nothing behind it.")
        m = st.columns(3)
        m[0].metric("Seasons", "–".join(map(str, streaming_fit.get("seasons") or [])))
        m[1].metric("Fit computed", ui.fmt_age(streaming_fit.get("fitted")))
        stats = streaming_fit.get("stats") or {}
        m[2].metric("Observations", stats.get("n") or stats.get("rows") or "—")
        curve = streaming_fit.get("curve") or {}
        cdf = pd.DataFrame([{"opponent implied total": k, "defence points": v}
                            for k, v in curve.items()])
        left, right = st.columns([3, 2])
        with left:
            st.dataframe(cdf, use_container_width=True, hide_index=True,
                         column_config={"defence points":
                                        st.column_config.NumberColumn(format="%.2f")})
        with right:
            st.bar_chart(cdf.set_index("opponent implied total"), height=300)
        st.caption("Monotone across every bucket: the weaker the offence a defence "
                   "faces, the more it scores. That is what makes the curve usable.")
        with st.expander("The fitted artifact, field by field"):
            st.dataframe(pd.DataFrame(evidence.explain_fields(streaming_fit))[
                             ["path", "value", "meaning"]],
                         use_container_width=True, hide_index=True, height=300,
                         column_config={"meaning":
                                        st.column_config.TextColumn(width="large")})

# ---------------------------------------------------------------- returns
with returns_tab:
    if returns_fit.get("error") or not returns_fit.get("curve"):
        st.error("The returns fit is unavailable: "
                 + str(returns_fit.get("error") or "empty fit"))
    else:
        st.caption("How likely a man is back after a given number of weeks out, by body "
                   "part. This is what turns a designation into the availability term "
                   "`a` on the Players page — and it is read at weeks-served-so-far "
                   "plus weeks-ahead, never at the target week, or the same elapsed "
                   "time gets counted twice.")
        m = st.columns(4)
        m[0].metric("Spells", returns_fit.get("spells"))
        m[1].metric("Censored", returns_fit.get("censored"),
                    help="Spells still running when the data ended. Dropping them would "
                         "bias every curve toward quick returns.")
        m[2].metric("Seasons", "–".join(map(str, returns_fit.get("seasons") or [])))
        m[3].metric("Fit computed", ui.fmt_age(returns_fit.get("fitted")))
        curve = returns_fit.get("curve") or {}
        pooled = returns_fit.get("pooled") or {}
        parts = sorted(curve, key=lambda p: -(curve[p].get("n") or 0))
        if parts:
            part = st.selectbox(
                "Body part", parts,
                format_func=lambda p: f"{p} ({curve[p].get('n', 0)} spells)",
                help=f"A part with fewer than {returns_fit.get('min_events')} spells "
                     "falls back to the pooled curve.")
            cell = curve.get(part) or {}
            # The artifact stores S -- SURVIVAL, the chance he is STILL OUT
            # after that many weeks. The question anyone asks is the
            # complement, so it is shown that way and the pooled curve is put
            # beside it, which is the comparison that says whether this part
            # behaves differently from injuries in general.
            surv = list(cell.get("S") or [])
            pooled_surv = list(pooled.get("S") or [])
            rdf = pd.DataFrame([{
                "weeks out": i + 1,
                f"back — {part}": round(1.0 - float(s), 4),
                "back — all injuries": (round(1.0 - float(pooled_surv[i]), 4)
                                        if i < len(pooled_surv) else None),
            } for i, s in enumerate(surv)])
            thin = (cell.get("n") or 0) < (returns_fit.get("min_events") or 0)
            if thin:
                st.warning(f"Only {cell.get('n')} spells for {part}, under the "
                           f"{returns_fit.get('min_events')} this fit requires, so the "
                           "pooled curve is what the model actually uses.")
            left, right = st.columns([3, 2])
            with left:
                st.dataframe(rdf, use_container_width=True, hide_index=True, height=300,
                             column_config={
                                 f"back — {part}": st.column_config.NumberColumn(
                                     format="%.3f",
                                     help="Chance he has returned by this many weeks out."),
                                 "back — all injuries": st.column_config.NumberColumn(
                                     format="%.3f",
                                     help="The pooled curve across every body part, for "
                                          "comparison."),
                             })
            with right:
                st.line_chart(rdf.set_index("weeks out"), height=300)
            st.caption(f"Fitted out to {returns_fit.get('max_weeks')} weeks; past that "
                       "the curve is flat and the answer is that nobody knows.")
        with st.expander("The fitted artifact, field by field"):
            st.dataframe(pd.DataFrame(evidence.explain_fields(returns_fit))[
                             ["path", "value", "meaning"]],
                         use_container_width=True, hide_index=True, height=300,
                         column_config={"meaning":
                                        st.column_config.TextColumn(width="large")})
