"""Public decision log: every consequential Robowner action gets a record.

Records live in decision-log/decisions.json (append-only list); the static
site decision-log/index.html is regenerated on every append and is what the
league sees (published via GitHub Pages, same pattern as the lottery site).

The store moved from the repo root to data/ on 31 Aug 2026. Read it through
decisions.DB rather than rebuilding the path: chat_responder had its own
hardcoded copy, unguarded, so the move would have taken every reply down with a
FileNotFoundError.
"""

import html
import json
import subprocess
from datetime import datetime, timezone

from robo import RAW, ROOT, TEAM_NAME

try:
    from robo.lineup import SLOTS
except Exception:
    SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF"]

LOG_DIR = ROOT / "decision-log"
# The record store behind index.html. Under data/ rather than beside the
# pages so the published repo's root is the four things a person opens.
DB = LOG_DIR / "data" / "decisions.json"

KINDS = ("keeper", "draft-slot", "draft-pick", "lineup", "waiver", "free-agent",
         "ir", "trade", "meta")

_PLAYERS_MAP = None


def _get_players() -> dict:
    global _PLAYERS_MAP
    if _PLAYERS_MAP is not None:
        return _PLAYERS_MAP
    try:
        cache = RAW / "players_nfl.json"
        if cache.exists():
            _PLAYERS_MAP = json.loads(cache.read_text(encoding="utf-8"))
            return _PLAYERS_MAP
    except Exception:
        pass
    try:
        from robo import sleeper_read
        _PLAYERS_MAP = sleeper_read.players()
        return _PLAYERS_MAP
    except Exception:
        _PLAYERS_MAP = {}
        return _PLAYERS_MAP


def _player_info(pid) -> dict:
    if pid in ("0", "", None, 0):
        return {"name": "(empty)", "pos": "", "team": "", "is_empty": True}
    pid_str = str(pid).strip()
    players = _get_players()
    p = players.get(pid_str)
    if not p:
        return {"name": pid_str, "pos": "", "team": "", "is_empty": False}

    first = p.get("first_name", "") or ""
    last = p.get("last_name", "") or ""
    full = p.get("full_name") or f"{first} {last}".strip() or pid_str
    pos = p.get("position") or ""
    team = p.get("team") or ""
    return {"name": full, "pos": pos, "team": team, "is_empty": False}


def _format_player_html(pid, bold_name: bool = False) -> str:
    info = _player_info(pid)
    if info["is_empty"]:
        return '<span class="dim">(empty)</span>'
    name_esc = html.escape(info["name"])
    if bold_name:
        name_esc = f"<strong>{name_esc}</strong>"
    pos = html.escape(info["pos"])
    team = html.escape(info["team"])
    if pos == "DEF" or pos == "DEFENSE":
        team_display = team or html.escape(str(pid))
        return f'{name_esc} <span class="team-tag">DEF &middot; {team_display}</span>'
    if pos and team:
        return f'{name_esc} <span class="team-tag">{pos} &middot; {team}</span>'
    if pos:
        return f'{name_esc} <span class="team-tag">{pos}</span>'
    return name_esc


def _format_lineup(data: dict) -> str:
    starters = data.get("starters") or []
    previous = data.get("previous") or []
    proj = data.get("projected")
    week = data.get("week")
    source = data.get("source")
    modelled = data.get("modelled")

    meta_parts = []
    if week is not None:
        meta_parts.append(f'<div><span class="meta-label">Week:</span> <strong>{html.escape(str(week))}</strong></div>')
    if proj is not None:
        meta_parts.append(f'<div><span class="meta-label">Projected:</span> <strong>{proj:.1f} pts</strong></div>')
    if modelled is not None:
        meta_parts.append(f'<div><span class="meta-label">Modelled:</span> <strong>{modelled} players</strong></div>')
    meta_html = f'<div class="data-meta-grid">{"".join(meta_parts)}</div>' if meta_parts else ""

    src_html = f'<div class="meta-source"><span class="meta-label">Source:</span> {html.escape(str(source))}</div>' if source else ""

    prev_set = set(previous)
    rows = []
    num_slots = max(len(starters), len(SLOTS))
    for i in range(num_slots):
        slot = SLOTS[i] if i < len(SLOTS) else f"SLOT {i+1}"
        s_id = starters[i] if i < len(starters) else None
        p_id = previous[i] if i < len(previous) else None

        s_info = _player_info(s_id)
        p_info = _player_info(p_id)

        changed = (s_id != p_id)
        tr_cls = ' class="changed"' if changed else ''

        if not changed:
            badge = '<span class="dim">&mdash;</span>'
        elif p_info["is_empty"]:
            badge = '<span class="badge-add">+ Filled empty slot</span>'
        elif s_info["is_empty"]:
            badge = '<span class="badge-cut">- Emptied slot</span>'
        elif s_id in prev_set:
            badge = '<span class="badge-swap">&harr; Shifted slot</span>'
        else:
            badge = '<span class="badge-swap">&harr; Swapped in</span>'

        s_html = _format_player_html(s_id, bold_name=changed)
        p_html = _format_player_html(p_id, bold_name=False)

        rows.append(f'<tr{tr_cls}><td><strong>{html.escape(slot)}</strong></td><td>{s_html}</td><td>{p_html}</td><td>{badge}</td></tr>')

    table = f"""<table class="lineup-table">
  <thead><tr><th>Slot</th><th>Starter</th><th>Previous</th><th>Change</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""
    return meta_html + src_html + table


def _format_free_agent(data: dict) -> str:
    add_id = data.get("add")
    drop_id = data.get("drop")
    mode = data.get("mode")
    week = data.get("week")
    gain = data.get("gain")
    ros = data.get("ros")
    reason = data.get("reason")

    add_html = _format_player_html(add_id, bold_name=True)
    drop_html = _format_player_html(drop_id, bold_name=False) if drop_id not in (None, "None", "") else '<span class="dim">(open roster spot)</span>'

    rows = [
        f'<tr><th>Added</th><td>{add_html}</td></tr>',
        f'<tr><th>Dropped</th><td>{drop_html}</td></tr>',
    ]
    if gain is not None:
        gain_val = float(gain) if isinstance(gain, (int, float, str)) and str(gain).replace(".", "", 1).replace("-", "", 1).isdigit() else None
        if gain_val is not None:
            gain_cls = "pos-gain" if gain_val >= 0 else "dim"
            gain_str = f"{gain_val:+.1f} pts"
        else:
            gain_cls = "pos-gain"
            gain_str = str(gain)
        rows.append(f'<tr><th>Projected Gain</th><td><span class="{gain_cls}">{html.escape(gain_str)}</span></td></tr>')
    if ros is not None:
        try:
            rows.append(f'<tr><th>Rest-of-Season Value</th><td>{float(ros):.1f} pts</td></tr>')
        except Exception:
            rows.append(f'<tr><th>Rest-of-Season Value</th><td>{html.escape(str(ros))}</td></tr>')
    if mode:
        rows.append(f'<tr><th>Evaluation Mode</th><td><code>{html.escape(str(mode))}</code></td></tr>')
    if week is not None:
        rows.append(f'<tr><th>Week</th><td>{html.escape(str(week))}</td></tr>')
    if reason:
        rows.append(f'<tr><th>Reason</th><td>{html.escape(str(reason))}</td></tr>')

    return f'<table class="data-table"><tbody>{"".join(rows)}</tbody></table>'


def _format_ir(data: dict) -> str:
    rows = []
    if "steps" in data and isinstance(data["steps"], list):
        for idx, s in enumerate(data["steps"], 1):
            action = s.get("action", f"step {idx}")
            details = []
            if s.get("activate"):
                acts = [_format_player_html(pid, bold_name=True) for pid in s["activate"]]
                details.append(f"Activated: {', '.join(acts)}")
            if s.get("reserve"):
                res = [_format_player_html(pid, bold_name=True) for pid in s["reserve"]]
                details.append(f"Reserved: {', '.join(res)}")
            if s.get("drop"):
                d_html = _format_player_html(s["drop"], bold_name=True)
                val = s.get("value")
                val_str = f" <span class=\"dim\">(hold value: {float(val):.1f})</span>" if val is not None else ""
                details.append(f"Dropped: {d_html}{val_str}")
            landed_badge = '<span class="badge-success">landed</span>' if s.get("landed") else ''
            desc = "; ".join(details) if details else html.escape(str(s))
            rows.append(f'<tr><th>Step {idx} ({html.escape(action)})</th><td>{desc} {landed_badge}</td></tr>')
        if "legal" in data:
            status_badge = '<span class="badge-success">Legal</span>' if data["legal"] else '<span class="badge-cut">Illegal</span>'
            rows.append(f'<tr><th>Roster Compliance</th><td>{status_badge}</td></tr>')
    else:
        res_ids = data.get("reserve") or []
        prev_ids = data.get("previous") or []
        res_names = [_format_player_html(pid, bold_name=True) for pid in res_ids]
        prev_names = [_format_player_html(pid, bold_name=False) for pid in prev_ids]

        res_str = ", ".join(res_names) if res_names else '<span class="dim">(none)</span>'
        prev_str = ", ".join(prev_names) if prev_names else '<span class="dim">(none)</span>'

        rows.append(f'<tr><th>Current Reserve (IR)</th><td>{res_str}</td></tr>')
        rows.append(f'<tr><th>Previous Reserve</th><td>{prev_str}</td></tr>')

    return f'<table class="data-table"><tbody>{"".join(rows)}</tbody></table>'


def _format_waiver(data: dict) -> str:
    claims = data.get("claims") or []
    week = data.get("week")

    meta = []
    if week is not None:
        meta.append(f'<div><span class="meta-label">Week:</span> <strong>{html.escape(str(week))}</strong></div>')
    meta.append(f'<div><span class="meta-label">Total Claims:</span> <strong>{len(claims)}</strong></div>')
    meta_html = f'<div class="data-meta-grid">{"".join(meta)}</div>'

    rows = []
    for idx, c in enumerate(claims, 1):
        add_html = _format_player_html(c.get("add"), bold_name=True)
        drop_html = _format_player_html(c.get("drop"), bold_name=False)
        bid = c.get("bid", 0)
        gain = c.get("gain")
        gain_str = f"{gain:+.1f}" if isinstance(gain, (int, float)) else str(gain or "")
        status = c.get("status")
        source = c.get("source") or ""

        status_badge = '<span class="badge-success">completed</span>' if status == "completed" else f'<span class="badge-failed">{html.escape(str(status))}</span>'
        row_cls = ' class="changed"' if status == "completed" else ''

        rows.append(f'<tr{row_cls}><td>#{idx}</td><td>{add_html}</td><td>{drop_html}</td><td>${bid}</td><td><span class="pos-gain">{html.escape(gain_str)}</span></td><td>{status_badge}</td><td><code>{html.escape(source)}</code></td></tr>')

    table = f"""<table class="claims-table">
  <thead><tr><th>#</th><th>Target Add</th><th>Drop</th><th>Bid</th><th>Gain</th><th>Status</th><th>Source</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>"""
    return meta_html + table


def _format_draft_pick(data: dict) -> str:
    row = data.get("board_row") or {}
    p_id = row.get("player_id")
    p_info = _player_info(p_id) if p_id else {}
    name = row.get("name") or p_info.get("name") or str(p_id or "")
    pos = row.get("pos") or p_info.get("pos") or ""
    team = row.get("team") or p_info.get("team") or ""
    bye = row.get("bye")
    vorp = row.get("vorp")
    val_rank = row.get("value_rank")
    pos_rank = row.get("pos_rank")
    tier = row.get("tier")
    blend_rank = row.get("blend_rank")
    ecr = row.get("ecr")
    proj_pts = row.get("proj_pts")
    blend_pts = row.get("blend_pts")
    expert_pts = row.get("expert_pts")
    adp_ffc = row.get("adp_ffc")
    adp_live = row.get("adp_live")
    adp_sd = row.get("adp_stdev")
    adp_sleeper = row.get("adp_sleeper_2qb")
    injury = row.get("injury_status")

    vorp_str = f"{vorp:+.1f} pts" if isinstance(vorp, (int, float)) else str(vorp or "—")
    proj_str = f"{proj_pts:.1f} pts" if isinstance(proj_pts, (int, float)) else str(proj_pts or "—")
    blend_str = f"{blend_pts:.1f}" if isinstance(blend_pts, (int, float)) else str(blend_pts or "—")
    expert_str = f"{expert_pts:.1f}" if isinstance(expert_pts, (int, float)) else str(expert_pts or "—")

    t_rows = [
        f'<tr><th>Player</th><td><strong>{html.escape(name)}</strong> <span class="team-tag">{html.escape(pos)} &middot; {html.escape(team)}</span> &middot; Bye: {bye or "&mdash;"}</td></tr>',
        f'<tr><th>Board Value</th><td>Value Rank #{val_rank or "&mdash;"} overall &middot; VORP: <strong class="pos-gain">{html.escape(vorp_str)}</strong> &middot; Pos Rank: {html.escape(str(pos))}{html.escape(str(pos_rank or ""))}</td></tr>',
        f'<tr><th>Consensus &amp; Tier</th><td>Tier {tier or "&mdash;"} &middot; Blend Rank: {blend_rank or "&mdash;"} &middot; ECR: #{ecr or "&mdash;"}</td></tr>',
        f'<tr><th>Projections</th><td><strong>{html.escape(proj_str)}</strong> (Blend: {html.escape(blend_str)} &middot; Expert: {html.escape(expert_str)})</td></tr>',
        f'<tr><th>Market ADP</th><td>FFC 2QB: {adp_ffc or "&mdash;"} &middot; Live: {adp_live or "&mdash;"} (&plusmn;{adp_sd or "&mdash;"}) &middot; Sleeper 2QB: {adp_sleeper or "&mdash;"}</td></tr>',
    ]
    if injury:
        t_rows.append(f'<tr><th>Injury Status</th><td><span class="badge-cut">{html.escape(str(injury))}</span></td></tr>')

    return f'<table class="data-table"><tbody>{"".join(t_rows)}</tbody></table>'


def _format_keeper(data: dict) -> str:
    rows = []
    if "keepers" in data and isinstance(data["keepers"], list):
        for k in data["keepers"]:
            p_html = _format_player_html(k.get("id"), bold_name=True) if k.get("id") else f"<strong>{html.escape(str(k.get('player', '')))}</strong>"
            round_cost = k.get("cost_round", "&mdash;")
            rows.append(f'<tr><th>Declared Keeper</th><td>{p_html} &mdash; Surrendered Pick: Round {html.escape(str(round_cost))}</td></tr>')
        if "supersedes" in data:
            rows.append(f'<tr><th>Supersedes</th><td>{html.escape(str(data["supersedes"]))}</td></tr>')
    else:
        for k, v in data.items():
            if isinstance(v, dict):
                p_info = _player_info(k)
                title = _format_player_html(k, bold_name=True) if not p_info["is_empty"] and p_info["name"] != k else html.escape(str(k).replace("_", " ").title())
                details = [f"{html.escape(str(sub_k).replace('_', ' '))}: <strong>{html.escape(str(sub_v))}</strong>" for sub_k, sub_v in v.items()]
                rows.append(f'<tr><th>{title}</th><td>{" &middot; ".join(details)}</td></tr>')
            else:
                rows.append(f'<tr><th>{html.escape(str(k).replace("_", " ").title())}</th><td>{html.escape(str(v))}</td></tr>')
    return f'<table class="data-table"><tbody>{"".join(rows)}</tbody></table>'


def _format_draft_slot(data: dict) -> str:
    rows = []
    for k, v in data.items():
        if k == "sim" and isinstance(v, dict):
            sim_str = " &middot; ".join(f"{html.escape(str(sub_k).upper())}: <strong>{float(sub_v):.1f} VORP</strong>" for sub_k, sub_v in v.items())
            rows.append(f'<tr><th>Simulation VORP</th><td>{sim_str}</td></tr>')
        elif k == "r1_downside_vorp" and isinstance(v, dict):
            r1_str = " vs ".join(f"{html.escape(str(sub_k).upper())}: <strong>{float(sub_v):.1f} VORP</strong>" for sub_k, sub_v in v.items())
            rows.append(f'<tr><th>Round 1 Downside</th><td>{r1_str}</td></tr>')
        elif k == "our_picks" and isinstance(v, list):
            rows.append(f'<tr><th>Our Picks</th><td>{html.escape(", ".join(map(str, v)))}</td></tr>')
        elif k == "keepers" and isinstance(v, list):
            rows.append(f'<tr><th>Keepers Declared</th><td>{html.escape(", ".join(map(str, v)))}</td></tr>')
        elif k in ("our_dead_rounds", "keeper_rounds_dead") and isinstance(v, list):
            rows.append(f'<tr><th>Forfeited Rounds</th><td>{html.escape("Rounds " + ", ".join(map(str, v)))}</td></tr>')
        else:
            rows.append(f'<tr><th>{html.escape(str(k).replace("_", " ").title())}</th><td>{html.escape(str(v))}</td></tr>')
    return f'<table class="data-table"><tbody>{"".join(rows)}</tbody></table>'


def _format_generic(data: dict) -> str:
    rows = []
    players = _get_players()
    for k, v in data.items():
        if isinstance(v, list):
            items = []
            for item in v:
                item_str = str(item)
                if item_str in players or (item_str.isdigit() and len(item_str) >= 2):
                    items.append(_format_player_html(item_str))
                else:
                    items.append(html.escape(item_str))
            v_html = ", ".join(items)
        elif isinstance(v, (int, str)) and (str(v) in players or (str(v).isdigit() and len(str(v)) >= 2)):
            v_html = _format_player_html(v)
        elif isinstance(v, dict):
            v_html = " &middot; ".join(f"{html.escape(str(sub_k))}: {html.escape(str(sub_v))}" for sub_k, sub_v in v.items())
        else:
            v_html = html.escape(str(v))
        rows.append(f'<tr><th>{html.escape(str(k).replace("_", " ").title())}</th><td>{v_html}</td></tr>')
    return f'<table class="data-table"><tbody>{"".join(rows)}</tbody></table>'


def format_data_html(kind: str, data: dict) -> str:
    """Render human-friendly, structured HTML for a decision's data payload."""
    if not data:
        return ""
    if kind == "lineup" and "starters" in data:
        body = _format_lineup(data)
    elif kind == "free-agent" and ("add" in data or "drop" in data):
        body = _format_free_agent(data)
    elif kind == "ir" and ("reserve" in data or "steps" in data):
        body = _format_ir(data)
    elif kind == "waiver" and "claims" in data:
        body = _format_waiver(data)
    elif kind == "draft-pick" and "board_row" in data:
        body = _format_draft_pick(data)
    elif kind == "keeper":
        body = _format_keeper(data)
    elif kind == "draft-slot":
        body = _format_draft_slot(data)
    else:
        body = _format_generic(data)
    return f'<details><summary>data</summary><div class="data-box">{body}</div></details>'


def _load() -> list[dict]:
    if DB.exists():
        return json.loads(DB.read_text(encoding="utf-8"))
    return []


def record(kind: str, title: str, decision: str, rationale: str,
           status: str = "final", data: dict | None = None) -> dict:
    assert kind in KINDS, f"unknown kind {kind}"
    entries = _load()
    entry = {
        "id": len(entries) + 1,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "title": title,
        "decision": decision,
        "rationale": rationale,
        "status": status,  # proposed | final | superseded
        "data": data or {},
    }
    entries.append(entry)
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    render()
    try:
        from robo import devlog
        devlog.render()
    except Exception:
        pass
    publish(f"decision #{entry['id']}: {title}")
    return entry


def publish(message: str) -> bool:
    """Commit + push the decision-log repo (best-effort; site is GitHub Pages)."""
    if not (LOG_DIR / ".git").exists():
        return False
    try:
        subprocess.run(["git", "add", "-A"], cwd=LOG_DIR, check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=LOG_DIR)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=LOG_DIR,
                           check=True, capture_output=True)

        # Take anything that landed on the remote, EVEN WHEN WE HAVE NOTHING TO
        # SEND. Editing a file through GitHub's web UI puts a commit there that
        # we do not have; without this the local copy drifts quietly behind,
        # every later automated push is rejected with "fetch first", and the
        # site stops updating while the refresh log still reports success. That
        # happened on 31 Aug 2026 after a README edit. Syncing before the
        # nothing-to-do exit also means the next regeneration starts from what
        # is actually published rather than from a stale base.
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=LOG_DIR,
                       capture_output=True, timeout=60)
        rb = subprocess.run(["git", "rebase", "-q", "origin/main"], cwd=LOG_DIR,
                            capture_output=True, timeout=60)
        if rb.returncode != 0:
            # A real conflict: someone edited by hand the same file we generate.
            # Abort rather than guess whose version wins -- a half-finished
            # rebase would wedge every future publish, which is far worse than
            # one skipped push.
            subprocess.run(["git", "rebase", "--abort"], cwd=LOG_DIR,
                           capture_output=True)
            print("decision log: remote and local both changed the same file; "
                  "rebase aborted, nothing pushed. Resolve by hand in "
                  "decision-log/ -- generated files should take the LOCAL copy, "
                  "README.md the remote one.")
            return False

        push = subprocess.run(["git", "push"], cwd=LOG_DIR, capture_output=True, timeout=60)
        if push.returncode != 0:
            print(f"decision log push failed (will retry next publish): "
                  f"{push.stderr.decode(errors='replace').strip()[:200]}")
            return False
        return True
    except Exception as e:
        print(f"decision log publish error: {e}")
        return False


def render() -> None:
    entries = _load()
    cards = []
    for e in reversed(entries):
        data_html = format_data_html(e["kind"], e.get("data") or {})
        cards.append(f"""
  <article class="card {e['status']}">
    <header><span class="kind">{e['kind']}</span>
      <span class="status">{e['status']}</span>
      <time>{e['ts']}</time></header>
    <h2>#{e['id']} — {html.escape(e['title'])}</h2>
    <p class="decision"><strong>Decision:</strong> {html.escape(e['decision'])}</p>
    <p class="rationale"><strong>Why:</strong> {html.escape(e['rationale'])}</p>
    {data_html}
  </article>""")
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Robowner Decision Log — RURFFL</title>
<style>
 :root {{ --bg:#0f1420; --card:#1a2233; --ink:#e8ecf5; --dim:#93a0b8; --acc:#5aa9ff; }}
 body {{ background:var(--bg); color:var(--ink); font:16px/1.5 system-ui,sans-serif; margin:0; padding:1rem; }}
 main {{ max-width:760px; margin:0 auto; }}
 h1 {{ color:var(--acc); }} .sub {{ color:var(--dim); }}
 .card {{ background:var(--card); border-radius:10px; padding:1rem 1.2rem; margin:1rem 0; }}
 .card header {{ display:flex; gap:.8rem; font-size:.8rem; color:var(--dim); }}
 .kind {{ text-transform:uppercase; letter-spacing:.05em; color:var(--acc); }}
 .card.proposed .status {{ color:#ffc76b; }} .card.superseded {{ opacity:.55; }}
 .card h2 {{ margin:.3rem 0 .5rem; font-size:1.1rem; }}
 
 details {{ color:var(--ink); font-size:.88rem; margin-top:.6rem; }}
 summary {{ cursor:pointer; color:var(--acc); font-weight:500; font-size:.82rem; user-select:none; }}
 summary:hover {{ text-decoration:underline; }}
 
 .data-box {{ background:#121826; border:1px solid #243048; border-radius:6px; padding:.7rem .9rem; margin-top:.5rem; overflow-x:auto; }}
 .data-meta-grid {{ display:flex; flex-wrap:wrap; gap:1.2rem; margin-bottom:.5rem; font-size:.82rem; color:var(--dim); }}
 .data-meta-grid strong {{ color:var(--ink); }}
 .meta-label {{ color:var(--dim); }}
 .meta-source {{ font-size:.78rem; color:var(--dim); margin-bottom:.6rem; word-break:break-all; line-height:1.4; }}
 
 .data-table, .lineup-table, .claims-table {{ width:100%; border-collapse:collapse; font-size:.84rem; margin-top:.4rem; }}
 .data-table th, .lineup-table th, .claims-table th {{ text-align:left; padding:.35rem .5rem; color:var(--dim); border-bottom:1px solid #2a3750; font-weight:600; font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; }}
 .data-table td, .lineup-table td, .claims-table td {{ padding:.35rem .5rem; border-bottom:1px solid #1a2336; vertical-align:middle; }}
 .data-table th {{ width:28%; white-space:nowrap; vertical-align:top; }}
 
 .lineup-table tr.changed {{ background:rgba(90, 169, 255, 0.08); }}
 .lineup-table tr.changed td:first-child {{ border-left:3px solid var(--acc); padding-left:.4rem; }}
 
 .claims-table tr.changed {{ background:rgba(74, 222, 128, 0.08); }}
 .claims-table tr.changed td:first-child {{ border-left:3px solid #4ade80; padding-left:.4rem; }}
 
 .team-tag {{ display:inline-block; font-size:.75rem; color:var(--dim); padding:0 .3rem; background:#1c263a; border-radius:3px; margin-left:.25rem; font-weight:normal; }}
 
 .badge-swap {{ display:inline-block; font-size:.72rem; padding:.1rem .4rem; border-radius:4px; background:rgba(90,169,255,.18); color:var(--acc); font-weight:500; white-space:nowrap; }}
 .badge-add {{ display:inline-block; font-size:.72rem; padding:.1rem .4rem; border-radius:4px; background:rgba(74,222,128,.18); color:#4ade80; font-weight:500; white-space:nowrap; }}
 .badge-cut {{ display:inline-block; font-size:.72rem; padding:.1rem .4rem; border-radius:4px; background:rgba(248,113,113,.18); color:#f87171; font-weight:500; white-space:nowrap; }}
 .badge-success {{ display:inline-block; font-size:.72rem; padding:.1rem .4rem; border-radius:4px; background:rgba(74,222,128,.18); color:#4ade80; white-space:nowrap; }}
 .badge-failed {{ display:inline-block; font-size:.72rem; padding:.1rem .4rem; border-radius:4px; background:rgba(147,160,184,.15); color:var(--dim); white-space:nowrap; }}
 
 .pos-gain {{ color:#4ade80; font-weight:600; }}
 .dim {{ color:var(--dim); }}
 code {{ font-family:monospace; font-size:.82rem; background:#1c263a; padding:1px 4px; border-radius:3px; color:var(--acc); }}
 
 @media (max-width: 600px) {{
   .lineup-table, .claims-table {{ display:block; overflow-x:auto; }}
   .data-table th {{ width:35%; }}
 }}
</style></head><body><main>
<h1>🤖 Robowner Decision Log</h1>
<p class="sub">Every consequential decision by the RURFFL AI owner, with reasoning.
Franchise: {TEAM_NAME} (inherited 2026). Newest first.
See also the <a href="changelog.html" style="color:var(--acc)">dev log</a> — how this thing is being built —
<a href="status.html" style="color:var(--acc)">status</a>, whether it is currently working,
and the <a href="https://github.com/Anders0nAZ/robowner/tree/main/code" style="color:var(--acc)">source</a>,
every line of Python that runs it.</p>
{''.join(cards)}
</main></body></html>"""
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / "index.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    render()
    print(f"rendered {LOG_DIR / 'index.html'} with {len(_load())} entries")
