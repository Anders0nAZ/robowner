"""Public dev log — what the bot can do, published with the decision log.

Renders data/changelog.json to decision-log/changelog.html so the league can
follow the build. It used to render the git log directly, which made the page a
lab notebook: every abandoned approach, every fix to a fix, in the engineer's
voice and at the engineer's length. Commits are still the engineering record and
still say what was wrong before; this page says what the bot does now.

WHAT BELONGS HERE: a capability landing or visibly changing. Two sentences at
most, describing the FINAL state -- not the path to it, not the three attempts
that missed. If an entry says "we tried X, then rolled it back, then rebuilt
it", it should just say what X does today. Leave out docs, config, plumbing,
refactors and anything that names private data (the quarantined archive, the
real-name map, API keys). If a league member would not find it interesting, it
does not go here.

Regenerated (and pushed with the rest of the decision-log site) by the daily
refresh and on demand: python -m robo.devlog
"""

import html
import json
from datetime import datetime

from robo import DATA, ROOT

OUT = ROOT / "decision-log" / "changelog.html"
# new = a capability that did not exist; enhancement = an existing one made
# better; fix = something that was broken for the league and is not now.
LABEL = {"new": "new feature", "enhancement": "enhancement", "fix": "bug fix"}
SRC = DATA / "changelog.json"


def entries() -> list[dict]:
    """Curated entries, newest first, grouped by day."""
    rows = json.loads(SRC.read_text(encoding="utf-8"))["entries"]
    for r in rows:
        datetime.strptime(r["date"], "%Y-%m-%d")  # fail loudly on a bad date
    return sorted(rows, key=lambda r: r["date"], reverse=True)


def render() -> None:
    rows = entries()
    days: dict[str, list] = {}
    for e in rows:
        days.setdefault(e["date"], []).append(e)
    n_new = sum(1 for e in rows if e.get("kind", "new") == "new")
    n_enh = sum(1 for e in rows if e.get("kind") == "enhancement")
    n_fix = sum(1 for e in rows if e.get("kind") == "fix")
    sections = []
    for day in sorted(days, reverse=True):
        items = "".join(f"""
    <article class="card">
      <span class="kind k-{e.get('kind', 'new')}">{LABEL.get(e.get('kind'), 'new')}</span>
      <h2>{html.escape(e['title'])}</h2>
      <p>{html.escape(e['text'])}</p>
    </article>""" for e in days[day])
        pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%A, %B %d %Y")
        sections.append(f"<h3 class='day'>{pretty}</h3>{items}")
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roboner Dev Log — RURFFL</title>
<style>
 :root {{ --bg:#0f1420; --card:#1a2233; --ink:#e8ecf5; --dim:#93a0b8; --acc:#5aa9ff; }}
 body {{ background:var(--bg); color:var(--ink); font:16px/1.6 system-ui,sans-serif; margin:0; padding:1rem; }}
 main {{ max-width:680px; margin:0 auto; }}
 h1 {{ color:var(--acc); margin-bottom:.2rem; }} .sub {{ color:var(--dim); margin-top:0; }}
 a {{ color:var(--acc); }}
 .day {{ color:var(--dim); font-size:.9rem; letter-spacing:.04em; text-transform:uppercase;
         border-bottom:1px solid #2a3550; padding-bottom:.4rem; margin-top:2.2rem; }}
 .card {{ background:var(--card); border-radius:10px; padding:1rem 1.2rem; margin:.7rem 0; }}
 .card h2 {{ margin:.35rem 0 .4rem; font-size:1.05rem; color:var(--ink); }}
 .kind {{ display:inline-block; font-size:.66rem; letter-spacing:.08em; text-transform:uppercase;
          padding:.15rem .5rem; border-radius:99px; font-weight:600; }}
 .k-new {{ background:#12351f; color:#5ad48a; }}
 .k-enhancement {{ background:#13293f; color:#5aa9ff; }}
 .k-fix {{ background:#3a2412; color:#f0a35a; }}
 .card p {{ margin:0; color:var(--dim); }}
</style></head><body><main>
<h1>🛠️ Roboner Dev Log</h1>
<p class="sub">What the RURFFL AI owner can do, and when it learned to do it.
{n_new} new features, {n_enh} enhancements, {n_fix} bug fixes.
See also the <a href="index.html">decision log</a> and the <a href="status.html">status page</a>.</p>
{''.join(sections)}
</main></body></html>"""
    OUT.write_text(page, encoding="utf-8")


if __name__ == "__main__":
    render()
    print(f"rendered {OUT} ({len(entries())} entries)")
