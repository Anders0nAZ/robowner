"""Publish the bot's Python source to the public site, on an allowlist.

The league can already read every decision the bot makes and a dev log of what
it can do. This adds the third thing people ask for: how it actually works. It
copies the source into decision-log/code/, which rides the existing GitHub Pages
repo, so there is one public link and nobody needs an account.

ALLOWLIST, NEVER A DENYLIST. ALLOW below names what goes; everything else in the
repository stays local. A denylist would make each newly added file public by
default, and the cost of getting that wrong is not symmetric -- an unpublished
module is a shrug, an unpublished-by-accident data file cannot be recalled.
Publishing something new is an edit here, deliberately.

Every file is checked against the live values in .env before anything is copied,
and the publish is refused outright rather than warned about.

python -m robo.publish_code            # copy, report, do not push
python -m robo.publish_code --push     # copy and publish
"""

import argparse
import filecmp
import re
import shutil

from robo import DATA, ROOT

OUT = ROOT / "decision-log" / "code"

# Exactly what goes public. Source only.
ALLOW = [
    ("robo", "*.py"),
    (".", "admin_gui.py"),
]

# Files matching the allowlist that still must not go. Kept tiny on purpose --
# if this list ever grows, the allowlist above is the thing that is wrong.
NEVER = set()

# READING LAYOUT, NOT THE PACKAGE LAYOUT. The package is flat -- everything is
# robo/<name>.py and imports each other that way. Forty-two files in one list is
# fine for an editor and hopeless for a league member who wants to know how the
# draft worked, so the published copy groups them by the job they do. The index
# says plainly that it has done this, because a directory tree that quietly
# disagrees with the imports inside it is worse than no tree at all.
CATEGORIES = [
    ("data", "Reading the world",
     "Everything the bot knows comes in through here. All free, all public "
     "except Sleeper's write API, which uses its own account.",
     ["sleeper_read", "sleeper_write", "adp", "adp_live", "fantasypros",
      "buzz", "scout", "history"]),
    ("draft", "The draft",
     "Valuing players, pricing keepers, and the agent that actually sat on the "
     "clock and submitted picks.",
     ["rankings", "keeper", "league_keepers", "bench", "draft_agent",
      "draft_sim", "mock_draft", "draft_chat"]),
    ("in-season", "In season",
     "Weekly lineups, injured reserve, and the add/drop and waiver machinery.",
     ["season", "lineup", "model_proj", "ir", "value", "moves"]),
    ("chat", "Talking",
     "The bot's voice in the league chats, the tools it calls to look things "
     "up mid-conversation, and its memory of what has been said.",
     ["chat_responder", "skills", "selfdoc", "chat_memory", "lore", "kb",
      "groupme", "sleeper_chat", "chat_cursor", "alerts", "media", "archive_media",
      "curate_media", "export_chat", "pull_chat_history"]),
    ("published", "Showing its work",
     "The three public pages and this publisher. Every consequential action "
     "writes a record before anyone asks for one.",
     ["decisions", "devlog", "status", "publish_code"]),
    ("running", "Keeping it running",
     "The daily pipeline, the tunable settings behind it, and the local admin "
     "app that edits them.",
     ["__init__", "refresh", "settings", "admin_gui"]),
]


def _sources() -> list:
    out = []
    for folder, pattern in ALLOW:
        base = ROOT if folder == "." else ROOT / folder
        for p in sorted(base.glob(pattern)):
            if p.name in NEVER or "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


def _secret_values() -> list[str]:
    """The actual values from .env, to check they appear in nothing we publish.

    Scanning for secret-shaped PATTERNS gives false positives here: three
    modules legitimately contain the string "SLEEPER_TOKEN=" because they parse
    .env. Checking for the literal values instead is exact -- it cannot miss a
    real leak and it cannot fire on a variable name.
    """
    env = ROOT / ".env"
    if not env.exists():
        return []
    vals = []
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        v = line.split("=", 1)[1].strip().strip('"').strip("'")
        if len(v) >= 8:
            vals.append(v)
    return vals


# Belt and braces on top of the value check: a JWT that is not ours is still a
# JWT, and should never be sitting in source.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def audit(paths: list) -> list[str]:
    """Anything that must stop the publish. Empty list means safe."""
    problems = []
    secrets = _secret_values()
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            problems.append(f"{p.name}: unreadable ({e})")
            continue
        for v in secrets:
            if v in text:
                problems.append(f"{p.name}: contains a literal value from .env")
        if _JWT.search(text):
            problems.append(f"{p.name}: contains something shaped like a JWT")
    return problems


def _category_of(stem: str) -> str:
    for slug, _title, _blurb, mods in CATEGORIES:
        if stem in mods:
            return slug
    return ""


def uncategorised(paths: list) -> list[str]:
    """Modules CATEGORIES does not place. Publishing stops on these.

    Same reasoning as the allowlist: a module that quietly lands in a catch-all
    folder is a module nobody decided about. Adding a file to robo/ should make
    this shout, not shrug.
    """
    return sorted(p.stem for p in paths if not _category_of(p.stem))


def _doc(p) -> str:
    import ast
    try:
        return (ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")))
                or "").strip().split("\n")[0]
    except Exception:
        return ""


def index(paths: list) -> str:
    """A readable front page, grouped the way the folders are."""
    by_stem = {p.stem: p for p in paths}
    out = [
        "# Roboner source",
        "",
        "The Python that runs the RURFFL AI owner. This is the whole of it, "
        "published automatically alongside the [decision log](../index.html) "
        "and the [dev log](../changelog.html).",
        "",
        "It runs entirely on a desktop in Phoenix -- no cloud, no hosting bill. "
        "Banter is a local model; only genuinely consequential judgment goes to "
        "a paid one.",
        "",
        "> These folders group the modules by the job they do, for reading. The "
        "package itself is flat: every file below lives at `robo/<name>.py` and "
        "the imports inside them say so.",
        "",
    ]
    for slug, title, blurb, mods in CATEGORIES:
        present = [m for m in mods if m in by_stem]
        if not present:
            continue
        out += [f"## {title}", "", blurb, "",
                "| module | what it does |", "|---|---|"]
        for m in present:
            out.append(f"| [`{m}.py`]({slug}/{m}.py) | {_doc(by_stem[m])} |")
        out.append("")
    out += [
        "<!-- Generated by robo/publish_code.py from each module's docstring and",
        "     rewritten on every daily refresh. Editing this file on GitHub will",
        "     be overwritten within a day; change the module docstring instead. -->",
        "",
    ]
    return "\n".join(out)


def publish(push: bool = False, verbose: bool = True) -> dict:
    paths = _sources()
    problems = audit(paths)
    if problems:
        # Refuse outright. A publish step that "warns and continues" is how the
        # thing it warned about ends up on the internet.
        raise RuntimeError("refusing to publish: " + "; ".join(problems))
    orphans = uncategorised(paths)
    if orphans:
        raise RuntimeError(
            "refusing to publish: no category for " + ", ".join(orphans)
            + " -- add them to CATEGORIES in this module")

    OUT.mkdir(parents=True, exist_ok=True)
    wanted, changed = set(), 0
    for p in paths:
        folder = OUT / _category_of(p.stem)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / p.name
        wanted.add(dest.resolve())
        if not dest.exists() or not filecmp.cmp(p, dest, shallow=False):
            shutil.copy2(p, dest)
            changed += 1

    idx = OUT / "README.md"
    wanted.add(idx.resolve())
    new_idx = index(paths)
    if not idx.exists() or idx.read_text(encoding="utf-8") != new_idx:
        idx.write_text(new_idx, encoding="utf-8")
        changed += 1

    # Drop anything we published before and no longer would: a module that was
    # deleted locally, or one that has since moved to a different category,
    # must not keep living on the public site under its old path.
    removed = 0
    for existing in list(OUT.rglob("*")):
        if existing.is_file() and existing.resolve() not in wanted:
            existing.unlink()
            removed += 1
    # ...and then the folders they leave behind, or a renamed category lingers
    # forever as an empty directory.
    for d in sorted(OUT.rglob("*"), key=lambda x: -len(x.parts)):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    if verbose:
        print(f"{len(paths)} source files -> {OUT}"
              f" ({changed} written, {removed} removed)")
    if push and (changed or removed):
        from robo import decisions
        decisions.publish(f"code: publish {len(paths)} source files")
    return {"files": len(paths), "changed": changed, "removed": removed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()
    res = publish(push=args.push)
    if not args.push:
        print("not pushed (use --push)")
    print(res)


if __name__ == "__main__":
    main()
