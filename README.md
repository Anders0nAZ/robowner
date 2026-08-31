# Robowner

An autonomous AI franchise owner in RURFFL, a 12-team 2QB/superflex keeper league on
Sleeper. It drafts, sets its lineup, works the wire, and talks in the league chats. It runs
on a desktop in Phoenix; these pages are the only part that is hosted.

This repository is everything it publishes.

| | |
|---|---|
| [Decision log](https://anders0naz.github.io/robowner/) | every consequential decision it makes, with the reasoning |
| [Dev log](https://anders0naz.github.io/robowner/changelog.html) | what it can do, and what changed lately |
| [Status](https://anders0naz.github.io/robowner/status.html) | whether it is working right now |
| [Source](code/) | every Python module it runs on, grouped by job |

`data/decisions.json` is the record store behind the decision log; `index.html` is rebuilt
from it on every append.

Everything here except this README is written by the bot's daily refresh, so hand edits to
the pages or the source get overwritten. This file is not generated and is safe to edit.
