"""What a bench pick is actually worth: insurance and lottery tickets.

The starting lineup is an expected-points problem and the DP in draft_agent
handles it. The bench is not. A bench player scores us nothing in the ordinary
case -- he is on the bench -- so ranking bench candidates by their own expected
production, whether raw points or VORP, is answering a question nobody asked.
It is how the bot ended up taking a QB3 it would never start (raw points), and
then, once that was fixed, taking no backup QB at all (VORP).

What a bench player is worth is CONDITIONAL: the chance he gets an opportunity,
times what the role is worth when he does -- and "worth" is measured against the
WAIVER WIRE, not against the last starter. That distinction is this file's
sharpest edge: rankings.vorp uses the last starter, which is right for a board
and wrong here, and using it here scored every receiver still available after
about pick 100 at exactly zero. See waiver_pts(). That splits into two jobs, and a good
bench does both:

  INSURANCE   he backs up someone WE start. If our RB1 goes down we slide his
              handcuff in and lose little. This shrinks variance and is worth
              most when the starter is expensive and fragile.
  LOTTERY     he backs up someone SOMEONE ELSE starts, on an offense where the
              job is worth having. He does nothing until a door opens, and then
              he can win a league. This adds variance, which is what a team
              needs when it is not the favourite.

Both are P(opportunity) x value-of-the-role. The difference is whose starter he
is standing behind, and that is a real strategic dial (INSURANCE_WEIGHT), not a
modelling detail: a contending roster wants insurance, a long-shot wants tickets.

Third job, unrelated to either: BYE COVERAGE. Two quarterbacks who share a bye
week is not a two-quarterback roster. This is pure schedule arithmetic and gets
counted separately from talent.

Depth charts come from Sleeper (`depth_chart_order`, ~93% covered on our board).
Every rate below is a stated assumption, not a fitted number -- tune them here.
"""

from robo import buzz
from robo import sleeper_read as api

# Chance the depth-1 player at a position misses time that matters over a
# season. Running backs get hurt and get benched; quarterbacks mostly play.
MISS_RATE = {"RB": 0.50, "WR": 0.38, "TE": 0.35, "QB": 0.28}
# Added risk per year of age past the point where a position starts breaking.
AGE_CLIFF = {"RB": 27, "WR": 30, "TE": 30, "QB": 35}
AGE_RISK = 0.04
# Two DIFFERENT uncertainties, previously collapsed into one number and thereby
# hidden. TALENT_KEEP was nominally "a backup produces less than the starter",
# but it was silently also asserting that he definitely gets the job.
#
#   INHERIT_P    P(this player gets the role | the job actually opens). This is
#                where the depth chart is weakest and where we were most
#                overconfident: if McCaffrey goes down, the RB2 might take the
#                backfield, or it splits into a committee, or the team signs
#                someone on Tuesday. Being listed second is weak evidence, not a
#                claim on the job.
#   TALENT_KEEP  what he produces IN the role, given he has it.
#
# The product is what a ticket is really worth, and it is roughly half what the
# old single number implied. That is the point: the old value was fiction.
# Orders 4 and 5 exist for RECEIVERS only (see MAX_DEPTH_TO_INHERIT): on a
# flat chart nobody that deep is next in line, but on a receiver chart the
# order-5 man may be the second option at his own alignment. Low and
# declining, because we cannot see WHICH alignment from this field -- it is
# a wider net cast with less confidence, which is the honest way to widen it.
INHERIT_P = {2: 0.55, 3: 0.25, 4: 0.15, 5: 0.10}
TALENT_KEEP = 0.75
# Someone already hurt is likelier to open the door.
HURT_BUMP = {"Questionable": 0.10, "Doubtful": 0.25, "Out": 0.35,
             "IR": 0.45, "PUP": 0.35, "Suspended": 0.30}
# Most probability the live market can add to a backup's chance of a role.
# Applied only to a player who is ALREADY a listed backup with a real
# projection, because the raw trending feed is noisy -- a kicker and a team
# defence sit in its top twenty.
#
# KNOWN FLAW, measured 30 Aug 2026 and deliberately not fixed before that day's
# draft. This is ADDITIVE, so it does NOT "adjust a case the depth chart already
# supports" the way this comment used to claim: on 20 of 29 candidates it
# contributes more probability than the depth chart does, and the thinner the
# case the bigger the relative distortion (a 5th receiver at base 0.038 gets
# +0.222). The root cause is buzz.signal's scaling rather than this constant:
# log1p(n)/log1p(top) puts 56 of 100 tracked players between 0.6 and 0.8, so
# the lift is nearly a flat +0.175 for anyone on the list at all. It has never
# cost a pick in any draft run -- no player carried by it was ever taken -- so
# it is a latent flaw, not an active one. Fix the scaling, not the weight.
BUZZ_LIFT = 0.25
# How much we prefer covering our own starters over holding other teams'
# backups. 1.0 = a handcuff and an equal-value ticket are worth the same.
INSURANCE_WEIGHT = 1.25
# Points a bye-week hole costs us, per hole. Deliberately small: a hole is only
# as expensive as the gap to whoever we would stream instead, and in a 17-round
# league that gap is usually a few points. Set high (12) this was not a
# tiebreaker but the driver -- it drafted a QB3 we would never start, twice,
# purely to "cover" weeks a waiver pickup covers for free.
# It is also small RELATIVE to the waiver-baseline scale, which is why the model
# will trade a guaranteed one-week hole for depth: it took a single tight end in
# every seed until MIN_AT_POS["TE"] was raised to 2. The constraint carries that,
# not this number.
BYE_HOLE_COST = 4.0

STARTERS = {"QB": 2, "RB": 2, "WR": 2, "TE": 1}


# What the bench is supposed to CONTAIN by the end, not just what each pick is
# worth in isolation. Without these the maths is locally sensible and globally
# bland: it fills seven slots with the highest-scoring available body every
# time and lands a bench of four-carry, two-catch, eight-point players who
# cannot win a week even if everything breaks right.
BENCH_QUOTA = {
    "qb3": 1,        # a 2QB league with two QBs is one hamstring from forfeiting
    "ticket": 2,     # somebody else's backup on an offence worth inheriting
    "rookie": 1,     # a first-year skill player the market has actually noticed
}
# Positions a rookie quota can be satisfied at. Not RB-only: a rookie receiver
# who wins a job in camp is the same bet as a rookie back who does, and in this
# format a first-year QB stepping into a starting job is worth more than either.
ROOKIE_POS = ("QB", "RB", "WR", "TE")
# "Flashed in camp", operationalised: a rookie the trending feed has moved on.
# We cannot read a beat writer, but 100k people who did leave a trace.
ROOKIE_BUZZ_MIN = 0.55
# A player who already owns a small role has no ceiling to unlock -- he is what
# he is. Worth having, worth less than the same points of upside.
COVER_DISCOUNT = 0.55
# Nudge applied when a quota is unmet, scaled up as picks run out. Big enough to
# beat a marginal alternative, not big enough to buy a bad player early.
QUOTA_BONUS = 45.0
# A quota is a description of a useful bench, not a shopping list to satisfy.
# Without this floor it bought the LABEL: a "ticket" behind a starter whose role
# is worth nothing scores p x 0, and the bonus alone put him on the roster.
# NOTE: 8.0 was tuned when bench scores ran 0-90. Since bench value moved to a
# waiver baseline they run 0-160, so this now filters far less than it did.
# It still catches a genuine p x 0, which is what it was for.
QUOTA_MIN_VALUE = 8.0


def depth_map(players: dict) -> dict:
    """(team, pos) -> {depth_chart_order: player_id}, starters first."""
    out: dict[tuple, dict] = {}
    for pid, v in players.items():
        team, pos, order = v.get("team"), v.get("position"), v.get("depth_chart_order")
        if not team or not pos or order is None:
            continue
        out.setdefault((team, pos), {}).setdefault(int(order), pid)
    return out


# How far down a depth chart still counts as "next in line". Beyond this a
# player is not inheriting the job if the starter goes down -- somebody above him
# is. Without the cap, ANY order >= 2 was priced as the direct backup, so a WR8
# was valued as if he stood to inherit the WR1 role.
# PER POSITION, because receivers are charted differently from everyone else.
# Sleeper ranks the whole receiver room in one list while carrying the alignment
# (LWR/RWR/SWR) in a separate field this model does not read. So the man who
# actually takes over the slot when the slot starter goes down can sit at global
# order 5 or 6 -- Cincinnati's RWR2 is order 5, behind an order-2 starter -- and
# a flat cutoff of 3 makes him invisible. RB, TE and QB charts are flat (checked:
# their depth_chart_position is just the position), so their order really is
# linear depth and 3 still means what it says.
MAX_DEPTH_TO_INHERIT = {"QB": 3, "RB": 3, "WR": 5, "TE": 3}
DEFAULT_MAX_DEPTH = 3


def max_depth(pos: str) -> int:
    return MAX_DEPTH_TO_INHERIT.get(pos, DEFAULT_MAX_DEPTH)


# ...and how far down a chart a player is still a STARTER. depth_chart_order is
# per position group, so order 2 at receiver means "second receiving option" --
# a full-time starter whose projection already reflects the role -- not a backup.
# Without this, Tee Higgins was priced as a lottery ticket on Ja'Marr Chase's job
# ON TOP OF his own WR2 projection, and the same for every WR2 and TE2 in the
# league. Only players BEYOND these are blocked in the way the model means.
STARTER_DEPTH = {"QB": 1, "RB": 1, "WR": 2, "TE": 1}

# data/settings.json overrides the constants above. Import-time, so a change
# there takes effect on the next run of this module -- see robo/settings.py.
from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())


def ahead_of(r: dict, players: dict, depth: dict) -> str | None:
    """player_id of the man this candidate is standing behind, if any."""
    v = players.get(r["player_id"]) or {}
    order = v.get("depth_chart_order")
    if order is None or int(order) <= 1:
        return None  # he IS the starter; his upside is already in his projection
    if int(order) <= STARTER_DEPTH.get(r["pos"], 1):
        return None  # he already starts; his projection is not suppressed
    if int(order) > max_depth(r["pos"]):
        return None  # too far back for the job to fall to him
    chart = depth.get((v.get("team"), v.get("position"))) or {}
    return chart.get(1)


def p_opportunity(starter_id: str, players: dict) -> float:
    """Chance the job in front of this backup comes open."""
    v = players.get(starter_id) or {}
    pos = v.get("position")
    p = MISS_RATE.get(pos, 0.35)
    age, cliff = v.get("age"), AGE_CLIFF.get(pos, 30)
    if age and age > cliff:
        p += AGE_RISK * (age - cliff)
    p += HURT_BUMP.get(v.get("injury_status") or "", 0.0)
    return min(p, 0.90)


def bye_holes(roster: list[dict]) -> int:
    """Starting slots we cannot legally fill, summed over the season.

    Counts each position independently against its dedicated slots; the flex is
    ignored, so this is a floor on the damage, not the whole of it.
    """
    weeks = {b for r in roster if (b := r.get("bye"))}
    holes = 0
    for pos, need in STARTERS.items():
        have = sum(1 for r in roster if r["pos"] == pos)
        if have < need:
            # not having a tight end yet is not a bye-week problem, it is an
            # unfilled starting slot, and the DP's starter needs already own it.
            # Counting it here too was double-charging, and it dominated: it
            # drafted a TE and a QB purely for "bye coverage" with no path to a
            # role, over players with real conditional value.
            continue
        for wk in weeks:
            available = sum(1 for r in roster if r["pos"] == pos and r.get("bye") != wk)
            holes += max(0, need - available)
    return holes


# How many players at each position are actually ROSTERED in this league, and
# therefore NOT on the waiver wire. 12 teams x 17 rounds = 204 spots; these are
# the counts measured off completed 12x17 drafts of this exact league.
ROSTERED_BY_POS = {"QB": 25, "RB": 63, "WR": 68, "TE": 24, "K": 12, "DEF": 12}

_WAIVER: dict = {}


def waiver_pts(pos: str, board_by_id: dict) -> float:
    """Points of the best player at this position genuinely left on waivers.

    THIS IS NOT rankings.vorp's baseline, and the difference is the whole point.
    rankings sets replacement at the last STARTER -- WR32, RB30 -- which is the
    right basis for a draft board, where VORP measures scarcity against what you
    are forced to start. bench.py asks a completely different question: what does
    holding this man gain over what we could stream instead? For that the
    baseline is the best UNROSTERED player, and 68 receivers and 63 backs are
    rostered in a 12x17 league.

    Using the board's number here was off by 69 points at WR and 97 at RB, and it
    is what made every receiver still available after ~pick 100 score exactly
    zero as bench depth: they all sit below WR32 and so failed a `vorp > 0` gate
    that was really asking "is he better than a startable receiver", not "is he
    better than the waiver wire".
    """
    if _WAIVER.get("_n") != len(board_by_id):
        _WAIVER.clear()
        _WAIVER["_n"] = len(board_by_id)
        bypos: dict = {}
        for row in board_by_id.values():
            bypos.setdefault(row["pos"], []).append(row["proj_pts"])
        for p, lst in bypos.items():
            lst.sort(reverse=True)
            i = min(ROSTERED_BY_POS.get(p, 24), len(lst) - 1)
            _WAIVER[p] = lst[i]
    return _WAIVER.get(pos, 0.0)


def over_waiver(r: dict, board_by_id: dict) -> float:
    """What this player is worth ABOVE the wire. bench.py's version of vorp."""
    return r["proj_pts"] - waiver_pts(r["pos"], board_by_id)


def replacement_pts(r: dict, board_by_id: dict) -> float:
    """Points of the freely-available player at this position. See waiver_pts."""
    return waiver_pts(r["pos"], board_by_id)


def no_value_reason(r: dict, players: dict, board_by_id: dict) -> str:
    """Why a player is worth nothing to OUR bench. Wording only, never a number.

    "No path to a role" used to be the only string here, and it is published
    verbatim to the league's public decision log -- which on 30 Aug 2026 meant
    saying it about Cam Ward, who starts for Tennessee. He has a role. What he
    lacks is value over the quarterback we could stream: he projects 244 against
    a freely-available 295 in this 2QB format, so rostering him gains nothing.
    That is a different claim, and one that does not read as though the bot has
    never heard of him.
    """
    v = players.get(r["player_id"]) or {}
    order = v.get("depth_chart_order")
    behind = replacement_pts(r, board_by_id) - r["proj_pts"]
    if order is not None and int(order) <= STARTER_DEPTH.get(r["pos"], 1):
        if behind < 1:
            return (f"already starts, but projects level with the freely-available "
                    f"{r['pos']}, so he adds nothing we could not stream")
        return (f"already starts, but projects {behind:.0f} below the "
                f"freely-available {r['pos']}")
    if order is None:
        return "no depth-chart data, and no value over replacement"
    return "too far down the depth chart to inherit, and below replacement"


def p_need_at(pos: str, roster: list[dict]) -> float:
    """Chance at least one of OUR starters at this position misses time."""
    starters = sum(1 for x in roster if x["pos"] == pos)
    if not starters:
        return 1.0
    return 1.0 - (1.0 - MISS_RATE.get(pos, 0.35)) ** min(starters, STARTERS.get(pos, 2))


def is_buzzed_rookie(r: dict, players: dict) -> bool:
    """A first-year skill player with real market noise behind him."""
    v = players.get(r["player_id"]) or {}
    if r["pos"] not in ROOKIE_POS or str(v.get("years_exp", "")) not in ("0", "None"):
        return False
    order = v.get("depth_chart_order")
    return (order is not None and int(order) <= max_depth(r["pos"])
            and buzz.signal(r["player_id"]) >= ROOKIE_BUZZ_MIN)


def classify(r: dict, roster: list[dict], board_by_id: dict, players: dict,
             depth: dict) -> str:
    """insurance | ticket | cover | none -- what job this player would do."""
    sid = ahead_of(r, players, depth)
    if sid and board_by_id.get(sid):
        return "insurance" if sid in {x["player_id"] for x in roster} else "ticket"
    return "cover" if over_waiver(r, board_by_id) > 0 else "none"


def audit(roster: list[dict], board_by_id: dict, players: dict,
          depth: dict) -> dict:
    """What the bench already has, against BENCH_QUOTA."""
    have = {"qb3": max(0, sum(1 for x in roster if x["pos"] == "QB") - 2),
            "ticket": 0, "rookie": 0}
    for x in roster:
        if classify(x, roster, board_by_id, players, depth) == "ticket":
            have["ticket"] += 1
        if is_buzzed_rookie(x, players):
            have["rookie"] += 1
    return have


def score(r: dict, roster: list[dict], board_by_id: dict, players: dict,
          depth: dict, picks_left: int = 0) -> tuple[float, str]:
    """Conditional value of adding `r` to the bench. Returns (points, why).

    Everything is measured in the same currency: points ABOVE WHAT WE WOULD
    OTHERWISE FIELD. That is what stops the quarterback bias creeping back. A
    QB3 projected for 244 sounds like the best body available until you note the
    freely-available QB scores 295 in this format -- he is worth nothing, and
    max(0, ...) says so, where "30% of his projection" said 73 and drafted him.
    """
    starter_id = ahead_of(r, players, depth)
    mine = {x["player_id"] for x in roster}
    conditional, why = 0.0, "no path to a role"

    starter = board_by_id.get(starter_id) if starter_id else None
    if starter:
        # he is a backup: value the ROLE he would inherit, less what we could
        # have picked off waivers instead
        p = p_opportunity(starter_id, players)
        # ...times the chance the job actually falls to HIM
        order = int((players.get(r["player_id"]) or {}).get("depth_chart_order") or 2)
        p *= INHERIT_P.get(order, 0.15)
        # The depth chart is a roster formality and ADP barely moves in August
        # (median 0.6 picks); a quarter of a million people adding a backup in
        # one day is the beat-reporter signal arriving before either catches up.
        lift = BUZZ_LIFT * buzz.signal(r["player_id"])
        if lift >= 0.02:
            p = min(0.95, p + lift)
            why_buzz = ", and the market has been adding him hard"
        else:
            why_buzz = ""
        gain = max(0.0, TALENT_KEEP * starter["proj_pts"]
                   - replacement_pts(starter, board_by_id))
        conditional = p * gain
        if starter_id in mine:
            conditional *= INSURANCE_WEIGHT
            why = (f"insurance behind our own {starter['name']} -- a {p:.0%} chance "
                   f"that job opens{why_buzz}, "
                   + (f"worth about {gain:.0f} points if it does" if gain >= 1
                      else "though the role itself is not worth much"))
        else:
            why = (f"a lottery ticket behind {starter['name']} -- a {p:.0%} chance "
                   f"that job opens{why_buzz}, "
                   + (f"worth about {gain:.0f} points if it does" if gain >= 1
                      else "though the role itself is not worth much"))
    elif over_waiver(r, board_by_id) > 0:
        # he already starts somewhere, so he has no door to walk through. He is
        # worth having only for the weeks one of ours is out, and only by how
        # much he beats the waiver wire.
        p = p_need_at(r["pos"], roster)
        ow = over_waiver(r, board_by_id)
        conditional = p * ow
        why = (f"{r['pos']} depth -- he starts elsewhere, and is worth about "
               f"{ow:.0f} points more than the wire in the weeks one of ours is out")
    else:
        # conditional stays 0.0 -- only the explanation changes. See the note in
        # no_value_reason(): this string goes straight to the public log.
        why = no_value_reason(r, players, board_by_id)

    kind = classify(r, roster, board_by_id, players, depth)
    if kind == "cover":
        conditional *= COVER_DISCOUNT

    # What the scout read in the reporting. Imported lazily: scout imports this
    # module for its depth-chart helpers, so a top-level import is circular.
    # Confidence-weighted and clamped inside trust_multiplier -- a model reading
    # a paragraph is one input among ADP, projections, depth charts and the
    # crowd, and must not be able to overturn all four.
    try:
        from robo.scout import trust_multiplier
        tm = trust_multiplier(r["player_id"])
    except Exception:
        tm = 1.0
    if abs(tm - 1.0) > 0.01:
        conditional *= tm
        # ONLY the magnitude, never the verdict's reason text. These reasons
        # quote reporting verbatim -- injuries, and in one case a named player's
        # criminal charge -- and this string is published to the league's public
        # decision log with every pick. The reasoning stays in the local file
        # where it can be audited without republishing an allegation.
        why += ("; recent reporting on him is encouraging" if tm > 1.0
                else "; recent reporting on him is a concern")

    # Quota pressure: grows as the picks run out, so a gap gets filled mid-draft
    # at a fair price rather than forced in the last round at any price.
    have = audit(roster, board_by_id, players, depth)
    urgency = min(1.0, 4.0 / max(1, picks_left)) if picks_left else 0.0
    fills = []
    real = conditional >= QUOTA_MIN_VALUE
    if real and kind == "ticket" and have["ticket"] < BENCH_QUOTA["ticket"]:
        fills.append("lottery-ticket")
    if real and is_buzzed_rookie(r, players) and have["rookie"] < BENCH_QUOTA["rookie"]:
        fills.append(f"rookie {r['pos']}")
    if r["pos"] == "QB" and have["qb3"] < BENCH_QUOTA["qb3"]:
        fills.append("third-quarterback")
    quota = QUOTA_BONUS * urgency * len(fills)
    if quota:
        why += ("; also fills our " + " and ".join(fills)
                + f" bench slot{'' if len(fills) == 1 else 's'}")

    saved = (bye_holes(roster) - bye_holes(roster + [r])) * BYE_HOLE_COST
    if saved:
        n_bye = int(round(saved / BYE_HOLE_COST))
        if n_bye > 0:
            why += f"; covers {n_bye} bye-week gap{'' if n_bye == 1 else 's'}"
        elif n_bye < 0:
            # Adding him can RAISE the hole count: bye_holes only counts weeks in
            # which somebody on the roster is actually off, so a new bye week can
            # expose thinness elsewhere. Worth saying plainly rather than
            # publishing "covers -2 bye-week gaps".
            why += "; though his bye week leaves us thin somewhere"
    return conditional + quota + saved, why


def context() -> tuple[dict, dict]:
    """(players, depth_map) -- cached by sleeper_read, so cheap to re-request."""
    players = api.players()
    return players, depth_map(players)
