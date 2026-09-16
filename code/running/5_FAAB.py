"""Private audit of the FAAB pricing behind every ordinary waiver slate."""

from datetime import datetime

import pandas as pd
import streamlit as st

from robo import ui, waiver_audit, waiver_manager

st.title("FAAB")
ui.gate_banner(st)
st.caption("Frozen evaluations from the scheduled roster manager. Opponent forecasts are "
           "private and never enter the public decision log.")

@st.cache_data(ttl=20, show_spinner=False)
def live_portfolio():
    """Two authenticated Sleeper reads, and it settles anything that has left
    the pending queue -- so it runs on a timer, not on every widget click."""
    return waiver_manager.status()


try:
    live = live_portfolio()
    x = st.columns(4)
    x[0].metric("Pending now", len(live.get("pending") or []))
    x[1].metric("Bot-owned", len(live.get("owned") or []))
    x[2].metric("Foreign", len(live.get("foreign") or []))
    x[3].metric("Worst-case exposure",
                f"${live.get('worst_case_exposure', 0)}")
    if live.get("foreign"):
        st.error("Automation is blocked: at least one pending claim is not in the bot ownership ledger.")
    with st.expander("Live pending ownership and settlement state"):
        st.json({"groups": live.get("groups"), "owned": live.get("owned"),
                 "foreign": live.get("foreign"),
                 "settled_this_read": live.get("settled"),
                 "recent_outcomes": ((live.get("state") or {}).get("history") or [])[-20:]})
    with st.expander("Reconciliation journal — what was cancelled, sent, rolled back"):
        events = live.get("events") or []
        if not events:
            st.caption("Nothing has been submitted or cancelled yet.")
        else:
            st.dataframe(pd.DataFrame([{
                "at": datetime.fromtimestamp(float(e.get("at") or 0))
                        .astimezone().strftime("%b %d %I:%M:%S %p"),
                "event": e.get("kind"),
                "source": e.get("source"),
                "transaction": e.get("transaction_id"),
                "detail": e.get("reason") or e.get("error")
                          or (e.get("result") or {}).get("status") or "",
            } for e in events]), use_container_width=True, hide_index=True)
            st.json(events[0])
except Exception as e:
    st.warning(f"Live pending queue unavailable: {type(e).__name__}")


@st.cache_data(ttl=10, show_spinner=False)
def load():
    return waiver_audit.events()


docs = load()
if not docs:
    st.info("No ordinary waiver evaluation has been recorded since this audit was enabled.")
    st.stop()

labels = {d["fingerprint"]: datetime.fromtimestamp(float(d["at"])).astimezone().strftime(
    "%b %d, %I:%M %p") + f" · week {d.get('week')} · {sum(len(s.get('claims') or []) for s in d.get('plans') or [])} claims"
          for d in docs}
chosen = st.selectbox("Evaluation", list(labels), format_func=labels.get)
doc = next(d for d in docs if d["fingerprint"] == chosen)

m = st.columns(4)
m[0].metric("FAAB left", f"${doc.get('faab_left', 0)}")
m[1].metric("Claims", sum(len(s.get("claims") or []) for s in doc.get("plans") or []))
m[2].metric("Transaction gate", "closed" if doc.get("gated") else "open")
m[3].metric("Valuation", ui.fmt_age(doc.get("valuation_computed")))
if doc.get("blackout") or doc.get("control_block"):
    st.warning(doc.get("blackout") or doc.get("control_block"))
if doc.get("sequence_basis"):
    st.caption("Claims were priced from the " + str(doc["sequence_basis"]) + ".")
if not doc.get("plans"):
    st.info("No waiver claim cleared the roster controls and value bars in this pass.")

for slate in doc.get("plans") or []:
    st.subheader(f"{slate.get('group_id') or 'claim group'} · "
                 f"capacity {slate.get('capacity', 1)} · "
                 f"worst-case ${slate.get('exposure', 0)}")
    for claim in slate.get("claims") or []:
        add = claim.get("add") or {}
        q = claim.get("bid_quote") or {}
        field = claim.get("opponent_field") or {}
        st.markdown(f"**Priority {claim.get('priority', 0)} · {add.get('name')} · ${claim.get('bid', 0)}**")
        c = st.columns(4)
        c[0].metric("Paired roster gain", f"{float(claim.get('bid_gain') or 0):+.2f}",
                    f"± {float(claim.get('bid_se') or 0):.2f}")
        c[1].metric("P(win)", f"{float(q.get('p_win') or 0):.0%}")
        c[2].metric("Expected high", (f"${float(q['expected_highest']):.1f}"
                                      if q.get("expected_highest") is not None else "pooled"))
        band = q.get("near_optimal") or [claim.get("bid", 0), claim.get("bid", 0)]
        c[3].metric("Near-optimal", f"${band[0]}–${band[1]}")
        # WHY WE ARE NOT BIDDING NEAR THE EXPECTED HIGH. Those two metrics sit
        # next to each other -- "expected high $34" beside "near-optimal $1-$1"
        # -- and read as a contradiction until you know the bid is capped by
        # arithmetic, not by nerve: a dollar costs `lam` lineup points, so a
        # gain of G cannot justify more than G/lam dollars no matter who else
        # is bidding. Past that the claim is worth less than the budget it eats.
        lam = float((q.get("shadow_price") or {}).get("points_per_dollar") or 0)
        res = q.get("reservation_bid")
        if res is not None and lam > 0:
            hi = q.get("expected_highest")
            line = (f"Reservation price **\\${res}** — at {lam:.2f} lineup points "
                    f"per dollar, a gain of {float(q.get('gain') or 0):+.2f} cannot "
                    f"justify more. The curve stops there.")
            if hi is not None and float(hi) > res:
                line += (f" Beating the expected high of \\${float(hi):.0f} would "
                         f"need a gain of about {(float(hi) + 1) * lam:.1f} points, "
                         f"so this one is unreachable by construction rather than "
                         f"by choice.")
            st.caption(line)
        direct = claim.get("direct_ros") or {}
        if direct:
            d = st.columns(4)
            d[0].metric("Simple add ROS", f"{float(direct.get('add_ros') or 0):.1f}")
            d[1].metric("Simple drop ROS", f"{float(direct.get('drop_ros') or 0):.1f}")
            d[2].metric("Simple ROS gain", f"{float(direct.get('gain') or 0):+.1f}")
            d[3].metric("Engines", "agree" if direct.get("prefers_move") else "disagree")
            st.caption("The simple comparator subtracts the two players' weekly-model ROS totals. "
                       "The paired figure above remains authoritative because it measures the "
                       "change in Robowner's optimized lineups from the same weekly inputs.")
        st.caption(ui.money(q.get("reason") or "No quote narrative recorded"))
        shadow = q.get("shadow_price") or {}
        if shadow:
            st.caption(f"FAAB shadow price: {float(shadow.get('points_per_dollar') or 0):.2f} "
                       f"lineup points/$ · {shadow.get('status')}: {shadow.get('basis')}")

        if q.get("curve"):
            curve = pd.DataFrame(q["curve"])
            display_curve = curve.copy()
            if "p_win" in display_curve:
                display_curve["p_win"] = display_curve["p_win"].map(
                    lambda value: f"{float(value):.1%}")
            st.dataframe(display_curve, use_container_width=True, hide_index=True,
                         column_config={
                             "bid": st.column_config.NumberColumn("bid", format="$%d"),
                             "p_win": "P(win)",
                             "utility": st.column_config.NumberColumn("utility", format="%.3f"),
                         })
            if "bid" in curve and "utility" in curve:
                st.line_chart(curve.set_index("bid")[["utility"]], height=220)
        if field.get("available"):
            valid = field.get("validation") or {}
            if valid:
                st.caption("Historical holdout: demand Brier "
                           f"{valid.get('demand_brier')} vs {valid.get('pooled_demand_brier')} pooled; "
                           f"bid CRPS {valid.get('bid_crps')} vs {valid.get('pooled_bid_crps')} pooled. "
                           f"Runtime check: {'pass' if valid.get('passes') else 'fallback'}.")
            rows = []
            for o in field.get("opponents") or []:
                need, evidence = o.get("need") or {}, o.get("evidence") or {}
                rows.append({"manager": o.get("manager"), "need": need.get("need"),
                             "lineup gain": need.get("gain"), "starts": need.get("starts"),
                             "claim probability": f"{float(o.get('p_claim') or 0):.0%}",
                             "bid p50": o.get("bid_p50"), "bid p75": o.get("bid_p75"),
                             "bid p90": o.get("bid_p90"), "FAAB": o.get("faab_left"),
                             "priority": o.get("waiver_position"),
                             "history": evidence.get("manager_bid_samples")})
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.warning("Opponent model unavailable: " + str(field.get("reason") or "unknown"))

with st.expander("Raw immutable evidence"):
    st.json({k: v for k, v in doc.items() if not k.startswith("_")}, expanded=False)
    st.caption(doc.get("_path"))
