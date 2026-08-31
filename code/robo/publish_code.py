"""Publish the bot's Python source to the public site, on an allowlist.

The league can already read every decision the bot makes and a dev log of what
it can do. This adds the third thing people ask for: how it actually works. It
copies the source into decision-log/code/, which rides the existing GitHub Pages
repo, so there is one public link and nobody needs an account.

ALLOWLIST, NEVER A DENYLIST. This publishes named patterns and nothing else. A
denylist would mean the next file added to the repo is public by default, which
is exactly how the identity map -- six league members' real names -- would end
up on the internet. If something new should be published, it has to be added
here deliberately.

WHAT IS DELIBERATELY NOT PUBLISHED, all of it code-adjacent and tempting:
  data/settings.json      the strategy weights. The whole reason they are not
                          published is that the audience IS the eleven opponents
  data/people.json        real names mapped to Sleeper handles: other people's
                          personal data, and not ours to publish
  data/mocks/, board CSV  our draft plans, contingencies and player valuations
  data/raw/               23MB of re-downloadable API dumps, of interest to
                          nobody
  .env                    obviously

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


def index(paths: list) -> str:
    """A readable front page: every module with its first docstring line."""
    import ast
    rows = []
    for p in paths:
        doc = ""
        try:
            doc = (ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")))
                   or "").strip().split("\n")[0]
        except Exception:
            pass
        rel = p.name if p.parent == ROOT else f"{p.parent.name}/{p.name}"
        rows.append(f"| [`{rel}`]({rel}) | {doc} |")
    return (
        "# Roboner source\n\n"
        "The Python that runs the RURFFL AI owner. This is the whole of it: "
        "every module, published automatically alongside the "
        "[decision log](../index.html) and the [dev log](../changelog.html).\n\n"
        "It runs entirely on a desktop in Phoenix -- no cloud, no hosting bill. "
        "Banter is a local model; only genuinely consequential judgment goes to "
        "a paid one.\n\n"
        # The paragraph that used to sit here listed what is withheld -- the
        # strategy weights, the identity map, the draft plans. Removed from the
        # page on 31 Aug 2026: it told eleven opponents that tuned weights exist
        # and are being kept from them, which is a tactical disclosure the page
        # does not need to volunteer. The bot still answers the question
        # honestly if anyone asks it directly, which is where that belongs.
        f"| module | what it does |\n|---|---|\n" + "\n".join(rows) + "\n\n"
        "<!-- Generated by robo/publish_code.py from each module's docstring and\n"
        "     rewritten on every daily refresh. Editing this file on GitHub will\n"
        "     be overwritten within a day; change the module docstring instead. -->\n")


def publish(push: bool = False, verbose: bool = True) -> dict:
    paths = _sources()
    problems = audit(paths)
    if problems:
        # Refuse outright. A publish step that "warns and continues" is how the
        # thing it warned about ends up on the internet.
        raise RuntimeError("refusing to publish: " + "; ".join(problems))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "robo").mkdir(exist_ok=True)
    wanted, changed = set(), 0
    for p in paths:
        dest = OUT / ("robo" if p.parent.name == "robo" else ".") / p.name
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
    # deleted locally must not keep living on the public site.
    removed = 0
    for existing in list(OUT.rglob("*")):
        if existing.is_file() and existing.resolve() not in wanted:
            existing.unlink()
            removed += 1

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
