"""Private audit of the FAAB pricing behind every ordinary waiver slate."""

from datetime import datetime

import pandas as pd
import streamlit as st

from robo import ui, waiver_audit

st.title("FAAB")
ui.gate_banner(st)
st.caption("Frozen evaluations from the scheduled roster manager. Opponent forecasts are "
           "private and never enter the public decision log.")


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
    st.subheader(f"Drop {(slate.get('drop') or {}).get('name')}")
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
        st.caption(q.get("reason") or "No quote narrative recorded")
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
