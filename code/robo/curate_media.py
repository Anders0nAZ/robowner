"""Human curation pass over the reaction-image pool.

The pool's captions come from a vision model that saw ONE frame, so a gif whose
joke lands in its last half-second gets described by its first: "a man sitting at
a desk" for a spit-take. Those captions are what pick() searches, so a bad one
makes an image permanently unreachable and a generic one makes it match
everything. Neither shows up as an error -- just a bot that posts the same few
gifs forever.

This serves the pool up in batches as a self-contained page (images inlined, so
it works with no server and no live archive), takes freeform annotations, and
merges them back. Annotations live in data/media_curation.json keyed by content
HASH, not path: the same image exists in the pool under up to five paths, and a
re-sync adds more, so a path-keyed file would lose the work.

python -m robo.curate_media batch              # next batch -> an html page
python -m robo.curate_media batch --n 12
python -m robo.curate_media ingest <file.json> # merge annotations back
python -m robo.curate_media status
"""

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from robo import DATA
from robo.archive_media import _local_bytes

DB = DATA / "media_pool.db"
CURATION = DATA / "media_curation.json"

# Base64 inflates by ~4/3 and the artifact ceiling is 16MB, so cap the raw bytes
# well under it. One 10MB gif in the pool would otherwise eat a whole batch.
BUDGET_BYTES = 9 * 1024 * 1024
MAX_PER_ITEM = 5 * 1024 * 1024


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def load_curation() -> dict:
    if CURATION.exists():
        return json.loads(CURATION.read_text(encoding="utf-8"))
    return {}


def distinct_images() -> list[dict]:
    """Collapse the pool to distinct IMAGES, keyed by content hash.

    193 rows are 165 images. The duplicates are largely self-inflicted: the bot
    posts a gif, it lands in the league GroupMe, the archive captions it that
    night, and sync pulls it back as a fresh row. So the images that already get
    posted are exactly the ones that accumulate copies -- and since pick() only
    looks at the top 3 semantic hits, copies of one winner can fill the whole
    shortlist and leave nothing to lose to.
    """
    c = _conn()
    rows = list(c.execute(
        "SELECT local_path, local_zip, source, caption, cdn_url FROM pool"))
    c.close()
    by_hash: dict[str, dict] = {}
    for r in rows:
        blob = _local_bytes(r["local_path"], r["local_zip"])
        if not blob:
            continue
        h = hashlib.sha256(blob).hexdigest()
        e = by_hash.setdefault(h, {
            "hash": h, "bytes": blob, "size": len(blob), "copies": 0,
            "paths": [], "sources": set(), "caption": "", "posted": False,
        })
        e["copies"] += 1
        e["paths"].append(r["local_path"])
        e["sources"].add(r["source"])
        if r["cdn_url"]:
            e["posted"] = True
        # Keep the longest caption: the vision model is more often too terse
        # than wrong, and a longer one carries more for a human to correct.
        if len(r["caption"] or "") > len(e["caption"]):
            e["caption"] = r["caption"] or ""
    for e in by_hash.values():
        e["sources"] = sorted(e["sources"])
    return list(by_hash.values())


def next_batch(n: int = 10, budget: int = BUDGET_BYTES) -> list[dict]:
    """The most valuable images to annotate next, under a byte budget.

    Ordered by how much a correction is worth: images already in rotation first
    (a wrong caption on one of those is being posted TODAY), then images with
    duplicate copies, then everything else.
    """
    done = load_curation()
    pool = [e for e in distinct_images() if e["hash"] not in done]
    pool.sort(key=lambda e: (not e["posted"], -e["copies"], e["size"]))
    out, spent = [], 0
    for e in pool:
        if len(out) >= n:
            break
        if e["size"] > MAX_PER_ITEM or spent + e["size"] > budget:
            continue
        out.append(e)
        spent += e["size"]
    return out


# --------------------------------------------------------------------------
# page

FONTS = ("https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700"
         "&family=IBM+Plex+Mono:wght@400;500&display=swap")

CSS = """
:root{
  --ground:#131316; --panel:#1a1a1f; --panel-2:#212128; --edge:#2e2e37;
  --ink:#eae8e4; --muted:#918c99; --accent:#f0a12e;
  --keep:#5ec98a; --drop:#e0574f;
  --r:10px;
}
/* Single-theme by intent: judging an image needs a constant neutral surround,
   so this page does not follow the viewer's light mode. Every colour is still
   painted explicitly so it holds on any host ground. */
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:Archivo,"Helvetica Neue",Arial,sans-serif;
  font-size:15px; line-height:1.5;
  padding:32px 24px 120px;
}
.wrap{max-width:1080px;margin:0 auto}
header{border-bottom:1px solid var(--edge);padding-bottom:20px;margin-bottom:28px}
h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0 0 6px;text-wrap:balance}
.sub{color:var(--muted);font-size:14px;margin:0;max-width:65ch}
.sub b{color:var(--ink);font-weight:600}

.queue{display:flex;flex-direction:column;gap:18px}
.card{
  display:grid;grid-template-columns:minmax(0,340px) minmax(0,1fr);gap:22px;
  background:var(--panel);border:1px solid var(--edge);border-radius:var(--r);
  padding:18px;
}
@media(max-width:760px){.card{grid-template-columns:1fr}}
.frame{
  background:var(--panel-2);border:1px solid var(--edge);border-radius:8px;
  display:flex;align-items:center;justify-content:center;
  min-height:190px;overflow:hidden;
}
.frame img{max-width:100%;max-height:340px;display:block}

.meta{display:flex;flex-direction:column;gap:12px;min-width:0}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.idx{
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;
  color:var(--muted);font-variant-numeric:tabular-nums;
}
.chip{
  font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
  padding:3px 8px;border-radius:999px;border:1px solid var(--edge);color:var(--muted);
}
.chip.hot{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 45%,var(--edge))}
.chip.dup{color:var(--ink)}

.machine{
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12.5px;
  line-height:1.55;color:var(--muted);margin:0;
  background:var(--panel-2);border-left:2px solid var(--edge);
  padding:9px 12px;border-radius:0 6px 6px 0;
}
.machine::before{
  content:"machine caption";display:block;font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:#6c6776;margin-bottom:4px;
}
label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  display:block;margin-bottom:5px;font-weight:600}
input[type=text]{
  width:100%;background:var(--ground);color:var(--ink);
  border:1px solid var(--edge);border-radius:7px;padding:9px 11px;
  font-family:inherit;font-size:14px;
}
input[type=text]:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
input::placeholder{color:#615c6b}

.verdict{display:flex;gap:8px}
.verdict button{
  flex:0 0 auto;background:var(--ground);color:var(--muted);
  border:1px solid var(--edge);border-radius:7px;padding:7px 14px;
  font-family:inherit;font-size:13px;font-weight:600;cursor:pointer;
}
.verdict button:hover{color:var(--ink)}
.verdict button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.verdict button[aria-pressed=true][data-v=keep]{
  color:var(--keep);border-color:color-mix(in srgb,var(--keep) 50%,var(--edge));
  background:color-mix(in srgb,var(--keep) 10%,var(--ground));
}
.verdict button[aria-pressed=true][data-v=drop]{
  color:var(--drop);border-color:color-mix(in srgb,var(--drop) 50%,var(--edge));
  background:color-mix(in srgb,var(--drop) 10%,var(--ground));
}
.card[data-verdict=drop]{opacity:.62}
.card[data-verdict=drop] .frame img{filter:grayscale(.85)}

.bar{
  position:fixed;left:0;right:0;bottom:0;background:rgba(19,19,22,.96);
  border-top:1px solid var(--edge);padding:12px 24px;
  display:flex;align-items:center;gap:16px;justify-content:center;
  backdrop-filter:blur(8px);
}
.count{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:13px;
  color:var(--muted);font-variant-numeric:tabular-nums}
.count b{color:var(--ink)}
.bar button{
  background:var(--accent);color:#1a1408;border:0;border-radius:7px;
  padding:10px 18px;font-family:inherit;font-size:14px;font-weight:700;cursor:pointer;
}
.bar button:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.bar button.ghost{background:transparent;color:var(--muted);border:1px solid var(--edge);font-weight:600}
.out{
  position:absolute;left:-9999px;width:1px;height:1px;
}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = """
const KEY='roboner-curation-__BATCH__';
const state=JSON.parse(localStorage.getItem(KEY)||'{}');

function save(){ try{localStorage.setItem(KEY,JSON.stringify(state))}catch(e){} tally(); }

function tally(){
  const n=Object.values(state).filter(v=>v && (v.shows||v.use||v.verdict)).length;
  document.getElementById('n').textContent=n;
}

document.querySelectorAll('.card').forEach(card=>{
  const h=card.dataset.hash;
  state[h]=state[h]||{shows:'',use:'',verdict:''};
  card.querySelectorAll('input[type=text]').forEach(inp=>{
    inp.value=state[h][inp.dataset.f]||'';
    inp.addEventListener('input',()=>{state[h][inp.dataset.f]=inp.value;save()});
  });
  card.querySelectorAll('.verdict button').forEach(b=>{
    b.addEventListener('click',()=>{
      const v=b.dataset.v;
      state[h].verdict = state[h].verdict===v ? '' : v;
      card.dataset.verdict=state[h].verdict;
      card.querySelectorAll('.verdict button').forEach(x=>
        x.setAttribute('aria-pressed', String(x.dataset.v===state[h].verdict)));
      save();
    });
  });
  card.dataset.verdict=state[h].verdict||'';
  card.querySelectorAll('.verdict button').forEach(x=>
    x.setAttribute('aria-pressed', String(x.dataset.v===state[h].verdict)));
});
tally();

document.getElementById('copy').addEventListener('click',async()=>{
  const payload={};
  for(const [h,v] of Object.entries(state)){
    if(v && (v.shows||v.use||v.verdict)) payload[h]=v;
  }
  const txt=JSON.stringify(payload,null,1);
  const btn=document.getElementById('copy');
  try{
    await navigator.clipboard.writeText(txt);
    btn.textContent='Copied';
  }catch(e){
    const ta=document.getElementById('out');
    ta.value=txt; ta.style.position='static'; ta.style.width='100%';
    ta.style.height='160px'; ta.select();
    btn.textContent='Select all and copy';
  }
  setTimeout(()=>{btn.textContent='Copy annotations'},2500);
});

document.getElementById('clear').addEventListener('click',()=>{
  if(!confirm('Clear every annotation on this page?'))return;
  for(const h of Object.keys(state)) state[h]={shows:'',use:'',verdict:''};
  document.querySelectorAll('input[type=text]').forEach(i=>i.value='');
  document.querySelectorAll('.card').forEach(c=>{c.dataset.verdict='';
    c.querySelectorAll('.verdict button').forEach(x=>x.setAttribute('aria-pressed','false'))});
  save();
});
"""


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(batch: list[dict], batch_id: str) -> str:
    cards = []
    for i, e in enumerate(batch, 1):
        b64 = base64.b64encode(e["bytes"]).decode("ascii")
        chips = []
        if e["posted"]:
            chips.append('<span class="chip hot">in rotation</span>')
        if e["copies"] > 1:
            chips.append(f'<span class="chip dup">&times;{e["copies"]} copies</span>')
        chips.append(f'<span class="chip">{esc("/".join(e["sources"]))}</span>')
        cards.append(f"""
    <article class="card" data-hash="{e['hash']}">
      <div class="frame"><img src="data:image/gif;base64,{b64}" alt="pool image {i}"></div>
      <div class="meta">
        <div class="row">
          <span class="idx">{i:02d} / {len(batch):02d}</span>
          {''.join(chips)}
        </div>
        <p class="machine">{esc(e['caption']) or '(no caption)'}</p>
        <div>
          <label for="s{i}">What it actually shows</label>
          <input type="text" id="s{i}" data-f="shows"
                 placeholder="describe the picture, including whatever happens at the end">
        </div>
        <div>
          <label for="u{i}">When Roboner should use it</label>
          <input type="text" id="u{i}" data-f="use"
                 placeholder="the moment it fits - gloating, a bad beat, an entrance">
        </div>
        <div class="verdict">
          <button type="button" data-v="keep" aria-pressed="false">Keep</button>
          <button type="button" data-v="drop" aria-pressed="false">Drop</button>
        </div>
      </div>
    </article>""")

    hot = sum(1 for e in batch if e["posted"])
    return f"""<title>Roboner Gif Bench</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<style>{CSS}</style>
<div class="wrap">
  <header>
    <h1>Reaction pool, batch {batch_id}</h1>
    <p class="sub">The caption under each gif is what a vision model made of a
    <b>single frame</b>, and it is the only text <b>pick()</b> searches. Correct what it
    shows, then say when the bot should reach for it &mdash; the second line is what
    actually decides whether it gets posted. <b>{hot}</b> of these {len(batch)} are
    already in rotation.</p>
  </header>
  <div class="queue">{''.join(cards)}
  </div>
</div>
<div class="bar">
  <span class="count"><b id="n">0</b> of {len(batch)} annotated</span>
  <button id="copy" type="button">Copy annotations</button>
  <button id="clear" class="ghost" type="button">Clear</button>
</div>
<textarea id="out" class="out" aria-hidden="true"></textarea>
<script>{JS.replace('__BATCH__', batch_id)}</script>
"""


# --------------------------------------------------------------------------
# ingest

def ingest(path: str) -> str:
    """Merge a pasted annotation payload into data/media_curation.json."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cur = load_curation()
    known = {e["hash"]: e for e in distinct_images()}
    added = skipped = 0
    for h, v in raw.items():
        if h not in known:
            skipped += 1
            continue
        cur[h] = {
            "shows": (v.get("shows") or "").strip(),
            "use": (v.get("use") or "").strip(),
            "verdict": v.get("verdict") or "",
            "paths": known[h]["paths"],
        }
        added += 1
    CURATION.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    return f"merged {added} annotations ({skipped} unknown hashes), {len(cur)} total"


def status() -> str:
    imgs = distinct_images()
    cur = load_curation()
    dup = sum(e["copies"] - 1 for e in imgs)
    posted = [e for e in imgs if e["posted"]]
    done_posted = sum(1 for e in posted if e["hash"] in cur)
    drops = sum(1 for v in cur.values() if v.get("verdict") == "drop")
    return (f"{sum(e['copies'] for e in imgs)} rows -> {len(imgs)} distinct images "
            f"({dup} duplicate rows)\n"
            f"annotated: {len(cur)}/{len(imgs)}   in-rotation annotated: "
            f"{done_posted}/{len(posted)}\nmarked drop: {drops}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "batch"
    if cmd == "batch":
        n = 10
        if "--n" in sys.argv:
            n = int(sys.argv[sys.argv.index("--n") + 1])
        done = len(load_curation())
        batch = next_batch(n)
        if not batch:
            print("nothing left to annotate")
            sys.exit(0)
        bid = str(done // max(len(batch), 1) + 1)
        out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv \
            else DATA / f"curation_batch_{bid}.html"
        out.write_text(render(batch, bid), encoding="utf-8")
        mb = sum(e["size"] for e in batch) / 1024 / 1024
        print(f"batch {bid}: {len(batch)} images, {mb:.1f} MB raw -> {out}")
    elif cmd == "ingest":
        print(ingest(sys.argv[2]))
    elif cmd == "status":
        print(status())
