"""Transactions — every roster change, one row each, with who asked for it.

The other pages follow a DECISION: a run screened candidates and priced a
move. This one follows the ROSTER: every add, drop, free-agent signing, waiver
claim (won, lost, cancelled or pending) and injured-reserve move, newest first,
whatever made it. Each row says when it happened, what instructed it -- the
chain of callers open at the moment of the write, e.g. "news pulse >
construction > ir unblock" -- and why, and links to the decision run and the
public decision-log entry where one exists.

Sleeper's own record is the spine, so a move nobody in the bot made (a
commissioner edit, a change by hand) still appears, flagged as such. Rows from
before the transaction journal existed are reconstructed from the decision log
and the run records and are labelled "reconstructed": an inferred path is not
presented as a recorded one.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from robo import decision_audit, evidence, news_audit, transactions, ui, ui_player_card

st.title("📜 Transactions")
ui.gate_banner(st)

col_s1, _ = st.columns([3, 1])
with col_s1:
    ui_player_card.render_player_search_bar(key="tx_player_search")
ui_player_card.check_query_params_player()


@st.cache_data(ttl=120, show_spinner="Reading Sleeper's transaction record…")
def _ledger():
    return transactions.ledger()


@st.cache_data(ttl=120, show_spinner=False)
def _run(fingerprint: str):
    return decision_audit.find(fingerprint)


if st.button("Refresh from Sleeper", help="The ledger is cached for two minutes."):
    _ledger.clear()
rows = _ledger()
if not rows:
    st.info("No transactions recorded for this roster yet.")
    st.stop()

# ------------------------------------------------------------------ filters
weeks = sorted({r["week"] for r in rows if r.get("week")}, reverse=True)
types = sorted({r["type"] for r in rows})
origins = sorted({r["origin"] for r in rows})
f = st.columns([1, 2, 2, 2])
week = f[0].selectbox("Week", ["All"] + weeks)
pick_types = f[1].multiselect("Type", types)
pick_origins = f[2].multiselect("Initiated by", origins,
                                help="The driver at the head of the path. "
                                     "'direct command' is a scheduled task or a "
                                     "hand-run module; the row's entry point says which.")
who = f[3].text_input("Player", placeholder="name contains…")

shown = [r for r in rows
         if (week == "All" or r.get("week") == week)
         and (not pick_types or r["type"] in pick_types)
         and (not pick_origins or r["origin"] in pick_origins)
         and (not who or who.lower() in " ".join(r["adds"] + r["drops"]).lower())]

m = st.columns(4)
m[0].metric("Transactions", len(shown))
m[1].metric("Adds", sum(len(r["add_ids"]) for r in shown
                        if r["status"] == "complete" and r["type"] != "ir activate"))
m[2].metric("Drops", sum(len(r["drop_ids"]) for r in shown
                         if r["status"] == "complete" and r["type"] != "ir park"))
m[3].metric("Not made by the bot", sum(r["origin"] == transactions.NOT_BOT for r in shown))

table = pd.DataFrame([{
    "When": news_audit.local_time(r["when"]) if r["when"] else "—",
    "Type": r["type"],
    "In": ", ".join(r["adds"]) or "—",
    "Out": ", ".join(r["drops"]) or "—",
    "Bid": f"${r['bid']}" if r.get("bid") is not None else "",
    "Status": r.get("status") or "",
    "Initiated by": r["initiated_by"],
    "Why": r.get("reason") or r.get("decision") or r.get("sleeper_note") or "",
    "Record": r["provenance"],
} for r in shown])

wanted = st.query_params.get("txn")
event = st.dataframe(table, use_container_width=True, hide_index=True,
                     on_select="rerun", selection_mode="single-row",
                     column_config={"Why": st.column_config.TextColumn(width="large")})
sel = getattr(getattr(event, "selection", None), "rows", None) or []
chosen = shown[sel[0]] if sel else next(
    (r for r in shown if wanted and r.get("transaction_id") == wanted), None)
st.caption("Select a row to trace it. 'reconstructed' rows predate the transaction "
           "journal: their path is inferred from the decision log and run records.")

if chosen is None:
    st.stop()

# ------------------------------------------------------------------ trace
r = chosen
st.subheader(f"{r['type']}: "
             + " / ".join(filter(None, [
                 ("+" + ", ".join(r["adds"])) if r["adds"] else "",
                 ("−" + ", ".join(r["drops"])) if r["drops"] else ""])))

# Plain text, not st.metric: a timestamp is too wide for the metric font and
# truncates to "Sep 23, 4:23:…" at any ordinary width.
c = st.columns(4)
c[0].markdown(f"**Made**  \n{news_audit.local_time(r['when']) if r['when'] else '—'}")
c[1].markdown(f"**Settled**  \n"
              f"{news_audit.local_time(r['settled_at']) if r.get('settled_at') else '—'}",
              help="For a waiver claim, when Sleeper processed it.")
c[2].markdown(f"**Status**  \n{r.get('status') or '—'}")
c[3].markdown(f"**Record**  \n{r['provenance']}")

st.markdown("##### Who instructed it")
if r["path"]:
    st.markdown(" → ".join(f"`{p}`" for p in r["path"]))
else:
    st.markdown(f"**{r['initiated_by']}**")
if r.get("entry"):
    st.caption(f"Process entry point: `{r['entry']}`")
if r.get("reason"):
    st.markdown(ui.money(r["reason"]))
if r.get("run_fingerprint"):
    run = _run(r["run_fingerprint"])
    trace = decision_audit.move_trace(run, (r["add_ids"] or [None])[0],
                                      (r["drop_ids"] or [None])[0]) if run else []
    if trace:
        st.markdown("##### How the run reached it")
        st.markdown("\n".join(f"{i}. **{s['step']}** — {ui.money(s['text'])}"
                              for i, s in enumerate(trace, 1)))
if r["origin"] == transactions.NOT_BOT:
    st.warning("No bot record explains this transaction: it was made on Sleeper by "
               "someone else (a commissioner edit or a change by hand).")

if r.get("decision"):
    st.markdown(f"##### Decision log entry #{r['decision_id']}")
    # Quoted, never paraphrased: this is the text the league reads.
    st.markdown(f"> {ui.money(r['decision'])}")
    if r.get("rationale"):
        st.markdown(f"> {ui.money(r['rationale'])}")
if r.get("sleeper_note"):
    st.markdown("##### Sleeper's note")
    st.info(ui.money(r["sleeper_note"]))

links = []
if r.get("run_fingerprint"):
    links.append(f"[Open the decision run front to back →](Moves?run={r['run_fingerprint']})")
for pid, label in zip(r["add_ids"] + r["drop_ids"], r["adds"] + r["drops"]):
    links.append(f"[{label} — player card](Transactions?player={pid})")
if links:
    st.markdown(" · ".join(links))

with st.expander("Raw records"):
    if r.get("journal"):
        st.markdown("**Journal row** (written by the Sleeper writer at send time)")
        st.json(r["journal"], expanded=False)
    if r.get("sleeper"):
        st.markdown("**Sleeper's transaction**")
        fields = evidence.explain_fields(r["sleeper"])
        st.dataframe(pd.DataFrame(fields)[["path", "value", "meaning"]],
                     use_container_width=True, hide_index=True)
        st.json(r["sleeper"], expanded=False)
