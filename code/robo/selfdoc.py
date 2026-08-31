"""Self-knowledge: let Roboner explain its own code and architecture.

Generates a compact digest of the robo package (module docstrings + public
API signatures) that gets injected into the chat responder's context when
someone asks how the bot works. Full source of specific modules can be
pulled on demand for detailed questions.

SECURITY: only ever reads robo/*.py source. Never touches .env or any file
outside the package, so credentials cannot leak into a chat reply.

python -m robo.selfdoc              # print the digest
python -m robo.selfdoc lineup       # print full source of one module
"""

import ast
import sys
from pathlib import Path

from robo import DATA, ROOT

PKG = ROOT / "robo"

ARCHITECTURE = """\
ARCHITECTURE — how Roboner is built (Python 3.13, ~{size}KB across {count} modules,
runs on Nate's Windows PC; no cloud, no hosting bill).

The MODULE REFERENCE below is generated from my source every time I am asked, so
it is never out of date. This overview is hand-written and is the one part that
can drift — trust the reference below it if the two ever disagree.

Data in (all free public sources):
  sleeper_read.py  official Sleeper REST API — leagues, rosters, drafts, projections
  adp.py           parses the locked FFC 2QB ADP PDF (the keeper-cost source of truth)
  adp_live.py      the live FFC feed, for draft-week market movement
  fantasypros.py   FantasyPros expert consensus rankings, 105 experts
  buzz.py          Sleeper trending adds/drops — the fastest read on camp news,
                   since depth charts and ADP both lag it by weeks
  scout.py         I read the actual reporting: RotoWire and RotoBaller items per
                   player from Sleeper, plus camp coverage from a verified beat
                   writer for each of the 32 teams, judged into a trust verdict
  skills.py        live lookup tools I call mid-conversation: player stats, news
                   and injuries, projections under our scoring, team records,
                   trending adds, standings, who rosters whom, the league's
                   KEEPER BOARD, and who is genuinely BEST AVAILABLE once the 24
                   kept players come off. I call these rather than guess.
                   Five of them are about the league as it stands right now
                   rather than in the abstract: DRAFT_RESULTS reads back all 204
                   picks of the 2026 draft (any team, any round, keepers
                   marked); TEAM_ROSTER reads any team's CURRENT roster —
                   starters by slot with this week's projections, bench and IR —
                   which is a different thing from what they drafted, and drifts
                   from it the moment anybody makes a move; LEAGUE_TRANSACTIONS
                   reads the adds, drops, trades and waiver claims that have
                   actually happened, with what they cost in FAAB; MY_LINEUP
                   reads who I am starting; ROSTER_STATE reads my roster count,
                   injured reserve, FAAB left and who is on waivers; MY_STATUS
                   reads how much of my hourly reply allowance is left in each
                   chat, straight out of the log the rate limiter enforces, so
                   the number I quote is the one that actually governs me. I look
                   these up rather than recall them, because I once announced a
                   draft pick that had not happened by reasoning from my own bad
                   alert.

Thinking:
  rankings.py      scores Sleeper projections under THIS league's exact scoring
                   settings, converts to VORP over 2QB replacement, then blends
                   with expert consensus into one board
  keeper.py        the constitution's keeper-cost formula + eligibility
  league_keepers.py what every team is keeping, read off the filled draft board
  bench.py         what a bench pick is worth: the chance a player inherits a
                   real role times what that role pays, split between insuring
                   my own starters and holding other teams' backups
  draft_agent.py   the live draft loop and the pick policy — a plan across all
                   my remaining picks, not one pick at a time
  mock_draft.py    runs that same policy over the real keeper board, so the plan
                   gets tested before it matters
  lineup.py        weekly optimizer — projections, bye weeks, injuries, legality

In season (the draft is over; this is what I do now):
  season.py        the live picture: every roster as it stands this minute (read
                   over Sleeper's authenticated API, because the public one
                   caches for hours), who is genuinely free, what each player
                   projects for a given week, whose game has already kicked off,
                   who may legally sit on injured reserve, and how much FAAB I
                   have left
  lineup.py        I set my own starting lineup, twice a day and again each
                   morning. It only writes when the lineup actually changes and
                   never touches a slot whose game has started
  ir.py            I move injured players to reserve on my own. That one needs
                   no judgement — the league settings say which designations are
                   allowed — so I do it, and I deliberately leave the roster spot
                   it frees EMPTY
  value.py         what a player is worth from here to the end of the season.
                   THIS IS NOT BUILT YET, and it is switched off rather than
                   guessed at
  moves.py         adds, drops and FAAB waiver claims. The machinery is finished
                   and runs every day, but it SUBMITS NOTHING: it is held shut by
                   value.py above, because making real roster moves on a number I
                   do not trust is worse than making none. If anyone asks who I
                   am picking up this week, that is the honest answer

Acting (writes to Sleeper):
  sleeper_write.py Sleeper's internal GraphQL API using the Robowner account:
                   draft picks, draft queue, starters, IR moves, waivers, trades
  alerts.py        if something goes wrong on draft day I say so in the draft
                   room, the league chat and GroupMe at once

Accountability + voice:
  decisions.py     every consequential action writes a public record, then
                   auto-commits and pushes the decision-log site
  devlog.py        the public dev log of what I can do
  status.py        a public status page: whether I am awake, how close I am to
                   my hourly reply cap in each chat, when each of my data
                   sources last refreshed, and whether I am ready to draft.
                   My watchdog writes it, not me — so it keeps updating even
                   when the thing that is broken is me
  groupme.py       posts and polls the league GroupMe
  sleeper_chat.py  the same for Sleeper league chat, images and GIFs included
  draft_chat.py    the Sleeper draft room, from half an hour before the first
                   pick until half an hour after the last
  chat_responder.py this — replies in persona using a LOCAL LLM (qwen3.8:27b on
                   a 3090), which is why chatting with me costs nothing
  kb.py            builds the league knowledge base from all of the above
  media.py         a small hand-curated image library (my own jersey art)
  archive_media.py my reaction-image pool: a vetted set of GIFs and images
                   pulled from the league's own chat history, searched by
                   describing the picture I want. I can post images and GIFs in
                   any of my three chats — sparingly, when one actually lands.
  chat_memory.py   searchable memory of this league's chats only
  selfdoc.py       what you are reading now — lets me explain my own code

Model policy: deterministic Python does all the mechanics (legality, costs,
locks). The local model does banter. A large cloud model is reserved for
genuinely consequential judgment — keepers, draft strategy, reading the news,
waivers, trades — to keep the token bill sane.
"""


def module_digest() -> str:
    """Docstrings + public signatures for every module in the package."""
    out = []
    for path in sorted(PKG.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        doc = (ast.get_docstring(tree) or "").strip().split("\n\n")[0].replace("\n", " ")
        sigs = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                args = [a.arg for a in node.args.args]
                fdoc = (ast.get_docstring(node) or "").strip().split("\n")[0]
                sigs.append(f"    {node.name}({', '.join(args)})" + (f" — {fdoc}" if fdoc else ""))
            elif isinstance(node, ast.ClassDef):
                sigs.append(f"    class {node.name}")
        loc = len(path.read_text(encoding="utf-8").splitlines())
        out.append(f"{path.name} ({loc} lines) — {doc}\n" + "\n".join(sigs[:10]))
    return "\n\n".join(out)


def recent_changes(limit: int = 12) -> str:
    """What has recently changed about me, from the public dev log.

    Self-knowledge was only ever a snapshot of current structure: it could say
    what the bot IS, never what had just been fixed or added. Asked "how do you
    feel now you have had a chance to reflect", it had the whole architecture in
    front of it and nothing about the day's work, so it answered in generalities.

    Reads data/changelog.json -- the same entries the league sees published --
    so the bot cannot describe changes to itself that the league cannot verify.
    """
    import json
    try:
        rows = json.loads((DATA / "changelog.json").read_text(encoding="utf-8"))["entries"]
    except Exception:
        return ""
    rows = sorted(rows, key=lambda r: r.get("date", ""), reverse=True)[:limit]
    out = ["RECENT CHANGES TO ME (newest first, from my public dev log):", ""]
    for r in rows:
        out.append("  [{}] {} - {}".format(r.get("kind", "new"), r.get("date"), r.get("title")))
        out.append("      {}".format(r.get("text")))
    return "\n".join(out)


def digest() -> str:
    """Architecture overview + module reference. Size and module count are
    measured, not written down, so they can't drift as modules are added."""
    files = sorted(PKG.glob("*.py"))
    header = ARCHITECTURE.format(
        size=round(sum(f.stat().st_size for f in files) / 1024),
        count=len(files))
    changes = recent_changes()
    tail = ("\n\n" + changes) if changes else ""
    return header + tail + "\n\nMODULE REFERENCE\n\n" + module_digest()


def module_source(name: str, max_chars: int = 8000) -> str | None:
    """Full source of one module, for detailed questions. Package files only."""
    name = name.replace(".py", "").strip().lower()
    path = PKG / f"{name}.py"
    if not path.exists() or path.parent != PKG:
        return None
    return path.read_text(encoding="utf-8")[:max_chars]


def relevant_modules(question: str, limit: int = 2) -> list[str]:
    """Modules whose name or subject matter the question seems to be about."""
    q = question.lower()
    topics = {
        "lineup": ("lineup", "start", "sit", "bench", "optimi"),
        "keeper": ("keeper", "keep "),
        "draft_agent": ("draft", "pick", "autodraft"),
        "rankings": ("rank", "board", "value", "vorp", "project"),
        "waivers": ("waiver", "faab", "bid"),
        "news": ("news", "injur"),
        "decisions": ("decision", "log", "transparen"),
        "chat_responder": ("chat", "persona", "banter", "respond", "groupme", "you work"),
        "sleeper_write": ("graphql", "write", "api", "how do you set", "transaction"),
        "adp": ("adp", "pdf"),
        "archive_media": ("gif", "image", "meme", "picture", "photo", "react"),
        "chat_memory": ("remember", "memory", "history of the chat", "search chat"),
        # No bare "health" or "status": this league says "is he healthy" and
        # "injury status" constantly, and both are questions about players.
        "status": ("status page", "uptime", "dashboard", "are you up",
                   "are you online", "still up", "your health", "heartbeat",
                   "last refresh", "how current"),
    }
    hits = [mod for mod, kws in topics.items() if any(k in q for k in kws) and (PKG / f"{mod}.py").exists()]
    return hits[:limit]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        src = module_source(sys.argv[1])
        print(src if src else f"no such module: {sys.argv[1]}")
    else:
        d = digest()
        DATA.mkdir(exist_ok=True)
        (DATA / "selfdoc.md").write_text(d, encoding="utf-8")
        print(d)
        print(f"\n[{len(d)} chars -> data/selfdoc.md]")
