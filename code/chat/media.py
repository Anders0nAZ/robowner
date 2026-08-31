"""Roboner's reaction-image library.

A *curated* set — the bot picks from images we've vetted rather than pulling
random internet content. Drop .png/.jpg/.gif files into data/media/, run
`python -m robo.media scan`, then tag them in data/media/manifest.json so the
model knows when each one fits.

Uploaded GroupMe CDN urls are cached in the manifest, so each file is only
uploaded once no matter how often it gets posted.

python -m robo.media scan          # add new files to the manifest
python -m robo.media list          # show the library and its tags
python -m robo.media test <slug>   # upload + print the CDN url
"""

import json
import sys
from pathlib import Path

from robo import DATA

MEDIA_DIR = DATA / "media"
MANIFEST = MEDIA_DIR / "manifest.json"
EXTS = {".png", ".jpg", ".jpeg", ".gif"}


def _load() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def _save(m: dict) -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=1), encoding="utf-8")


def scan() -> dict:
    """Register any new media files (untagged) so they can be described."""
    m = _load()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(MEDIA_DIR.iterdir()):
        if f.suffix.lower() not in EXTS or f.name == "manifest.json":
            continue
        slug = f.stem.lower().replace(" ", "_")
        if slug not in m:
            m[slug] = {"file": f.name, "tags": [], "description": "", "cdn_url": None}
    _save(m)
    return m


def library() -> dict:
    """Only entries that are actually usable (described)."""
    return {k: v for k, v in _load().items() if v.get("description")}


def catalog_for_prompt() -> str:
    """Compact listing the model can choose from."""
    lib = library()
    if not lib:
        return ""
    return "\n".join(f"  [img:{k}] — {v['description']}" for k, v in lib.items())


def cdn_url(slug: str) -> str | None:
    """CDN url for a slug, uploading (and caching) on first use."""
    m = _load()
    entry = m.get(slug)
    if not entry:
        return None
    if entry.get("cdn_url"):
        return entry["cdn_url"]
    path = MEDIA_DIR / entry["file"]
    if not path.exists():
        return None
    from robo.groupme import upload_image
    url = upload_image(str(path))
    entry["cdn_url"] = url
    _save(m)
    return url


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "scan":
        m = scan()
        untagged = [k for k, v in m.items() if not v.get("description")]
        print(f"{len(m)} files registered.")
        if untagged:
            print(f"needs description before use: {', '.join(untagged)}")
            print(f"edit {MANIFEST}")
    elif cmd == "test":
        print(cdn_url(sys.argv[2]))
    else:
        for k, v in _load().items():
            state = "ready" if v.get("description") else "UNTAGGED"
            print(f"[{state}] {k}: {v.get('description') or v['file']}")
