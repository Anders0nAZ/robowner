"""Frozen ordinary add/drop decisions, including exclusions and near-misses."""

from datetime import datetime

import pandas as pd
import streamlit as st

from robo import ui, waiver_audit

st.title("Ordinary moves")
ui.gate_banner(st)
st.caption("The ordered weekly pass: take the best free improvement first, then "
           "reprice waivers against that roster. Everything shown is frozen at decision time.")


@st.cache_data(ttl=10, show_spinner=False)
def load():
    return waiver_audit.events()


def player(row: dict | None) -> str:
    row = row or {}
    return str(row.get("name") or row.get("player_id") or "(open roster spot)")


def plan_rows(doc: dict) -> list[dict]:
    rows = []
    for p in doc.get("free_plans") or []:
        rows.append({"channel": "free now", "add": player(p.get("add")),
                     "drop": player(p.get("drop")), "gain": p.get("gain"),
                     "ceiling": p.get("ceiling"), "why": p.get("why")})
    for slate in doc.get("plans") or []:
        for claim in slate.get("claims") or []:
            # A slate is a LADDER -- Sleeper reaches them in bid order and the
            # first winner takes the slot -- so the rung and the bid are the
            # decision. Showing only the gain hid which claim we actually
            # expected to land.
            rows.append({"channel": "waiver claim", "add": player(claim.get("add")),
                         "drop": player(claim.get("drop") or slate.get("drop")),
                         "rung": claim.get("priority", claim.get("seq")),
                         "bid": claim.get("bid"),
                         "gain": claim.get("gain"), "ceiling": claim.get("ceiling"),
                         "why": claim.get("why")})
    rows.sort(key=lambda r: (r["channel"] != "free now", r.get("rung") or 0))
    return rows


docs = load()
if not docs:
    st.info("No ordinary move evaluation has been recorded since this audit was enabled.")
    st.stop()

labels = {d["fingerprint"]: datetime.fromtimestamp(float(d["at"])).astimezone().strftime(
    "%b %d, %I:%M %p") + f" · week {d.get('week')}" for d in docs}
chosen = st.selectbox("Evaluation", list(labels), format_func=labels.get)
doc = next(d for d in docs if d["fingerprint"] == chosen)
free_audit = doc.get("free_audit") or {}
claims_audit = doc.get("claims_audit") or {}

proposals = plan_rows(doc)
metrics = st.columns(4)
metrics[0].metric("Week", doc.get("week"))
metrics[1].metric("Moves that cleared", len(proposals))
metrics[2].metric("Transaction gate", "closed" if doc.get("gated") else "open")
metrics[3].metric("Valuation", ui.fmt_age(doc.get("valuation_computed")))
if doc.get("sequence_basis"):
    st.caption("Waiver baseline: " + str(doc["sequence_basis"]) + ".")
if doc.get("blackout") or doc.get("control_block"):
    st.warning(doc.get("blackout") or doc.get("control_block"))

st.subheader("What cleared")
if proposals:
    st.dataframe(pd.DataFrame(proposals), use_container_width=True, hide_index=True,
                 column_config={"gain": st.column_config.NumberColumn(format="%+.2f"),
                                "ceiling": st.column_config.NumberColumn(format="%.2f")})
else:
    st.info("No add/drop comparison cleared every control and value bar in this pass.")

if not free_audit and not claims_audit:
    st.info("This is a legacy event from before candidate-level auditing was enabled. "
            "The outcome is intact, but exclusions and near-misses were not recorded.")
else:
    st.subheader("Roster drop screen")
    checks = free_audit.get("drop_checks") or claims_audit.get("drop_checks") or []
    if checks:
        st.dataframe(pd.DataFrame([{
            "player": c.get("name") or c.get("player_id"), "pos": c.get("pos"),
            "eligible": c.get("eligible"), "drop price": c.get("drop_price"),
            "reason": c.get("reason"),
        } for c in checks]), use_container_width=True, hide_index=True)
    else:
        st.caption("The run stopped before a drop pool was needed.")

    control_checks = ((free_audit.get("control_checks") or [])
                      + (claims_audit.get("control_checks") or []))
    if control_checks:
        with st.expander("Roster-construction checks"):
            st.caption(
                "The coverage floor ORDERS the ladder, it does not close the "
                "door. A move is refused only if it makes a position worse; a "
                "roster already under a floor still evaluates everyone, and the "
                "candidates that refill the short position are ranked first. "
                "`priority 0` relieves or preserves every floor, `1` is merely "
                "not worse.")

            def counts(c: dict, key: str) -> str:
                got = (c.get("coverage") or {}).get(key) or {}
                return " ".join(f"{k}{v}" for k, v in got.items()) or "—"

            st.dataframe(pd.DataFrame([{
                "add": player(c.get("add")) if c.get("add") else c.get("add_id"),
                "drop": player(c.get("drop")) if c.get("drop") else c.get("drop_id"),
                "eligible": c.get("eligible"),
                "priority": c.get("coverage_priority"),
                "before": counts(c, "counts_before"),
                "after": counts(c, "counts"),
                "relieves": ", ".join((c.get("coverage") or {}).get("relieves") or []),
                "reason": c.get("reason"),
                "direct ROS gain": (c.get("direct_ros") or {}).get("gain"),
            } for c in control_checks]).drop_duplicates(),
                use_container_width=True, hide_index=True)

    st.subheader("Candidate board and near-misses")
    phase = st.radio("Phase", ["free now", "waivers"], horizontal=True)
    audit = free_audit if phase == "free now" else claims_audit
    thresholds = audit.get("thresholds") or {}
    if thresholds:
        floor = thresholds.get("coverage_floor")
        st.caption("Recorded bars: gain must beat "
                   f"{thresholds.get('noise_multiple')}× simulation error; starting gains "
                   f"must reach {thresholds.get('starting_gain')}; bench ceilings must reach "
                   f"{thresholds.get('bench_ceiling')}; drop price is capped at "
                   f"{thresholds.get('drop_floor')}"
                   + (f"; coverage floor {floor}." if floor else "."))
    options = audit.get("options") or []
    if options:
        st.dataframe(pd.DataFrame([{
            "add": player(o.get("add")), "drop": player(o.get("drop")),
            "gain": o.get("gain"), "error": o.get("se"),
            "ceiling": o.get("ceiling"), "starter": o.get("starter"),
            "priority": o.get("coverage_priority"),
            "noise margin": o.get("noise_margin"),
            "policy margin": o.get("policy_margin"), "verdict": o.get("verdict"),
        } for o in options]), use_container_width=True, hide_index=True,
            column_config={
                "gain": st.column_config.NumberColumn(format="%+.2f"),
                "error": st.column_config.NumberColumn(format="%.2f"),
                "ceiling": st.column_config.NumberColumn(format="%.2f"),
                "noise margin": st.column_config.NumberColumn(format="%+.2f"),
                "policy margin": st.column_config.NumberColumn(format="%+.2f"),
            })
    else:
        st.caption("No candidates reached the simulator in this phase.")

    # CLEARED BUT NEVER OFFERED. ROS_MAX_MUTATIONS caps how many SLATES get
    # built, and an open roster spot sorts first because it costs nothing to
    # fill -- so on a night with a spare slot every add-against-our-own-roster
    # comparison is priced, clears every bar, and is then dropped on the floor
    # without appearing anywhere. That is a defensible policy and an
    # indefensible silence: the page showed the winners and the near-misses and
    # left out the options that passed and were not offered.
    unslated = [o for o in options
                if o.get("drop", {}).get("player_id") is not None
                and not o.get("selected")
                and str(o.get("verdict", "")).startswith("cleared")]
    if unslated:
        with st.expander(
                f"Cleared against our own roster but not offered "
                f"({len(unslated)}) — mutation budget spent elsewhere"):
            st.caption(
                f"Every bar passed; no slate was available. `mutation_limit` is "
                f"{thresholds.get('mutation_limit')} for this mode, and an open "
                "roster spot takes the slot ahead of any swap because nobody is "
                "dropped to use it. These are the trades that were priced and "
                "then never put in front of Sleeper.")
            st.dataframe(pd.DataFrame([{
                "add": player(o.get("add")), "pos": (o.get("add") or {}).get("pos"),
                "drop": player(o.get("drop")),
                "gain": o.get("gain"), "error": o.get("se"),
                "ceiling": o.get("ceiling"), "starter": o.get("starter"),
            } for o in sorted(unslated, key=lambda x: -(x.get("gain") or 0))]),
                use_container_width=True, hide_index=True,
                column_config={
                    "gain": st.column_config.NumberColumn(format="%+.2f"),
                    "error": st.column_config.NumberColumn(format="%.2f"),
                    "ceiling": st.column_config.NumberColumn(format="%.2f"),
                })

with st.expander("Raw immutable evidence"):
    st.json({k: v for k, v in doc.items() if not k.startswith("_")}, expanded=False)
    st.caption(doc.get("_path"))
