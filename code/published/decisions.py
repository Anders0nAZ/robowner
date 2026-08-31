"""Public decision log: every consequential Robowner action gets a record.

Records live in decision-log/decisions.json (append-only list); the static
site decision-log/index.html is regenerated on every append and is what the
league sees (published via GitHub Pages, same pattern as the lottery site).
"""

import html
import json
import subprocess
from datetime import datetime, timezone

from robo import ROOT, TEAM_NAME

LOG_DIR = ROOT / "decision-log"
DB = LOG_DIR / "decisions.json"

KINDS = ("keeper", "draft-slot", "draft-pick", "lineup", "waiver", "free-agent",
         "ir", "trade", "meta")


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
    LOG_DIR.mkdir(exist_ok=True)
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
        data_html = ""
        if e["data"]:
            rows = "".join(
                f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(json.dumps(v) if isinstance(v,(dict,list)) else str(v))}</td></tr>"
                for k, v in e["data"].items()
            )
            data_html = f"<details><summary>data</summary><table>{rows}</table></details>"
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
 main {{ max-width:720px; margin:0 auto; }}
 h1 {{ color:var(--acc); }} .sub {{ color:var(--dim); }}
 .card {{ background:var(--card); border-radius:10px; padding:1rem 1.2rem; margin:1rem 0; }}
 .card header {{ display:flex; gap:.8rem; font-size:.8rem; color:var(--dim); }}
 .kind {{ text-transform:uppercase; letter-spacing:.05em; color:var(--acc); }}
 .card.proposed .status {{ color:#ffc76b; }} .card.superseded {{ opacity:.55; }}
 .card h2 {{ margin:.3rem 0 .5rem; font-size:1.1rem; }}
 details {{ color:var(--dim); font-size:.85rem; }} td {{ padding:.1rem .5rem; vertical-align:top; }}
 table {{ border-collapse:collapse; }}
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
