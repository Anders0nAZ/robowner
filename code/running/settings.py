"""Tunable settings: one registry, a JSON override file, and a revert checkpoint.

Every dial in this project is a module-level constant, and `bench.py` says
outright that they are "stated assumptions, not fitted numbers -- tune them
here". This makes "here" a GUI instead of a text editor, without moving the
values out of the code: the code keeps the default, this file only overrides it.

HOW IT APPLIES. Each tunable module ends its constant block with

    settings.apply(__name__, globals())

which, for every registry entry belonging to that module:
  1. records the value already in globals() as the DEFAULT, if it has never been
     recorded -- so the checkpoint is captured from the source itself and cannot
     drift away from what the code says;
  2. overrides it from data/settings.json, but only for a name that already
     exists and only when the value survives its type and bounds check.
A typo in the JSON therefore cannot invent a phantom setting or wedge a module.
It is logged and the code value stands.

RESTART REQUIRED. These are import-time constants. A saved change does nothing
until the process restarts; entries carry `restart` so the GUI can say so
plainly rather than let someone believe a change has landed when it has not.

WHAT THIS IS NOT. It tunes POLICY, never PLAYERS. Weights, thresholds, rates and
caps -- yes. A per-player boost, avoid, target or exclusion -- never, in any
section, under any label. A `data/watchlist.json` doing exactly that was built
and deleted on the same day (28 Aug 2026) because it made the bot a tool
executing someone else's draft. The registry is a fixed list of named constants
precisely so there is no free-text field a player name could be typed into.

Settings stay local. The decision log records what the bot decided; this file
is how the operator tunes it, which is a different thing and not part of the
public record.

python -m robo.settings            # show every setting, default vs current
python -m robo.settings --json     # the same, machine-readable
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

from robo import DATA

OVERRIDES = DATA / "settings.json"
DEFAULTS = DATA / "settings_defaults.json"
HISTORY = DATA / "settings_history.jsonl"

RESPONDER, DRAFT_PREP, LIVE_DRAFT = "responder", "draft_prep", "live_draft"
LINEUP, ROSTER = "lineup", "roster"

SECTIONS = [
    (RESPONDER, "Responder", "How the bot behaves in the three chats."),
    (DRAFT_PREP, "Draft prep", "How players are valued before the draft opens."),
    (LIVE_DRAFT, "Live draft", "How the agent behaves while the draft runs."),
    (LINEUP, "Weekly lineup", "Setting the weekly starting lineup."),
    (ROSTER, "Roster manager", "Adds, drops, IR moves and waiver budget."),
]


@dataclass(frozen=True)
class S:
    """One tunable.

    `does` and `implication` are mostly lifted from the comments already sitting
    above these constants, which are unusually candid about what went wrong when
    a value was set badly. Where a comment records a past failure, that failure
    IS the implication and it is quoted rather than paraphrased.
    """
    section: str
    module: str
    name: str
    type: type
    does: str
    implication: str
    bounds: tuple | None = None      # (min, max) for int/float
    choices: tuple = ()              # for str
    restart: bool = True
    danger: bool = False             # structural: a fact about the league
    unit: str = ""

    @property
    def key(self) -> str:
        return f"{self.module}.{self.name}"

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").lower()


# --------------------------------------------------------------------------
# the registry

REGISTRY: list[S] = [

    # ---------------- responder ----------------
    S(RESPONDER, "robo.chat_responder", "MODEL", str,
      "Which local Ollama model writes the bot's replies.",
      "MUST be a tag with num_ctx baked in. The bare qwen3.8:27b-mtp-q4_K_M tag "
      "bakes none, so it inherits the machine-wide 32k limit -- and when a prompt "
      "exceeds that, Ollama silently drops the OLDEST tokens, which is the system "
      "prompt: the persona, the identity map, and the rules about what it may "
      "claim. No error, just a bot that quietly forgets who it is.",
      danger=True),
    S(RESPONDER, "robo.chat_responder", "MAX_REPLIES_PER_HOUR", int,
      "How many replies the bot will send in one chat in one hour.",
      "Per channel, not total. Raise it and it can dominate a busy thread; lower "
      "it and it goes quiet mid-conversation. The status page shows how close to "
      "this cap each chat currently is.",
      bounds=(1, 200), unit="replies/hour"),
    S(RESPONDER, "robo.chat_responder", "POLL_SECS", int,
      "How often the bot checks each chat for messages addressed to it.",
      "Lower means it answers faster and makes more API calls; higher means it "
      "can take this long to notice you. It polls -- nothing pushes messages to "
      "it -- so this is the whole of its reaction time.",
      bounds=(10, 600), unit="seconds"),
    S(RESPONDER, "robo.chat_responder", "HISTORY_DAYS", float,
      "How much recent conversation goes into every prompt unasked.",
      "Time-bound so it stays relevant. Anything older is only reachable through "
      "the league_chat_history tool, which the bot must choose to call. Widening "
      "this made replies longer, so the terseness rule had to be hardened with it.",
      bounds=(0.5, 30.0), unit="days"),
    S(RESPONDER, "robo.chat_responder", "HISTORY_MAX", int,
      "Hard cap on how many recent messages go into the prompt.",
      "Count-capped so one blow-up day cannot crowd out the week -- this group's "
      "busiest day was 153 messages. A week costs ~5k tokens of a 98k window, so "
      "the binding limit is the model's attention, not room.",
      bounds=(10, 1000), unit="messages"),
    S(RESPONDER, "robo.chat_responder", "KEEP_ALIVE", str,
      "How long Ollama holds the 17GB model in VRAM after a reply.",
      "The machine-wide default is 30s, and triggers are rare, so without this "
      "the model unloaded between every reply and each one paid a full cold load. "
      "Longer keeps the GPU occupied for other apps.",
      choices=("5m", "15m", "30m", "1h", "-1")),
    S(RESPONDER, "robo.chat_responder", "MAX_BATCH_ATTEMPTS", int,
      "How many times a failing batch of messages is retried before it is skipped.",
      "Cursors are only committed once a batch is handled, so a poison message "
      "would otherwise wedge the loop forever. Set to 1 and one transient network "
      "blip loses messages; set high and one bad batch stops the bot replying.",
      bounds=(1, 20), unit="attempts"),
    S(RESPONDER, "robo.chat_responder", "TRIGGERS", tuple,
      "The words that make the bot decide it was spoken to.",
      "It is reactive only -- it never speaks unprompted -- so this list is the "
      "whole of what wakes it. Remove a nickname people actually use and it will "
      "look like it is ignoring them. Matching is lowercase substring.",
      restart=True),
    S(RESPONDER, "robo.archive_media", "PICK_CUTOFF", float,
      "How close a reaction image must match before the bot will post it.",
      "Cosine distance, and pick() posts without review. Measured on this pool: a "
      "caption that genuinely matches lands at 0.22-0.29 and nonsense lands at "
      "0.37+, so 0.32 splits them. A loose threshold here does not return nothing, "
      "it returns a confidently wrong GIF in front of the league.",
      bounds=(0.05, 0.60)),
    S(RESPONDER, "robo.archive_media", "SEM_CUTOFF", float,
      "The looser threshold used when a human is browsing search results.",
      "Only affects the search tool, where someone is looking at a grid and can "
      "ignore a bad hit. Has no effect on what the bot posts -- that is "
      "PICK_CUTOFF.",
      bounds=(0.05, 1.0), restart=True),

    # ---------------- draft prep ----------------
    S(DRAFT_PREP, "robo.bench", "INSURANCE_WEIGHT", float,
      "How much we prefer handcuffing our own starters over holding other teams' backups.",
      "1.0 means a handcuff and an equal-value lottery ticket are worth the same. "
      "Above 1.0 protects the roster we have; below 1.0 chases other people's "
      "upside. This is the main strategic dial in bench valuation.",
      bounds=(0.5, 3.0)),
    S(DRAFT_PREP, "robo.bench", "TALENT_KEEP", float,
      "What a backup produces once he actually has the starting role.",
      "Kept deliberately separate from INHERIT_P. Collapsing the two into one "
      "number quietly asserted that a listed backup definitely inherits the job, "
      "and made every ticket worth about twice what it really was.",
      bounds=(0.2, 1.0)),
    S(DRAFT_PREP, "robo.bench", "INHERIT_P", dict,
      "Chance this player gets the role, given the job actually opens. Keyed by depth-chart slot.",
      "Where the depth chart is weakest and where we were most overconfident: if "
      "the starter goes down the job might split into a committee, or the team "
      "signs someone on Tuesday. Being listed second is weak evidence, not a claim.",
      bounds=(0.0, 1.0)),
    S(DRAFT_PREP, "robo.bench", "BUZZ_LIFT", float,
      "Most probability the live trending feed can add to a backup's chance of a role.",
      "Capped, and only ever applied to a player who is ALREADY a listed backup "
      "with a real projection. The raw feed is noisy -- a kicker and a team "
      "defence sit in its top twenty -- so it adjusts a case the depth chart "
      "already supports rather than inventing one.",
      bounds=(0.0, 1.0)),
    S(DRAFT_PREP, "robo.bench", "BYE_HOLE_COST", float,
      "Points we treat a bye-week hole as costing, per hole.",
      "Deliberately small: a hole is only as expensive as the gap to whoever we "
      "would stream instead. Set to 12 this stopped being a tiebreaker and became "
      "the driver -- it drafted a QB3 we would never start, twice, purely to cover "
      "weeks a waiver pickup covers for free.",
      bounds=(0.0, 20.0), unit="points"),
    S(DRAFT_PREP, "robo.bench", "MISS_RATE", dict,
      "Chance the starter at each position misses time that matters over a season.",
      "Drives how valuable insurance is at all. Running backs get hurt and get "
      "benched; quarterbacks mostly play. Raise these and every handcuff looks "
      "more attractive.",
      bounds=(0.0, 1.0)),
    S(DRAFT_PREP, "robo.bench", "HURT_BUMP", dict,
      "Extra chance the job opens when the starter is already carrying an injury tag.",
      "Someone already hurt is likelier to open the door. Keyed by Sleeper's "
      "injury_status strings, so a key that does not match one of those is simply "
      "never used.",
      bounds=(0.0, 1.0)),
    S(DRAFT_PREP, "robo.bench", "AGE_CLIFF", dict,
      "Age past which each position starts breaking down.",
      "Combined with AGE_RISK to add risk per year beyond it. Blunt, and meant to "
      "be: it is a nudge on top of projections, not an aging curve.",
      bounds=(20, 45), unit="years"),
    S(DRAFT_PREP, "robo.bench", "AGE_RISK", float,
      "Added injury risk per year of age past the cliff.",
      "Small by design. Set high, a 30-year-old receiver becomes uninsurable and "
      "the bot starts handcuffing players who are perfectly healthy.",
      bounds=(0.0, 0.25)),
    S(DRAFT_PREP, "robo.bench", "MAX_DEPTH_TO_INHERIT", dict,
      "How far down a depth chart still counts as next in line. Per position, "
      "because WR is charted differently: Sleeper ranks the whole receiver room "
      "in one list and keeps the alignment (LWR/RWR/SWR) in a field this model "
      "does not read, so the man who takes over the slot can sit at order 5.",
      "Beyond this, somebody above him inherits the job, not him. Without any cap, "
      "ANY order >= 2 was priced as the direct backup and a WR8 was valued as if "
      "he stood to inherit the WR1 role. Raise WR and you catch real second-string "
      "receivers at other alignments, but you also catch genuinely buried ones -- "
      "INHERIT_P prices orders 4 and 5 low (0.15, 0.10) for exactly that reason. "
      "RB/TE/QB charts ARE flat, so 3 means what it says there; raising them just "
      "invents heirs.",
      bounds=(1, 10), danger=True),
    S(DRAFT_PREP, "robo.bench", "STARTER_DEPTH", dict,
      "How far down a chart a player still counts as a starter rather than a backup.",
      "depth_chart_order is per position group, so order 2 at receiver means "
      "'second receiving option' -- a full-time starter whose projection already "
      "reflects the role. Without this, Tee Higgins was priced as a lottery ticket "
      "on Ja'Marr Chase's job ON TOP OF his own WR2 projection.",
      danger=True),
    S(DRAFT_PREP, "robo.bench", "BENCH_QUOTA", dict,
      "What the bench should CONTAIN by the end: a QB3, lottery tickets, a rookie.",
      "Without quotas the maths is locally sensible and globally bland -- it fills "
      "seven slots with the highest-scoring available body and lands a bench that "
      "cannot win a week even if everything breaks right.",
      bounds=(0, 6)),
    S(DRAFT_PREP, "robo.bench", "QUOTA_BONUS", float,
      "Nudge applied when a bench quota is still unmet, scaled up as picks run out.",
      "Big enough to beat a marginal alternative, not big enough to buy a bad "
      "player early. Raise it and the bot reaches for quota-fillers in rounds "
      "where it should still be taking the best player.",
      bounds=(0.0, 200.0), unit="points"),
    S(DRAFT_PREP, "robo.bench", "QUOTA_MIN_VALUE", float,
      "Floor a player must clear before he can satisfy a quota at all.",
      "A quota is a description of a useful bench, not a shopping list. Without "
      "this floor the bot bought the LABEL: a 'ticket' behind a starter whose role "
      "is worth nothing scores p x 0, and the bonus alone put him on the roster.",
      bounds=(0.0, 60.0), unit="points"),
    S(DRAFT_PREP, "robo.bench", "ROOKIE_BUZZ_MIN", float,
      "How much market attention a rookie needs before he counts for the rookie quota.",
      "'Flashed in camp', operationalised. We cannot read a beat writer, but 100k "
      "people who did leave a trace. Raise it and the rookie quota goes unfilled; "
      "lower it and any rookie counts.",
      bounds=(0.0, 1.0)),
    S(DRAFT_PREP, "robo.bench", "COVER_DISCOUNT", float,
      "How much to discount a player who already owns a small role.",
      "He has no ceiling to unlock -- he is what he is. Worth having, worth less "
      "than the same points of upside.",
      bounds=(0.0, 1.0)),
    S(DRAFT_PREP, "robo.bench", "STARTERS", dict,
      "Dedicated starting slots per position, for bench valuation only. QB 2 is "
      "the QB + SUPER_FLEX. The RB/WR/TE FLEX is deliberately NOT counted here, "
      "which is why this sums to 7 and not the league's 8 skill starters.",
      "STRUCTURAL -- the league's format, not a preference. The flex is left out "
      "on purpose in the two places this is used: bye_holes() calls itself 'a "
      "floor on the damage', and p_need_at() caps injury exposure at dedicated "
      "slots. Both understate rather than overstate, which is the safe direction "
      "-- BYE_HOLE_COST had to be cut because bye coverage was driving picks. Add "
      "the flex here and you re-inflate both. The DRAFT policy handles the flex "
      "separately and correctly (draft_agent.FLEX_POS); this is not it.",
      danger=True),
    S(DRAFT_PREP, "robo.buzz", "WINDOW_HOURS", int,
      "How many hours of trending adds/drops count as 'the market right now'.",
      "Long enough to smooth a slow news day, short enough to still be news. "
      "Depth charts and ADP lag camp news by weeks; this is the fast signal, so "
      "widening it throws away the reason it exists.",
      bounds=(6, 336), unit="hours"),
    S(DRAFT_PREP, "robo.buzz", "STALE_AFTER", int,
      "How old the cached buzz file may get before it is refetched.",
      "The daily refresh pulls it anyway; this only matters for ad-hoc runs. Set "
      "very low and every board rebuild hits Sleeper.",
      bounds=(300, 86400), unit="seconds"),
    S(ROSTER, "robo.scout", "POOL_WIRE", int,
      "How far down the waiver wire the scout reads news, by rest-of-season value.",
      "Every rostered player in the league is read regardless -- the trade "
      "evaluator needs the other side of a deal priced as well as ours -- as is "
      "anyone who cannot play. So this only trims the tail of the WIRE, players "
      "a claim would never reach anyway, and it is not the main cost dial: the "
      "league's own roster count is.",
      bounds=(10, 250), unit="players"),
    S(ROSTER, "robo.scout", "K_PUZZLE", float,
      "Calibration residual above which a player is read as a role the model cannot see.",
      "expected.py's k: 1.0 means the market agrees with our model of his role, "
      "3 or 4 means it is paying for a job he does not hold yet. Lower this and "
      "the scout reads ordinary noise as a coming handoff.",
      bounds=(1.2, 6.0)),
    S(ROSTER, "robo.scout", "NEWS_LIMIT", int,
      "How many news items per player the scout reads.",
      "More context per verdict, more tokens per player. The items come back "
      "newest-first, so a low number is recent news and a high number is history.",
      bounds=(1, 25), unit="items"),
    S(ROSTER, "robo.scout", "TRUST_LIFT", dict,
      "How much a scout verdict multiplies a player's bench value at the extremes.",
      "Deliberately modest: a model reading a paragraph is one input among the "
      "projection, the role and the market, and it must not be able to overturn "
      "all three. Widen this and one bad read moves the roster.",
      bounds=(0.1, 3.0)),
    S(ROSTER, "robo.moves", "NOISE_MULTIPLE", float,
      "How many standard errors a simulated gain must clear before it counts.",
      "The simulator reports the paired standard error of every option, and a "
      "difference inside its own noise is not a ranking. Set this to 0 and the "
      "bot starts making moves a coin flip would have made; raise it and it "
      "waits for gaps the simulation can actually resolve.",
      bounds=(0.0, 6.0), unit="std errors"),
    S(ROSTER, "robo.moves", "SHORTLIST", int,
      "How many candidates per channel get priced by simulation.",
      "Everything above this is ranked cheaply and only the top of each channel "
      "is simulated, because each priced option costs a full set of drawn "
      "seasons. Raise it and the Tuesday job gets slower for candidates a claim "
      "would never reach.",
      bounds=(3, 40), unit="players"),
    S(ROSTER, "robo.marginal", "HIT_POINTS", float,
      "Season points a bench player must move us by before he counts as having mattered.",
      "Measured, not chosen: 7.0 is the median realised starting contribution of "
      "this league's own 1,861 completed adds. 36% of adds here never start a "
      "single game and the ninetieth percentile returns 62, so most adds doing "
      "nothing is the normal shape rather than a failure. Raise this and the bot "
      "stops crediting the tail that is the only reason to spend a bench spot.",
      bounds=(1.0, 60.0), unit="points"),
    S(ROSTER, "robo.marginal", "SIMS", int,
      "How many seasons are simulated to price a roster move.",
      "Every hypothetical is scored against the SAME drawn worlds, so this buys "
      "precision on a difference rather than on a total and a few hundred goes a "
      "long way. The standard error is printed beside every number; if a gap you "
      "care about sits inside its own +/-, raise this rather than trusting it.",
      bounds=(25, 5000), unit="seasons"),
    S(ROSTER, "robo.marginal", "CONTENDER_ODDS", float,
      "Playoff odds above which the bot optimises the mean instead of the upside.",
      "A contender wants the safe bench; a long-shot needs the variance that wins "
      "a league. This is bench.py's INSURANCE_WEIGHT expressed as an objective "
      "rather than a weight, and driven by the odds ros.week_weights already uses.",
      bounds=(0.0, 1.0)),
    S(ROSTER, "robo.injuries", "MAX_AGE_H", float,
      "How stale the ESPN injury feed may be before the valuation ignores it.",
      "Sized to the daily refresh with room for one missed run. Too low and one "
      "failed pull sends the eligibility floor back to being inferred from "
      "Sleeper's projection, which is what priced men for weeks they are barred "
      "from playing. Too high and a designation a week old is treated as today's.",
      bounds=(6.0, 168.0), unit="hours"),
    S(DRAFT_PREP, "robo.rankings", "ECR_WEIGHT", float,
      "How much the expert field counts against our own projection when choosing "
      "between two players for the same starting slot.",
      "0.0 is projection alone, which is what the agent did until 30 Aug 2026 -- a "
      "0.7-point projection edge was enough to take a quarterback the field ranked "
      "15th over one it ranked 3rd. 1.0 is the experts' order outright, with our "
      "projections supplying only the magnitudes. The experts' ranks are converted "
      "INTO points first, by permuting each position's own projection curve into "
      "ECR order, so nothing is ever compared across scales and cross-position "
      "value survives. Where projection and consensus agree it changes nothing.",
      bounds=(0.0, 1.0)),
    S(DRAFT_PREP, "robo.rankings", "REPLACEMENT_RANK", dict,
      "The rank at each position treated as replacement level, for VORP.",
      "STRUCTURAL -- derived from the league's starting requirements (2QB leagues "
      "start ~20-22 QBs). Change these and every player's value shifts at once, "
      "including relative value ACROSS positions, which is what the board is for.",
      danger=True),

    # ---------------- live draft ----------------
    S(LIVE_DRAFT, "robo.draft_agent", "STATUS_REFRESH_SECS", int,
      "How often injury status is re-pulled during the draft.",
      "The board bakes injury_status in when it is built and the player dump "
      "caches for 24h, so without this a knee blown out on Saturday night is "
      "invisible at Sunday's 3pm draft -- the one failure that cannot be undone "
      "afterwards.",
      bounds=(60, 3600), unit="seconds"),
    S(LIVE_DRAFT, "robo.draft_agent", "P_GONE_TAKE", float,
      "How likely a player must be to disappear before we take him now.",
      "The take-now threshold. Lower and the bot reaches for players who would "
      "have lasted; higher and it keeps losing the ones it wanted to come back to.",
      bounds=(0.05, 0.95)),
    S(LIVE_DRAFT, "robo.draft_agent", "BENCH_WEIGHT", float,
      "How much bench value counts against starter value when choosing a pick.",
      "Raise it and the bot spends early picks on upside it will not start; drop "
      "it to zero and it never takes a handcuff or a lottery ticket at all.",
      bounds=(0.0, 1.5)),
    S(LIVE_DRAFT, "robo.draft_agent", "START_WEIGHT", dict,
      "Marginal lineup value of the Nth player at each position.",
      "The shape of positional need. Index 0 is the first player at that position, "
      "and the drop-off is what stops the bot taking a fourth running back while "
      "its tight end slot is empty.",
      danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "MAX_AT_POS", dict,
      "Hard ceiling on how many players we will roster at each position.",
      "A safety rail on the pick policy. Set a position to 0 and the bot will "
      "never draft one, which in a 2QB league is how you forfeit.",
      bounds=(0, 20), danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "MIN_AT_POS", dict,
      "Minimum viable roster the bot must be able to complete.",
      "STRUCTURAL. Drives the endgame: as picks run out the policy reserves them "
      "to satisfy these. Wrong values here and the draft ends with an illegal or "
      "unstartable roster.",
      bounds=(0, 20), danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "STARTER_NEEDS", dict,
      "The starting slots the pick policy plans to fill.",
      "STRUCTURAL -- this is the league's lineup, not a preference. It is what the "
      "whole plan-across-remaining-picks calculation is solving for.",
      danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "QUEUE_DEPTH", int,
      "How deep the autopick fallback queue is built.",
      "Sleeper autopicks from our queue before its own ADP, so this is what "
      "happens if the agent dies mid-draft. Deeper is free and the fallback should "
      "never run dry.",
      bounds=(5, 700), unit="players"),
    S(LIVE_DRAFT, "robo.draft_agent", "QUEUE_REFRESH_SECS", int,
      "How often the fallback queue is rebuilt and pushed.",
      "Refreshed off the clock, never on it: a SET QUEUE BLOCKS LIVE PICKS. "
      "Sleeper autopicks from the queue the moment our clock opens and rejects the "
      "pick the agent submits a second later.",
      bounds=(10, 600), unit="seconds"),
    S(LIVE_DRAFT, "robo.draft_agent", "QUEUE_MAX_AT_POS", dict,
      "Hard ceiling on what the fallback queue may contain, per position.",
      "A frozen queue cannot know we already drafted three quarterbacks. Without "
      "an absolute ceiling a contingency draft ended round 9 with FIVE QBs and one "
      "receiver.",
      bounds=(0, 99), danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "HEARTBEAT_STALE_SECS", int,
      "How long the draft guard waits before deciding the agent is wedged.",
      "Checking that a process exists is not a liveness check -- a hung agent "
      "satisfies it forever. 90s is generous: the off-clock injury refresh pulls "
      "16MB and can stall a loop for ~30s, and a false restart costs one pick.",
      bounds=(30, 600), unit="seconds"),
    S(LIVE_DRAFT, "robo.draft_agent", "BAD_STATUS", tuple,
      "Injury statuses that make a player undraftable.",
      "STRUCTURAL-ish. Doubtful counts as undraftable on purpose. Remove entries "
      "and the bot will happily spend a pick on someone who is out for the season.",
      danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "TEAMS", int,
      "Teams in the league.",
      "STRUCTURAL -- a fact about the league, not a setting. Wrong and every pick "
      "number, snake turn and P(gone) calculation is wrong with it.",
      bounds=(2, 32), danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "ROUNDS", int,
      "Rounds in the draft.",
      "STRUCTURAL. Determines how many picks the policy plans across. Wrong and "
      "it either hoards for picks that do not exist or runs out of roster.",
      bounds=(1, 30), danger=True),
    S(LIVE_DRAFT, "robo.draft_agent", "KEEPER_ROUNDS", tuple,
      "Rounds forfeited to our own keepers.",
      "STRUCTURAL -- we keep Omarion Hampton (R3) and Nico Collins (R2), so those "
      "picks do not exist. Wrong and the policy plans around picks it will never "
      "get to make.",
      danger=True),

    # ---------------- weekly lineup ----------------
    S(LINEUP, "robo.lineup", "MIN_GAIN_TO_CHANGE", float,
      "How much a reshuffle must be worth before the lineup is rewritten.",
      "Every write is a public decision-log entry, so a lineup that churns by "
      "0.1 points twice a day reads as indecision and buries the changes that "
      "mattered. LEGALITY IGNORES THIS: a bye or injured-out starter is always "
      "replaced, however small the gain, because the threshold exists to stop "
      "churn and must never end up protecting a guaranteed zero.",
      bounds=(0.0, 20.0), unit="projected points"),
    S(LINEUP, "robo.lineup", "RESPECT_LOCKS", bool,
      "Leave a slot alone once that player's game has kicked off.",
      "The league sets bench_lock=1, so players freeze individually at their own "
      "kickoff rather than at one weekly deadline. Turn this off and a Sunday "
      "afternoon run tries to rewrite a half-locked lineup, which Sleeper "
      "rejects -- leaving no way to know which half landed. Off is for testing "
      "only."),
    S(LINEUP, "robo.lineup", "NEVER_START", set,
      "Injury designations that disqualify a player from starting.",
      "Separate from what may go on IR, which this league restricts further. A "
      "Questionable player is startable and NOT IR-eligible; an Out player is "
      "neither. Removing an entry here means the optimizer will happily start "
      "someone who cannot play if he out-projects the alternative."),
    S(LINEUP, "robo.model_proj", "USE_MODEL", bool,
      "Which engine prices the weekly lineup.",
      "On, the projection is the NFL Model's simulated mean -- 4,000 stat "
      "lines scored under all 57 of our keys. Off, it is Sleeper's weekly "
      "number, which carries 23 of them and has never included a "
      "quarterback's sack penalty or any bonus tier. Off is the rollback if "
      "the model starts producing something strange mid-season; the lineup "
      "still gets set either way."),
    S(LINEUP, "robo.model_proj", "MAX_AGE_H", float,
      "How old the model's artifact may be before the lineup ignores it.",
      "Too high and a Sunday afternoon lineup runs on a Tuesday simulation "
      "that never heard about a Saturday scratch. Too low and one failed "
      "export benches the model for the rest of the week. Sized to the DAILY "
      "export, so the pre-kickoff refreshes tighten it in practice without "
      "this having to know the kickoff schedule.",
      bounds=(1.0, 168.0), unit="hours"),
    S(LINEUP, "robo.lineup", "SLOTS", list,
      "The starting lineup's slots, in Sleeper's order.",
      "STRUCTURAL -- a fact about the league. The submitted starters array is "
      "positional, so a wrong order does not error: it silently files each "
      "player under the wrong slot and the lineup is quietly illegal.",
      danger=True),

    # ---------------- roster manager ----------------
    S(ROSTER, "robo.ir", "IR_ENABLED", bool,
      "Whether the bot moves injured players to reserve on its own.",
      "The one roster move that needs no valuation -- eligibility is written in "
      "the league settings, not inferred. Off means it still reports what it "
      "would do and changes nothing. It never fills the slot it frees; signing "
      "somebody is a separate decision the bot cannot make yet."),
    S(ROSTER, "robo.moves", "MIN_GAIN_TO_ADD", float,
      "How much better a candidate must be than the man he replaces.",
      "In rest-of-season points, comparing what a candidate is worth (`mean`) "
      "against what the man he replaces would cost us (`hold`). Lower it and the "
      "bot churns the bottom of the roster for fractional gains; raise it and it "
      "sits out real upgrades. Ignored in `patch` mode, where an empty starting "
      "slot scores zero and anyone startable beats it.",
      bounds=(0.0, 200.0), unit="points"),
    S(ROSTER, "robo.moves", "DROP_FLOOR", float,
      "Never cut anyone whose drop price is above this.",
      "NOW A BACKSTOP AND LITTLE ELSE. It existed to stop a broken valuation "
      "cutting a genuine starter, and the simulator answers that directly: a real "
      "starter prices at 60 to 175 points to drop and a spare part at 0 to 4, "
      "with a standard error under half a point. The guard doing the work is the "
      "hard rule that nobody in the current optimal lineup is droppable. THE "
      "UNITS CHANGED UNDERNEATH IT and that made it weaker, not stricter: drop "
      "prices now run 0.4 to 120 where hold values ran 0.4 to 302, so a floor of "
      "120 that used to exclude our top six men now excludes roughly one. Left "
      "there deliberately, because the starter rule is the guard that matters and "
      "a second one tuned in stale units would be worse than none.",
      bounds=(0.0, 500.0), unit="points"),
    S(ROSTER, "robo.moves", "MAX_SLOTS_TO_TURN_OVER", int,
      "How many roster spots one waiver run may change.",
      "Caps SLOTS, never claims. A losing claim costs nothing in FAAB -- no "
      "penalty, and no rolling priority to burn -- so capping the number of "
      "claims would throw away free optionality for no benefit. What is worth "
      "limiting is how much of the roster actually turns over.",
      bounds=(0, 6), unit="roster spots"),
    S(ROSTER, "robo.moves", "SLATE_DEPTH", int,
      "How deep each slot's waiver priority list goes.",
      "Several claims naming the SAME drop form a priority list: Sleeper works "
      "them in seq order, the first winner takes the slot, and the rest bounce "
      "off a player no longer on our roster at zero cost. A one-deep slate is "
      "the failure mode here, not a long one.",
      bounds=(1, 20), unit="claims"),
    S(ROSTER, "robo.faab", "MAX_SINGLE_BID_PCT", float,
      "Hard ceiling on one bid, as a share of the budget still unspent.",
      "Stops a single week emptying the season's budget. Bid pricing moved out "
      "of moves.py into faab.py when it stopped being an invented formula and "
      "started being this league's own 1,032 recorded bids, and this dial moved "
      "with it.",
      bounds=(0.0, 1.0)),
    S(ROSTER, "robo.faab", "MIN_POINTS_PER_DOLLAR", float,
      "What a FAAB dollar is worth in rest-of-season points, at an even pace.",
      "THE BID POLICY IN ONE NUMBER -- it decides where on the win-probability "
      "curve we stop. The first dollar buys about ten points of win probability "
      "and everything past $10 buys a fraction of one, so lowering this bids "
      "harder into a steeply flattening curve. The budget genuinely is scarce "
      "here (median team spends $89 of $100, 24 of 55 team-seasons exhausted "
      "it) and it expires worthless, which is why the price is paced rather "
      "than fixed -- see PACE_BOUNDS.",
      bounds=(0.0, 20.0), unit="points per FAAB dollar"),
    S(ROSTER, "robo.faab", "PACE_BOUNDS", tuple,
      "How far the budget pace may push the price of a dollar, (min, max).",
      "Unclamped, a team that had spent nothing by week 14 would price dollars "
      "near zero and empty the budget on the first player it saw; a team that "
      "had spent everything would price them so high it stopped bidding at all.",
      bounds=(0.0, 20.0)),
    S(ROSTER, "robo.faab", "MIN_LIVE_BID", int,
      "The least we will offer on a claim we actually want.",
      "A $0 claim is a real claim in this league and plenty have won, so this is "
      "not about validity -- it is a dollar of separation from everyone who left "
      "the field blank. Raising it spends budget on rungs that were converting "
      "anyway.",
      bounds=(0, 10), unit="FAAB"),
    S(ROSTER, "robo.moves", "ROS_MOVE_BLACKOUT_H", float,
      "Hours before kickoff when long-horizon roster moves stop.",
      "A rest-of-season swap made an hour before the early games is this week's "
      "panic with the season's consequences, and nothing about it could not have "
      "waited for Tuesday. Only `ros` and `block` are held; `patch` still runs, "
      "because an unfillable starting slot is the emergency the hour justifies. "
      "Set to 0 to remove the brake entirely.",
      bounds=(0.0, 48.0), unit="hours"),
    S(ROSTER, "robo.moves", "BYE_LOOKAHEAD_WEEKS", int,
      "How far ahead to look for a week we cannot field a legal lineup.",
      "Raising it pre-empts byes earlier at the cost of holding cover we may not "
      "need; the wire turns over, so cover bought five weeks out is often wasted.",
      bounds=(0, 6), unit="weeks"),
    S(ROSTER, "robo.moves", "BLOCK_MIN_DENY", float,
      "How much a free agent must improve an OPPONENT before denying him.",
      "Blocking is the third priority and buys us nothing directly -- the gain is "
      "purely somebody else's loss, priced on a bench we can only estimate. Set "
      "it low and the bot spends roster spots on players it will never start.",
      bounds=(0.0, 300.0), unit="points"),
    S(ROSTER, "robo.moves", "BLOCK_MAX_BID", int,
      "The most FAAB a purely defensive claim may spend.",
      "Denial is worth a roster spot occasionally and real budget almost never. "
      "Budget spent here is budget missing when our own need appears.",
      bounds=(0, 50), unit="FAAB"),
    S(ROSTER, "robo.ros", "UPSIDE_WEIGHT", float,
      "How hard the rising-role premium protects a player from being dropped.",
      "THE ROOKIE-HOLD DIAL. At 0 an add and a drop are priced off the same "
      "number and the bot will cut a breakout-in-waiting for any established "
      "veteran; at 2 it hoards lottery tickets it will never start. Only affects "
      "drops -- an add is always judged on what a man is worth now.",
      bounds=(0.0, 3.0)),
    S(ROSTER, "robo.ros", "NEWS_APPLY_FUTURE", float,
      "How much of a scout news verdict reaches the FUTURE weeks.",
      "PROVISIONAL AT 0.5 pending evidence. Sleeper demonstrably reprices the "
      "CURRENT week on news; whether it reprices week 9 has never been observed. "
      "At 1.0 a feed that does reprice counts the same injury twice; at 0.0 a "
      "feed that does not carries no news at all. `python -m robo.projarchive "
      "--diff` is collecting the answer.",
      bounds=(0.0, 1.0)),
    S(ROSTER, "robo.expected", "K_CLAMP", tuple,
      "Bounds on the residual between the structural model and the market.",
      "k is the part of a man's market value the availability-and-role model "
      "does NOT explain, and a large one is information rather than an error: "
      "Carson Beck reads 3.99 because the market prices a handoff no hazard "
      "rate knows about, which is the whole signal this module was built to "
      "find. Set narrow and that signal is clamped away; set absurdly wide and "
      "one stale season row can move a valuation by an order of magnitude. "
      "16 of 785 players clamp at (0.25, 6.0).",
      bounds=(0.05, 20.0)),
    S(ROSTER, "robo.expected", "MIN_RAW_TO_SCALE", float,
      "Structural total below which a player is priced off the season file alone.",
      "A man the model gives almost nothing has no per-week SHAPE for the "
      "market's level to scale, and dividing by it explodes on rounding noise "
      "instead of on information. Below this he gets the season projection "
      "spread evenly over the games left, flagged `season-only` -- which is how "
      "37 players, Michael Penix at 141.5 among them, stop pricing at 0.00. "
      "Raise it and more men fall back to a flat line; lower it and the "
      "residual starts amplifying noise.",
      bounds=(0.0, 25.0), unit="points"),
    S(ROSTER, "robo.returns", "MIN_EVENTS", int,
      "Spells a body part needs before it gets its own return curve.",
      "Same discipline as roles.MIN_EVENTS. Below this the curve is noise and "
      "falls back to the pooled one across all 2,301 spells; 15 body parts "
      "clear it. Lower it and a handful of freak injuries start setting a "
      "return date for everyone who shares the label.",
      bounds=(5, 200), unit="spells"),
    S(ROSTER, "robo.returns", "MAX_WEEKS", int,
      "How far out the return curves are carried.",
      "Past this the men still out are a different population -- season-ending "
      "injuries -- and a rest-of-season sum has nothing left to spend on them. "
      "Extending it does not add information, it adds a tail fitted on the "
      "handful of spells that reached it.",
      bounds=(4, 18), unit="weeks"),
    S(ROSTER, "robo.playoffs", "WEEKLY_SD", float,
      "Week-to-week scoring spread used to simulate playoff odds.",
      "This league's own within-team standard deviation, 23.8 across 72 "
      "team-seasons. Do NOT set it to the spread of all scores (26.8): that also "
      "contains the real gap between good and bad teams, which the projected "
      "lineups already carry, and using it would count team quality twice.",
      bounds=(1.0, 60.0), unit="points"),
    S(ROSTER, "robo.vegas", "MIN_COVERAGE", float,
      "Share of a week's games that must be priced before it is planned around.",
      "Books post a week or two out, not a season. Below this the best available "
      "defence is really the best of whichever few games the book happened to "
      "post first, which is a different question.",
      bounds=(0.0, 1.0)),
    S(ROSTER, "robo.season", "ROSTER_MAX", int,
      "Active roster size.",
      "STRUCTURAL -- 10 starting slots plus 7 bench. Wrong and every add looks "
      "illegal, so the bot simply stops making moves without ever erroring. "
      "season.audit() compares this against Sleeper and reports drift.",
      bounds=(1, 40), danger=True),
    S(ROSTER, "robo.season", "IR_SLOTS", int,
      "Reserve slots, on top of the active roster.",
      "STRUCTURAL. These are extra bodies, not part of ROSTER_MAX -- which is "
      "why parking an injured player frees a spot rather than just relabelling "
      "one.",
      bounds=(0, 10), danger=True),
    S(ROSTER, "robo.season", "FAAB_BUDGET", int,
      "Season-long free-agent budget.",
      "STRUCTURAL -- read off the league, not chosen. Remaining budget is "
      "computed as this minus what Sleeper says we have spent, so a wrong value "
      "here makes every bid ceiling wrong.",
      bounds=(0, 1000), danger=True),
    S(ROSTER, "robo.season", "WAIVER_CLEAR_DAYS", int,
      "How long a dropped player sits on waivers before anyone can just take him.",
      "STRUCTURAL. Decides which channel a player belongs to: too long and the "
      "bot bids FAAB on someone it could have had free; too short and it tries "
      "to take someone who is still locked.",
      bounds=(0, 7), unit="days", danger=True),
]

BY_KEY = {s.key: s for s in REGISTRY}


def by_module(module: str) -> list[S]:
    return [s for s in REGISTRY if s.module == module]


def by_section(section: str) -> list[S]:
    return [s for s in REGISTRY if s.section == section]


# --------------------------------------------------------------------------
# storage

def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load() -> dict:
    """Current user overrides. Sparse: only what differs from default."""
    d = _read(OVERRIDES, {})
    return d if isinstance(d, dict) else {}


def defaults() -> dict:
    """The revert checkpoint, captured from the code on first ever run."""
    d = _read(DEFAULTS, {})
    return d if isinstance(d, dict) else {}


def _record_default(key: str, value) -> None:
    """Write a default exactly once. Never overwritten afterwards: that is what
    makes it a checkpoint rather than a mirror of the current state."""
    d = defaults()
    if key in d:
        return
    d[key] = value
    try:
        DATA.mkdir(exist_ok=True)
        DEFAULTS.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def coerce(spec: S, value):
    """Return value as the spec's type, or raise ValueError.

    JSON has no tuples and its object keys are always strings, so a tuple comes
    back as a list and {"QB": 3} round-trips fine but {2: 0.55} does not -- the
    integer key returns as "2". Both are repaired here rather than at every use.
    """
    if spec.type is tuple:
        if not isinstance(value, (list, tuple)):
            raise ValueError("expected a list")
        return tuple(value)
    if spec.type is dict:
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    if spec.type in (set, frozenset, list):
        # JSON has neither a set nor a distinction between them, so all three
        # arrive as an array. apply() hands the result to _restore_container(),
        # which puts back whatever kind the CODE had. Rejecting a non-array here
        # matters: set(5) raises inside apply() where nothing catches it, which
        # would take the whole module down over one bad line of JSON.
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("expected a list")
        return list(value)
    if spec.type is bool:
        if not isinstance(value, bool):
            raise ValueError("expected true or false")
        return value
    if spec.type is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        if isinstance(value, float) and value != int(value):
            raise ValueError("expected a whole number")
        return int(value)
    if spec.type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        return float(value)
    if spec.type is str:
        if not isinstance(value, str):
            raise ValueError("expected text")
        return value
    return value


def validate(spec: S, value) -> tuple[bool, str]:
    """(ok, why not). Bounds apply to the values INSIDE a dict, not the dict."""
    try:
        value = coerce(spec, value)
    except ValueError as e:
        return False, str(e)
    if spec.choices and value not in spec.choices:
        return False, "must be one of: " + ", ".join(map(str, spec.choices))
    if spec.bounds:
        lo, hi = spec.bounds
        vals = list(value.values()) if isinstance(value, dict) else \
            (list(value) if isinstance(value, tuple) else [value])
        for v in vals:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue          # a non-numeric member is not bounds-checked
            if not (lo <= v <= hi):
                return False, f"must be between {lo} and {hi} (got {v})"
    return True, ""


def apply(module_name: str, ns: dict) -> list[str]:
    """Override a module's constants from settings.json. See the module docstring.

    Only touches names that ALREADY exist in the namespace, so a stale or
    misspelled key cannot inject a new global. Returns the names it changed.
    """
    changed = []
    over = load()
    for spec in by_module(module_name):
        if spec.name not in ns:
            continue                          # registry is ahead of the code
        _record_default(spec.key, _jsonable(ns[spec.name]))
        if spec.key not in over:
            continue
        ok, why = validate(spec, over[spec.key])
        if not ok:
            print(f"[settings] ignoring {spec.key}: {why}", flush=True)
            continue
        value = coerce(spec, over[spec.key])
        if spec.type is dict:
            value = _restore_keys(ns[spec.name], value)
        value = _restore_container(ns[spec.name], value)
        ns[spec.name] = value
        changed.append(spec.name)
    return changed


def _restore_container(original, loaded):
    """Hand back the same KIND of container the code had.

    KEEPER_ROUNDS and BAD_STATUS are sets in the source; JSON only has arrays, so
    an overridden one would come back a tuple. Today every use is `in`, which
    works either way -- but that makes it a trap, not a non-issue: the first
    `KEEPER_ROUNDS - filled` anyone writes would work perfectly until someone
    changed the setting, and then fail only for them.
    """
    if isinstance(original, frozenset):
        return frozenset(loaded)
    if isinstance(original, set):
        return set(loaded)
    return loaded


def _restore_keys(original, loaded: dict):
    """JSON stringifies object keys; put integer keys back if that is what the
    code had. bench.INHERIT_P is keyed {2: .., 3: ..} and would silently stop
    matching any depth-chart slot otherwise."""
    if not isinstance(original, dict) or not original:
        return loaded
    if all(isinstance(k, int) for k in original):
        out = {}
        for k, v in loaded.items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                out[k] = v
        return out
    return loaded


def _jsonable(v):
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return v


def save(key: str, value, note: str = "") -> tuple[bool, str]:
    """Set one override, or clear it if the value equals the checkpoint default."""
    spec = BY_KEY.get(key)
    if not spec:
        return False, f"unknown setting {key}"
    ok, why = validate(spec, value)
    if not ok:
        return False, why
    over = load()
    old = over.get(key, defaults().get(key))
    value = _jsonable(coerce(spec, value))
    if value == defaults().get(key):
        over.pop(key, None)          # back at default: stop carrying an override
    else:
        over[key] = value
    DATA.mkdir(exist_ok=True)
    OVERRIDES.write_text(json.dumps(over, indent=1, sort_keys=True), encoding="utf-8")
    _log(key, old, value, note)
    return True, ""


def revert(key: str) -> None:
    over = load()
    if key in over:
        old = over.pop(key)
        OVERRIDES.write_text(json.dumps(over, indent=1, sort_keys=True), encoding="utf-8")
        _log(key, old, defaults().get(key), "revert")


def revert_all() -> int:
    """Back to the starting-state checkpoint. Clears every override at once."""
    over = load()
    n = len(over)
    for key, old in over.items():
        _log(key, old, defaults().get(key), "revert all")
    OVERRIDES.write_text("{}", encoding="utf-8")
    return n


def _log(key, old, new, note) -> None:
    try:
        with HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(),
                                "human": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "key": key, "old": old, "new": new,
                                "note": note}) + "\n")
    except OSError:
        pass


def ensure_defaults() -> int:
    """Import every registered module once so the checkpoint is complete.

    Defaults are captured by apply() at import time, so on a fresh machine a tool
    that reads the registry without importing the modules would see an empty
    checkpoint and have nothing to revert to. Returns how many values are held.
    """
    import importlib
    for module in sorted({s.module for s in REGISTRY}):
        try:
            importlib.import_module(module)
        except Exception as e:
            print(f"[settings] could not import {module}: {e}", flush=True)
    return len(defaults())


def current(spec: S):
    """What this setting is right now: the override if any, else the checkpoint."""
    over = load()
    if spec.key in over:
        return over[spec.key]
    return defaults().get(spec.key)


def is_overridden(spec: S) -> bool:
    return spec.key in load()


def same_as_current(spec: S, value) -> bool:
    """Is this value what the setting already holds?

    Compared in the JSON form both sides will be stored in, so a tuple and the
    list it round-trips to, or 3 and 3.0, do not read as an edit. Without this
    every Save writes every field and the history file becomes noise.
    """
    try:
        return _jsonable(coerce(spec, value)) == _jsonable(current(spec))
    except ValueError:
        return False


def pending_restart() -> list[S]:
    """Overridden settings whose module must restart before they mean anything."""
    over = load()
    return [s for s in REGISTRY if s.key in over and s.restart]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Roboner tunable settings")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    # Import every registered module first. Defaults are captured by apply() at
    # import time, so without this a freshly registered setting reports None
    # here -- not because it has no default, but because nothing has loaded the
    # module that owns it yet.
    ensure_defaults()
    if args.json:
        print(json.dumps({s.key: {"section": s.section, "default": defaults().get(s.key),
                                  "current": current(s), "overridden": is_overridden(s)}
                          for s in REGISTRY}, indent=1, default=str))
        return
    for section, title, _ in SECTIONS:
        rows = by_section(section)
        if not rows:
            continue
        print(f"\n=== {title} ===")
        for s in rows:
            mark = "*" if is_overridden(s) else " "
            danger = " [structural]" if s.danger else ""
            print(f" {mark} {s.name:<24} {str(current(s))[:44]:<46}{danger}")
    print("\n* = changed from the starting-state checkpoint")


if __name__ == "__main__":
    main()
