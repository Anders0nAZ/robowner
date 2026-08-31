"""Roboner admin GUI — the tunable settings, with what each one does.

Local only. Run it with AdminGUI.bat, or:

    streamlit run admin_gui.py --server.port 8502

WHAT THIS TUNES: policy. Weights, thresholds, rates, caps -- the stated
assumptions the model runs on. Every field is a named constant from
robo/settings.py's registry.

WHAT IT WILL NEVER TUNE: players. There is no field here for boosting, avoiding,
targeting or excluding a named player, and there must never be one. A
data/watchlist.json doing exactly that was built and deleted on the same day
(28 Aug 2026), because the moment a human supplies the names the bot is a tool
executing someone else's draft and the whole premise is gone. Where a signal is
weak the honest answer is to lower the value and widen the uncertainty, not to
open a channel for a person to decide instead.

Nothing here is published. The decision log is the bot's decisions; these are the
operator's tuning, and publishing them would hand eleven opponents our weights.
"""

import json
import time

import streamlit as st

from robo import DATA, settings as S

st.set_page_config(page_title="Roboner admin", page_icon="🎛", layout="wide")


# --------------------------------------------------------------------------
# state the page needs

@st.cache_data(ttl=60, show_spinner=False)
def draft_state() -> dict:
    """Reuses the status page's collector rather than re-fetching Sleeper."""
    try:
        from robo import status
        return status.draft()
    except Exception as e:
        return {"state": "unknown", "error": str(e)[:200]}


@st.cache_data(ttl=300, show_spinner=False)
def ollama_models() -> list[dict]:
    """Installed tags, and whether each bakes a context window.

    This project's most expensive gotcha: a tag with no num_ctx inherits the
    machine-wide 32k and then SILENTLY drops the oldest tokens -- which is the
    system prompt. No error, just a bot that forgets who it is.
    """
    import requests
    out = []
    try:
        tags = requests.get("http://localhost:11434/api/tags", timeout=8).json()
    except Exception:
        return out
    for m in tags.get("models", []):
        name = m.get("name", "")
        ctx = None
        try:
            info = requests.post("http://localhost:11434/api/show",
                                 json={"model": name}, timeout=8).json()
            for line in (info.get("parameters") or "").splitlines():
                if line.strip().startswith("num_ctx"):
                    ctx = int(line.split()[-1])
        except Exception:
            pass
        out.append({"name": name, "num_ctx": ctx})
    return sorted(out, key=lambda d: d["name"])


@st.cache_data(show_spinner="Reading settings from the code…")
def checkpoint_ready() -> int:
    return S.ensure_defaults()


def history(limit=25) -> list[dict]:
    rows = []
    try:
        for line in S.HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows[-limit:][::-1]


# --------------------------------------------------------------------------
# widgets

def _num(spec, value, key, disabled):
    lo, hi = spec.bounds or (None, None)
    if spec.type is int:
        return st.number_input(spec.label, value=int(value), min_value=lo, max_value=hi,
                               step=1, key=key, disabled=disabled,
                               label_visibility="collapsed")
    step = 0.01 if (hi is None or hi <= 3) else 0.1
    return st.number_input(spec.label, value=float(value), min_value=float(lo) if lo is not None else None,
                           max_value=float(hi) if hi is not None else None,
                           step=step, format="%.3f", key=key, disabled=disabled,
                           label_visibility="collapsed")


def _dict_widget(spec, value, key, disabled):
    """Numeric dicts get one bounded input per key; anything else gets JSON.

    Keys are fixed -- you can change what a position is worth, not invent a new
    position -- which is also what keeps this from becoming a free-text field.
    """
    value = dict(value)
    if value and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                     for v in value.values()):
        lo, hi = spec.bounds or (None, None)
        out = {}
        cols = st.columns(min(len(value), 6))
        for i, (k, v) in enumerate(value.items()):
            with cols[i % len(cols)]:
                is_int = isinstance(v, int)
                out[k] = st.number_input(
                    str(k), value=v if not is_int else int(v),
                    min_value=(int(lo) if is_int else float(lo)) if lo is not None else None,
                    max_value=(int(hi) if is_int else float(hi)) if hi is not None else None,
                    step=1 if is_int else 0.01,
                    format=None if is_int else "%.3f",
                    key=f"{key}.{k}", disabled=disabled)
        return out
    txt = st.text_area(spec.label, value=json.dumps(value, indent=1),
                       key=key, disabled=disabled, height=140,
                       label_visibility="collapsed")
    try:
        return json.loads(txt)
    except ValueError:
        st.caption("⚠ not valid JSON — this field will be skipped on save")
        return value


def _tuple_widget(spec, value, key, disabled):
    txt = st.text_area(spec.label, value="\n".join(str(v) for v in value),
                       key=key, disabled=disabled, height=110,
                       label_visibility="collapsed",
                       help="One per line.")
    return [ln.strip() for ln in txt.splitlines() if ln.strip()]


def _model_widget(spec, value, key, disabled):
    models = ollama_models()
    if not models:
        st.caption("Ollama is not answering — showing the current value only.")
        return st.text_input(spec.label, value=value, key=key, disabled=disabled,
                             label_visibility="collapsed")
    names = [m["name"] for m in models]
    if value not in names:
        names = [value] + names
    idx = names.index(value)
    picked = st.selectbox(spec.label, names, index=idx, key=key, disabled=disabled,
                          label_visibility="collapsed")
    ctx = next((m["num_ctx"] for m in models if m["name"] == picked), None)
    if ctx:
        st.caption(f"✅ bakes num_ctx {ctx:,} — safe")
    else:
        st.caption("🛑 **no num_ctx baked into this tag.** It will inherit the "
                   "machine-wide 32k limit, and Ollama will then silently drop "
                   "the oldest tokens — which is the system prompt.")
    return picked


def render_widget(spec, value, disabled):
    key = f"w::{spec.key}"
    if spec.name == "MODEL":
        return _model_widget(spec, value, key, disabled)
    if spec.choices:
        opts = list(spec.choices)
        if value not in opts:
            opts = [value] + opts
        return st.selectbox(spec.label, opts, index=opts.index(value), key=key,
                            disabled=disabled, label_visibility="collapsed")
    if spec.type is bool:
        return st.checkbox(spec.label, value=bool(value), key=key,
                           disabled=disabled, label_visibility="collapsed")
    if spec.type is dict:
        return _dict_widget(spec, value, key, disabled)
    if spec.type in (tuple, set, frozenset, list):
        # All of these edit as one-per-line text and come back as a list;
        # settings.apply puts the original container kind back afterwards.
        return _tuple_widget(spec, value, key, disabled)
    if spec.type in (int, float):
        return _num(spec, value, key, disabled)
    return st.text_input(spec.label, value=str(value), key=key, disabled=disabled,
                         label_visibility="collapsed")


# --------------------------------------------------------------------------
# page

checkpoint_ready()
draft = draft_state()
drafting = draft.get("state") == "drafting"

st.title("🎛 Roboner admin")
st.caption("Tunable settings for the RURFFL AI owner. Local only — nothing here "
           "is published, and none of it is visible to the league.")

with st.sidebar:
    st.subheader("State")
    st.metric("Draft", draft.get("state", "unknown"))
    over = S.load()
    st.metric("Changed from defaults", len(over))
    pend = S.pending_restart()
    if pend:
        st.warning(f"{len(pend)} setting(s) changed. These are read once at "
                   f"startup, so they do nothing until the process restarts.")
        if st.button("Restart the responder now", use_container_width=True):
            with st.spinner("Restarting…"):
                from robo import refresh
                refresh.restart_responder()
            st.success("Responder bounced.")
    st.divider()
    st.subheader("Revert")
    st.caption("The checkpoint was captured from the code the first time this "
               "ran. It is never overwritten.")
    if st.button("Revert everything to defaults", type="secondary",
                 use_container_width=True, disabled=not over):
        n = S.revert_all()
        st.success(f"Reverted {n} setting(s).")
        st.rerun()
    st.divider()
    st.caption("**This tunes policy, never players.** There is no field here for "
               "boosting, avoiding or targeting a named player, and there must "
               "never be one — that would make the bot a tool executing "
               "someone else's draft.")

labels = [t for _, t, _ in S.SECTIONS]
tabs = st.tabs(labels)

for tab, (section, title, blurb) in zip(tabs, S.SECTIONS):
    with tab:
        specs = S.by_section(section)
        if not specs:
            st.info(f"**{title} — not built yet.** {blurb} Reserved here so the "
                    f"section is visibly pending rather than missing.")
            if section == S.LINEUP:
                st.caption("`robo/lineup.py` exists (the weekly optimiser) but "
                           "has no tunables wired up and no GUI yet.")
            else:
                st.caption("Roster management — adds, drops, IR moves, waiver "
                           "budget — has not been built.")
            continue

        locked = drafting and section in (S.DRAFT_PREP, S.LIVE_DRAFT)
        st.caption(blurb)
        if locked:
            st.error("**Locked: the draft is live.** These are read at agent "
                     "startup, and the draft guard restarts the agent "
                     "automatically if it dies — so a change made now would sit "
                     "inert and then apply mid-draft without warning. Responder "
                     "settings are still editable.")

        with st.form(key=f"form::{section}"):
            proposed = {}
            confirms = {}
            for spec in specs:
                cur = S.current(spec)
                default = S.defaults().get(spec.key)
                changed = S.is_overridden(spec)

                head = f"**{spec.label}**"
                if spec.danger:
                    head += "  ⚠️ *structural*"
                if changed:
                    head += "  🔵 *changed*"
                st.markdown(head)
                st.caption(spec.does)

                left, right = st.columns([2, 3])
                with left:
                    proposed[spec.key] = render_widget(spec, cur, locked)
                    if spec.unit:
                        st.caption(spec.unit)
                    if changed:
                        st.caption(f"default was `{default}`")
                with right:
                    st.caption(f"**If you change it:** {spec.implication}")
                    if spec.restart:
                        st.caption("↻ takes effect on next restart")
                    if spec.danger and not locked:
                        confirms[spec.key] = st.checkbox(
                            "I understand this describes the league, not a preference",
                            key=f"ok::{spec.key}")
                st.divider()

            submitted = st.form_submit_button(
                f"Save {title.lower()}", type="primary", disabled=locked)

        if submitted and not locked:
            saved, skipped, failed = [], [], []
            for spec in specs:
                new = proposed[spec.key]
                ok, why = S.validate(spec, new)
                if not ok:
                    failed.append(f"{spec.name}: {why}")
                    continue
                if S.same_as_current(spec, new):
                    continue                       # untouched field
                if spec.danger and not confirms.get(spec.key):
                    skipped.append(spec.name)
                    continue
                ok, why = S.save(spec.key, new, note="admin gui")
                if ok:
                    saved.append(spec.name)
                else:
                    failed.append(f"{spec.name}: {why}")
            if saved:
                st.success("Saved: " + ", ".join(saved))
            if skipped:
                st.warning("Not saved — tick the confirm box for structural "
                           "settings: " + ", ".join(skipped))
            if failed:
                st.error("Rejected: " + "; ".join(failed))
            if saved:
                st.rerun()

with st.expander("Recent changes"):
    rows = history()
    if not rows:
        st.caption("Nothing changed yet.")
    for r in rows:
        st.text(f"{r.get('human','')}  {r.get('key','')}\n"
                f"    {str(r.get('old'))[:60]}  ->  {str(r.get('new'))[:60]}")
