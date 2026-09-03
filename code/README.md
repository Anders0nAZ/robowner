# Roboner source

The Python that runs the RURFFL AI owner. This is the whole of it, published automatically alongside the [decision log](../index.html) and the [dev log](../changelog.html).

It runs entirely on a desktop in Phoenix -- no cloud, no hosting bill. Banter is a local model; only genuinely consequential judgment goes to a paid one.

> These folders group the modules by the job they do, for reading. The package itself is flat: every file below lives at `robo/<name>.py` and the imports inside them say so.

## Reading the world

Everything the bot knows comes in through here. All free, all public except Sleeper's write API, which uses its own account.

| module | what it does |
|---|---|
| [`sleeper_read.py`](data/sleeper_read.py) | Read-only Sleeper REST API client (official, no auth). |
| [`sleeper_write.py`](data/sleeper_write.py) | Authenticated Sleeper GraphQL client (unofficial write API). |
| [`adp.py`](data/adp.py) | Parse the locked FFC 2QB ADP PDF snapshot into data/adp_2026.csv. |
| [`adp_live.py`](data/adp_live.py) | Live FFC 2QB ADP — the market model for draft-day survival odds. |
| [`fantasypros.py`](data/fantasypros.py) | FantasyPros half-PPR superflex expert consensus rankings (ECR). |
| [`buzz.py`](data/buzz.py) | What the market is doing RIGHT NOW, because ADP and depth charts lag. |
| [`scout.py`](data/scout.py) | Pre-draft gut check: read what the writers say, decide who to trust. |
| [`history.py`](data/history.py) | Harvest the league's full Sleeper history into data/history.db. |

## The draft

Valuing players, pricing keepers, and the agent that actually sat on the clock and submitted picks.

| module | what it does |
|---|---|
| [`rankings.py`](draft/rankings.py) | Build the 2026 draft value board: data/board_2026.csv. |
| [`keeper.py`](draft/keeper.py) | Keeper eligibility + draft-cost formula (constitution §5). |
| [`league_keepers.py`](draft/league_keepers.py) | Model the whole league's keeper landscape, so draft sims know who's really available. |
| [`bench.py`](draft/bench.py) | What a bench pick is actually worth: insurance and lottery tickets. |
| [`draft_agent.py`](draft/draft_agent.py) | Live draft agent: poll the Sleeper draft, pick on our turn. |
| [`draft_sim.py`](draft/draft_sim.py) | Earliest-vs-latest draft slot comparison (constitution §4.3.4.1). |
| [`mock_draft.py`](draft/mock_draft.py) | Mock the 2026 draft with the REAL pick policy, over the real keeper board. |
| [`draft_chat.py`](draft/draft_chat.py) | The draft room as a chat channel, so the bot can talk while it drafts. |

## In season

Weekly lineups, injured reserve, and the add/drop and waiver machinery.

| module | what it does |
|---|---|
| [`season.py`](in-season/season.py) | Live in-season league state -- what is true right now, not what the board froze. |
| [`lineup.py`](in-season/lineup.py) | Weekly lineup optimizer. |
| [`model_proj.py`](in-season/model_proj.py) | This week's projections from the NFL Model, or nothing at all. |
| [`ir.py`](in-season/ir.py) | Injured-reserve moves: the one roster decision that needs no valuation. |
| [`value.py`](in-season/value.py) | Rest-of-season player value -- the seam, in progress. |
| [`moves.py`](in-season/moves.py) | Roster moves: free-agent adds and FAAB waiver claims. One policy, two channels. |

## Talking

The bot's voice in the league chats, the tools it calls to look things up mid-conversation, and its memory of what has been said.

| module | what it does |
|---|---|
| [`chat_responder.py`](chat/chat_responder.py) | Roboner chat responder: polls the league GroupMe, replies when addressed. |
| [`skills.py`](chat/skills.py) | Roboner's live-data skills — the tools the chat bot can call to look things up. |
| [`selfdoc.py`](chat/selfdoc.py) | Self-knowledge: let Roboner explain its own code and architecture. |
| [`chat_memory.py`](chat/chat_memory.py) | League chat memory — the RUReady GroupMe plus Sleeper league chat, searchable. |
| [`lore.py`](chat/lore.py) | League lore — derived narrative facts from data/history.db. |
| [`kb.py`](chat/kb.py) | Build the league knowledge base: data/league_kb.json + LEAGUE.md. |
| [`groupme.py`](chat/groupme.py) | GroupMe integration for the Roboner bot. |
| [`sleeper_chat.py`](chat/sleeper_chat.py) | Sleeper league chat — read and post as Robowner. |
| [`chat_cursor.py`](chat/chat_cursor.py) | Where each chat channel got to -- the "already answered this" marker. |
| [`alerts.py`](chat/alerts.py) | Shout across every channel at once, for the things a human must not miss. |
| [`media.py`](chat/media.py) | Roboner's reaction-image library. |
| [`archive_media.py`](chat/archive_media.py) | Roboner's reaction-image pool, sourced from the personal GroupMe Archive. |
| [`curate_media.py`](chat/curate_media.py) | Human curation pass over the reaction-image pool. |
| [`export_chat.py`](chat/export_chat.py) | Export league chat transcripts for human reading. |
| [`pull_chat_history.py`](chat/pull_chat_history.py) | One-time pull of prior-season Sleeper league chat. |

## Showing its work

The three public pages and this publisher. Every consequential action writes a record before anyone asks for one.

| module | what it does |
|---|---|
| [`decisions.py`](published/decisions.py) | Public decision log: every consequential Robowner action gets a record. |
| [`devlog.py`](published/devlog.py) | Public dev log — what the bot can do, published with the decision log. |
| [`status.py`](published/status.py) | Public status dashboard — is the bot alive, and is what it knows current? |
| [`publish_code.py`](published/publish_code.py) | Publish the bot's Python source to the public site, on an allowlist. |

## Keeping it running

The daily pipeline, the tunable settings behind it, and the local admin app that edits them.

| module | what it does |
|---|---|
| [`__init__.py`](running/__init__.py) | RURFFL Robo Owner - AI franchise manager for the R U Ready 4 Some Football?! league. |
| [`refresh.py`](running/refresh.py) | Daily data refresh — keeps the AI owner's world current without a human. |
| [`settings.py`](running/settings.py) | Tunable settings: one registry, a JSON override file, and a revert checkpoint. |
| [`admin_gui.py`](running/admin_gui.py) | Roboner admin GUI — the tunable settings, with what each one does. |

<!-- Generated by robo/publish_code.py from each module's docstring and
     rewritten on every daily refresh. Editing this file on GitHub will
     be overwritten within a day; change the module docstring instead. -->
