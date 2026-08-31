# Roboner source

The Python that runs the RURFFL AI owner. This is the whole of it: every module, published automatically alongside the [decision log](../index.html) and the [dev log](../changelog.html).

It runs entirely on a desktop in Phoenix -- no cloud, no hosting bill. Banter is a local model; only genuinely consequential judgment goes to a paid one.

| module | what it does |
|---|---|
| [`robo/__init__.py`](robo/__init__.py) | RURFFL Robo Owner - AI franchise manager for the R U Ready 4 Some Football?! league. |
| [`robo/adp.py`](robo/adp.py) | Parse the locked FFC 2QB ADP PDF snapshot into data/adp_2026.csv. |
| [`robo/adp_live.py`](robo/adp_live.py) | Live FFC 2QB ADP — the market model for draft-day survival odds. |
| [`robo/alerts.py`](robo/alerts.py) | Shout across every channel at once, for the things a human must not miss. |
| [`robo/archive_media.py`](robo/archive_media.py) | Roboner's reaction-image pool, sourced from the personal GroupMe Archive. |
| [`robo/bench.py`](robo/bench.py) | What a bench pick is actually worth: insurance and lottery tickets. |
| [`robo/buzz.py`](robo/buzz.py) | What the market is doing RIGHT NOW, because ADP and depth charts lag. |
| [`robo/chat_memory.py`](robo/chat_memory.py) | League chat memory — the RUReady GroupMe plus Sleeper league chat, searchable. |
| [`robo/chat_responder.py`](robo/chat_responder.py) | Roboner chat responder: polls the league GroupMe, replies when addressed. |
| [`robo/curate_media.py`](robo/curate_media.py) | Human curation pass over the reaction-image pool. |
| [`robo/decisions.py`](robo/decisions.py) | Public decision log: every consequential Robowner action gets a record. |
| [`robo/devlog.py`](robo/devlog.py) | Public dev log — what the bot can do, published with the decision log. |
| [`robo/draft_agent.py`](robo/draft_agent.py) | Live draft agent: poll the Sleeper draft, pick on our turn. |
| [`robo/draft_chat.py`](robo/draft_chat.py) | The draft room as a chat channel, so the bot can talk while it drafts. |
| [`robo/draft_sim.py`](robo/draft_sim.py) | Earliest-vs-latest draft slot comparison (constitution §4.3.4.1). |
| [`robo/export_chat.py`](robo/export_chat.py) | Export league chat transcripts for human reading. |
| [`robo/fantasypros.py`](robo/fantasypros.py) | FantasyPros half-PPR superflex expert consensus rankings (ECR). |
| [`robo/groupme.py`](robo/groupme.py) | GroupMe integration for the Roboner bot. |
| [`robo/history.py`](robo/history.py) | Harvest the league's full Sleeper history into data/history.db. |
| [`robo/ir.py`](robo/ir.py) | Injured-reserve moves: the one roster decision that needs no valuation. |
| [`robo/kb.py`](robo/kb.py) | Build the league knowledge base: data/league_kb.json + LEAGUE.md. |
| [`robo/keeper.py`](robo/keeper.py) | Keeper eligibility + draft-cost formula (constitution §5). |
| [`robo/league_keepers.py`](robo/league_keepers.py) | Model the whole league's keeper landscape, so draft sims know who's really available. |
| [`robo/lineup.py`](robo/lineup.py) | Weekly lineup optimizer. |
| [`robo/lore.py`](robo/lore.py) | League lore — derived narrative facts from data/history.db. |
| [`robo/media.py`](robo/media.py) | Roboner's reaction-image library. |
| [`robo/mock_draft.py`](robo/mock_draft.py) | Mock the 2026 draft with the REAL pick policy, over the real keeper board. |
| [`robo/moves.py`](robo/moves.py) | Roster moves: free-agent adds and FAAB waiver claims. One policy, two channels. |
| [`robo/publish_code.py`](robo/publish_code.py) | Publish the bot's Python source to the public site, on an allowlist. |
| [`robo/pull_chat_history.py`](robo/pull_chat_history.py) | One-time pull of prior-season Sleeper league chat. |
| [`robo/rankings.py`](robo/rankings.py) | Build the 2026 draft value board: data/board_2026.csv. |
| [`robo/refresh.py`](robo/refresh.py) | Daily data refresh — keeps the AI owner's world current without a human. |
| [`robo/scout.py`](robo/scout.py) | Pre-draft gut check: read what the writers say, decide who to trust. |
| [`robo/season.py`](robo/season.py) | Live in-season league state -- what is true right now, not what the board froze. |
| [`robo/selfdoc.py`](robo/selfdoc.py) | Self-knowledge: let Roboner explain its own code and architecture. |
| [`robo/settings.py`](robo/settings.py) | Tunable settings: one registry, a JSON override file, and a revert checkpoint. |
| [`robo/skills.py`](robo/skills.py) | Roboner's live-data skills — the tools the chat bot can call to look things up. |
| [`robo/sleeper_chat.py`](robo/sleeper_chat.py) | Sleeper league chat — read and post as Robowner. |
| [`robo/sleeper_read.py`](robo/sleeper_read.py) | Read-only Sleeper REST API client (official, no auth). |
| [`robo/sleeper_write.py`](robo/sleeper_write.py) | Authenticated Sleeper GraphQL client (unofficial write API). |
| [`robo/status.py`](robo/status.py) | Public status dashboard — is the bot alive, and is what it knows current? |
| [`robo/value.py`](robo/value.py) | Rest-of-season player value -- the seam, in progress. |
| [`admin_gui.py`](admin_gui.py) | Roboner admin GUI — the tunable settings, with what each one does. |

<!-- Generated by robo/publish_code.py from each module's docstring and
     rewritten on every daily refresh. Editing this file on GitHub will
     be overwritten within a day; change the module docstring instead. -->
