"""Immutable, pulse-by-pulse audit of injury-triggered roster re-evaluations."""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from robo import expected, news_audit, newswatch, ui

st.title("News pulse")
ui.gate_banner(st)
st.caption("Every event-triggered re-evaluation is frozen here. This page reads the evidence; "
           "it cannot rebuild a value, change policy, or send a transaction.")


@st.cache_data(ttl=10, show_spinner=False)
def audit_rows() -> tuple[list[dict], dict, dict]:
    docs = news_audit.events()
    state = news_audit.state()
    current = expected.load().get("players") or {}
    fallback = {str(pid): {"name": r.get("name") or str(pid), "pos": r.get("pos"),
                           "team": r.get("team")} for pid, r in current.items()}
    return docs, state, fallback


docs, pulse_state, fallback = audit_rows()
last_event = docs[0] if docs else {}
metrics = st.columns(4)
metrics[0].metric("Last poll", ui.fmt_age(pulse_state.get("last_poll")))
metrics[1].metric("Last re-evaluation", ui.fmt_age(last_event.get("at")))
metrics[2].metric("Recorded cascades", len(docs))
metrics[3].metric("Latest result", news_audit.outcome(last_event) if last_event else "none")
if pulse_state.get("paused"):
    st.info("Watcher intentionally paused: " + str(pulse_state["paused"].get("reason") or "scheduled pause"))
if pulse_state.get("filtered"):
    labels = {
        "sleeper_timestamp_only": "Sleeper timestamp-only",
        "espn_timestamp_only": "ESPN timestamp-only",
        "pft_indirect_mention": "indirect PFT mention",
        "trending_without_corroboration": "uncorroborated trending",
        "postgame_projection": "completed-game projection cleanup",
        "postgame_inactive": "expired postgame inactive row",
        "espn_record_removed": "removed ESPN row",
    }
    st.caption("Latest poll filtered: " + ", ".join(
        f"{labels.get(k, k)} {v}" for k, v in pulse_state["filtered"].items() if v))
if pulse_state.get("source_errors"):
    st.error("Latest poll source failures: " + "; ".join(pulse_state["source_errors"]))
if pulse_state.get("pending_reviews"):
    st.caption(f"Advisory prose queue: {len(pulse_state['pending_reviews'])} player(s); "
               f"up to {newswatch.PULSE_REVIEW_BATCH} are reviewed per pulse.")

if not docs:
    st.info("No pulse has triggered a recorded re-evaluation yet.")
    st.stop()

st.subheader("Recent re-evaluations")
timeline = pd.DataFrame([{
    "when": news_audit.local_time(d.get("at")),
    "week": d.get("week"),
    "triggers": len(d.get("events") or []),
    "affected": len((d.get("action") or {}).get("event_deltas") or d.get("affected") or []),
    "candidates": len((d.get("action") or {}).get("candidate_checks")
                      or (d.get("action") or {}).get("rejections") or []),
    "proposals": len(news_audit.proposals(d)),
    "submitted": len(news_audit.submitted(d)),
    "result": news_audit.outcome(d),
    "fingerprint": d.get("fingerprint"),
} for d in docs])
st.dataframe(timeline, use_container_width=True, hide_index=True, height=min(300, 38 + 35 * len(timeline)))

labels = {
    d["fingerprint"]: f"{news_audit.local_time(d.get('at'))} · {news_audit.outcome(d)} · {d['fingerprint']}"
    for d in docs
}
chosen = st.selectbox("Open a re-evaluation", list(labels), format_func=labels.get)
doc = next(d for d in docs if d["fingerprint"] == chosen)
action = doc.get("action") or {}
index = news_audit.player_index(doc, fallback)

st.divider()
st.subheader("What happened")
for paragraph in news_audit.narrative(doc):
    st.write(paragraph)

st.subheader("1 · Trigger")


def change_value(field, value) -> str:
    if value is None:
        return "—"
    if field == "news_updated":
        try:
            return news_audit.local_time(float(value) / 1000)
        except Exception:
            pass
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


trigger_rows = []
for e in doc.get("events") or []:
    for c in e.get("changes") or [{"field": "event", "before": None, "after": None}]:
        trigger_rows.append({"player": e.get("name") or news_audit.label(e.get("player_id"), index),
                             "signal": c.get("field"),
                             "before": change_value(c.get("field"), c.get("before")),
                             "after": change_value(c.get("field"), c.get("after")),
                             "why it fired": "; ".join(e.get("reasons") or [])})
st.dataframe(pd.DataFrame(trigger_rows), use_container_width=True, hide_index=True)

timing = doc.get("timing") or {}
tc = st.columns(4)
tc[0].metric("Explicit timing", len(timing.get("deterministic") or []))
tc[1].metric("Advisory reviews", news_audit.model_review_count(timing))
tc[2].metric("Quarantined", len(timing.get("quarantined") or {}))
tc[3].metric("Old timing cleared", len(timing.get("cleared") or []))
st.caption((timing.get("summary") or "No timing pass recorded") +
           ". Deterministic dates may change availability. Model-read prose is visible context only.")
if timing.get("quarantined"):
    st.warning(" · ".join(f"{news_audit.label(pid, index)}: {why}"
                          for pid, why in timing["quarantined"].items()))
if timing.get("bounds"):
    st.dataframe(pd.DataFrame([{
        "player": news_audit.label(pid, index),
        "earliest return week": b.get("return_week_min"),
        "latest return week": b.get("return_week_max"),
        "out for season": bool(b.get("out_for_season")),
        "basis": b.get("return_basis"), "exact sentence": b.get("timing_sentence"),
    } for pid, b in timing["bounds"].items()]), use_container_width=True, hide_index=True)
if timing.get("advisory_reviews"):
    with st.expander("Show model-read advisory context"):
        st.dataframe(pd.DataFrame(timing["advisory_reviews"]), use_container_width=True,
                     hide_index=True)

st.subheader("2 · Room expansion and measured value")
deltas = action.get("event_deltas") or {}
provider = doc.get("provider_at_trigger") or {}
delta_rows = []
for pid, d in deltas.items():
    edge = d.get("causal_edge")
    relationship = ("trigger player" if str(edge or "").startswith("self:") else
                    f"successor to {news_audit.label(d.get('lead_id'), index)}"
                    if str(edge or "").startswith("successor-of:") else "same room / context")
    delta_rows.append({
        "player": d.get("name") or news_audit.label(pid, index),
        "pos": d.get("pos") or (index.get(pid) or {}).get("pos"),
        "team": d.get("team") or (index.get(pid) or {}).get("team"),
        "relationship": relationship,
        "ownership then": d.get("ownership", "not frozen in legacy record"),
        "acquirable": d.get("acquisition_candidate"),
        "Sleeper before": (provider.get(pid) or {}).get("points_before"),
        "Sleeper at trigger": (provider.get(pid) or {}).get("points_at_trigger"),
        "before ROS": d.get("pre_ros"), "after ROS": d.get("post_ros"),
        "change": d.get("delta_ros"), "complete series": bool(d.get("complete")),
    })
delta_rows.sort(key=lambda r: (not bool(r.get("acquirable")), -abs(float(r.get("change") or 0)), r["player"]))
st.dataframe(pd.DataFrame(delta_rows), use_container_width=True, hide_index=True,
             column_config={
                 "before ROS": st.column_config.NumberColumn(format="%.2f"),
                 "after ROS": st.column_config.NumberColumn(format="%.2f"),
                 "Sleeper before": st.column_config.NumberColumn(format="%.2f"),
                 "Sleeper at trigger": st.column_config.NumberColumn(format="%.2f"),
                 "change": st.column_config.NumberColumn(format="%+.2f"),
             })
if deltas:
    inspect_pid = st.selectbox("Inspect weekly ripple", list(deltas),
                               format_func=lambda pid: news_audit.label(pid, index))
    weekly = deltas[inspect_pid].get("by_week") or {}
    st.dataframe(pd.DataFrame([{"week": int(w), "modeled change": v}
                               for w, v in sorted(weekly.items(), key=lambda x: int(x[0]))]),
                 use_container_width=True, hide_index=True,
                 column_config={"modeled change": st.column_config.NumberColumn(format="%+.3f")})
    st.caption("A causal edge is either the trigger player himself or a fitted successor whose lead was a "
               "trigger. Same-room players without that edge may explain the rebuild, but cannot authorize an add.")

st.subheader("3 · Acquisition and drop gates")
candidates = action.get("candidate_checks") or action.get("rejections") or []
if candidates:
    cdf = pd.DataFrame([{
        "candidate": c.get("name") or news_audit.label(c.get("player_id"), index),
        "pos": c.get("pos") or (index.get(str(c.get("player_id"))) or {}).get("pos"),
        "channel": "waivers" if c.get("on_waivers") else "free agent",
        "event change": c.get("delta_ros"), "candidate ROS": c.get("candidate_ros", c.get("post_ros")),
        "stage reached": c.get("stage", "legacy"), "outcome": c.get("outcome", "rejected"),
        "reason": c.get("reason"),
    } for c in candidates])
    st.dataframe(cdf, use_container_width=True, hide_index=True,
                 column_config={"event change": st.column_config.NumberColumn(format="%+.2f"),
                                "candidate ROS": st.column_config.NumberColumn(format="%.2f")})
else:
    st.info("No affected free agent or waiver player entered the acquisition screen.")

drop_checks = action.get("drop_checks") or []
if drop_checks:
    with st.expander(f"Show all {len(drop_checks)} incumbent drop checks"):
        ddf = pd.DataFrame([{
            "for candidate": news_audit.label(r.get("candidate_id"), index),
            "incumbent": r.get("drop_name") or news_audit.label(r.get("drop_id"), index),
            "pos": r.get("drop_pos"), "ROS": r.get("drop_ros"),
            "eligible": bool(r.get("eligible")), "reason": r.get("reason"),
            "coverage counts": ", ".join(f"{p} {n}"
                                          for p, n in (r.get("coverage") or {}).get("counts", {}).items()),
            "weeks fillable": ", ".join(str(x.get("week")) for x in
                                        (r.get("coverage") or {}).get("weeks", [])
                                        if x.get("skill_lineup_fillable")),
        } for r in drop_checks])
        st.dataframe(ddf, use_container_width=True, hide_index=True,
                     column_config={"ROS": st.column_config.NumberColumn(format="%.2f")})
else:
    st.caption("No candidate passed far enough to build a drop pool, or this is a legacy event record.")

props = news_audit.proposals(doc)
if props:
    st.markdown("**Proposals that cleared every decision gate**")
    st.dataframe(pd.DataFrame([{
        "add": (p.get("add") or {}).get("name"), "add ROS": p.get("add_value"),
        "drop": (p.get("drop") or {}).get("name"), "drop ROS": p.get("drop_value"),
        "gain": p.get("gain"), "event change": p.get("event_delta"),
        "channel": "waivers" if p.get("on_waivers") else "free agent", "why": p.get("why")}
        for p in props]), use_container_width=True, hide_index=True)

claim_props = [c for slate in action.get("claim_proposals") or []
               for c in slate.get("claims") or []]
if claim_props:
    st.subheader("4 · Waiver market and bid")
    for claim in claim_props:
        quote = claim.get("bid_quote") or {}
        field = claim.get("opponent_field") or {}
        st.markdown(f"**{(claim.get('add') or {}).get('name')} — ${claim.get('bid', 0)}**")
        metrics = st.columns(4)
        metrics[0].metric("Paired roster gain",
                          f"{float(claim.get('bid_gain') or 0):+.2f}",
                          f"± {float(claim.get('bid_se') or 0):.2f}")
        metrics[1].metric("Win probability", f"{float(quote.get('p_win') or 0):.0%}")
        metrics[2].metric("Expected high",
                          (f"${float(quote['expected_highest']):.1f}"
                           if quote.get("expected_highest") is not None else "pooled fallback"))
        band = quote.get("near_optimal") or [claim.get("bid", 0), claim.get("bid", 0)]
        metrics[3].metric("Near-optimal band", f"${band[0]}–${band[1]}")
        st.caption(quote.get("reason") or "No bid explanation recorded")
        shadow = quote.get("shadow_price") or {}
        if shadow:
            st.caption(f"FAAB shadow price: {float(shadow.get('points_per_dollar') or 0):.2f} "
                       f"lineup points/$ · {shadow.get('status')}: {shadow.get('basis')}")
        if quote.get("curve"):
            qdf = pd.DataFrame(quote["curve"])
            focus = sorted(set([0, int(claim.get("bid") or 0),
                                int(quote.get("expected_highest_plus_one") or 0),
                                max(0, int(claim.get("bid") or 0) - 1),
                                int(claim.get("bid") or 0) + 1]))
            st.dataframe(qdf[qdf["bid"].isin(focus)], use_container_width=True,
                         hide_index=True)
        if field.get("available"):
            valid = field.get("validation") or {}
            if valid:
                st.caption("Historical holdout: demand Brier "
                           f"{valid.get('demand_brier')} vs {valid.get('pooled_demand_brier')} pooled; "
                           f"bid CRPS {valid.get('bid_crps')} vs {valid.get('pooled_bid_crps')} pooled. "
                           f"Runtime check: {'pass' if valid.get('passes') else 'fallback'}.")
            odf = pd.DataFrame([{
                "manager": o.get("manager"),
                "roster need": (o.get("need") or {}).get("need"),
                "modeled lineup gain": (o.get("need") or {}).get("gain"),
                "candidate starts": (o.get("need") or {}).get("starts"),
                "P(claim)": f"{float(o.get('p_claim') or 0):.0%}",
                "bid p50": o.get("bid_p50"), "bid p75": o.get("bid_p75"),
                "bid p90": o.get("bid_p90"), "FAAB left": o.get("faab_left"),
                "tie priority": o.get("waiver_position"),
                "manager samples": (o.get("evidence") or {}).get("manager_samples"),
                "manager bid samples": (o.get("evidence") or {}).get("manager_bid_samples"),
            } for o in sorted(field.get("opponents") or [],
                              key=lambda x: -float(x.get("p_claim") or 0))])
            st.dataframe(odf,
                         use_container_width=True, hide_index=True,
                         column_config={"modeled lineup gain":
                                        st.column_config.NumberColumn(format="%+.2f")})
        else:
            st.warning("Opponent model unavailable; pooled history used. "
                       + str(field.get("reason") or "No reason recorded"))

st.subheader("5 · Final state" if claim_props else "4 · Final state")
fc = st.columns(4)
fc[0].metric("Mode", "dry run" if doc.get("dry_run") else "live requested")
fc[1].metric("Source failures", len(doc.get("source_errors") or []))
fc[2].metric("Proposals", len(props))
fc[3].metric("Submissions", len(news_audit.submitted(doc)))
st.write(f"Result: **{news_audit.outcome(doc)}**. "
         f"Free-agent gate: {'closed' if action.get('free_gated') else 'open'}. "
         f"Waiver gate: {'closed' if action.get('claims_gated') else 'open'}. "
         f"The pulse itself had live authority: {'yes' if doc.get('submission_authorized') else 'no'}.")
st.caption(f"Capture: {action.get('capture', 'not recorded')} · Export: {action.get('export', 'not recorded')} · "
           f"Model: {action.get('model', 'not recorded')}")

with st.expander("Raw immutable evidence"):
    report_path = Path(doc.get("_report_path") or "")
    if report_path.is_file():
        st.code(report_path.read_text(encoding="utf-8"), language="text")
    st.json({k: v for k, v in doc.items() if not k.startswith("_")}, expanded=False)
    st.caption(doc.get("_path"))
