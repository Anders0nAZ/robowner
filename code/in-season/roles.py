"""Who actually has the job, and who gets it if it opens -- measured, not declared.

WHAT THIS REPLACES. bench.py priced inheritance off Sleeper's depth_chart_order
with four invented constants (INHERIT_P = {2: .55, 3: .25, 4: .15, 5: .10}, one
curve for every position). Both repos already say the input is bad: this one
calls depth_chart_order "a roster formality [that] says nothing about a position
battle", and the NFL Model's notes say the nflverse depth-chart schema broke at
2025 and recommend snap or target share instead. It was the right call at draft
time -- a stated assumption beats a fabricated regression -- but the rookie-hold
rule in ros.py is only as good as this number, so it gets built properly.

WHAT REPLACES IT. Ten seasons of nflverse player_stats, already on disk in the
NFL Model's parquet cache. Two things are measured rather than assumed:

  * WHO IS AHEAD OF HIM -- rolling share of the team's positional opportunity,
    so the ordering is whoever actually gets the touches.
  * WHAT HE INHERITS -- over 2016-2025, find weeks where the man with an
    established role did not play, and measure how his share was really
    redistributed by prior rank.

THE FIT SAYS THE OLD CONSTANTS WERE WRONG IN A SPECIFIC WAY. Across 1,242
vacancy events:

    pos   rank2  rank3  rank4        the flat curve said
    QB    0.840  0.373      -        0.55 / 0.25 / 0.15
    RB    0.451  0.257  0.170        for every position
    TE    0.548  0.385  0.176
    WR    0.172  0.247  0.266

A backup QB inherits essentially the whole job -- median 1.000, it is one man's
snaps and somebody takes them. A vacated WR1's targets do NOT go to the WR2:
ranks 3 and 4 absorb more, because targets spread across a route tree instead of
promoting one man. So the single biggest error in the old curve was OVERpricing
every WR2 in the league, which is the same fiction as pricing a WR8 as the WR1's
heir, just pointed the other way.

WHY THE MEAN AND NOT THE MEDIAN. The distributions are bimodal -- a QB2 medians
1.000 and a WR5 medians 0.000, because mostly you either get the job or you do
not. The mean is the expected value, and expected value is what a roster
decision is priced on.

THE FIT IS HISTORY; THE PANEL IS LIVE. The curve refits monthly at most. Current
usage needs nflverse's CURRENT-season player_stats, which only exists once games
have been played and only if somebody refreshes it -- see freshness() and the
cold-start ladder in role().

    python -m robo.roles --fit          # refit and cache the curve
    python -m robo.roles --report       # the curve, with sample sizes
    python -m robo.roles --player "Name"
"""

import argparse
import json
import time
from functools import lru_cache

from robo import DATA, MODEL_ROOT, vegas

PARQUET = MODEL_ROOT / "data" / "parquet"
FIT_FILE = DATA / "roles_fit.json"
SCHEMA = 1

# What counts as "an opportunity" per position. A back's targets are part of his
# job, so counting only carries would call a receiving back a backup.
OPPORTUNITY = {"RB": ("carries", "targets"), "WR": ("targets",),
               "TE": ("targets",), "QB": ("attempts", "carries")}

# Trailing weeks that define the current pecking order. Four is long enough to
# survive one quiet game and short enough to notice a change of role.
WINDOW = 4

# The share that makes a role "established" -- below this, a man not playing is
# not a vacancy, it is a rotation.
MIN_ESTABLISHED_SHARE = 0.30

# Below this many observations a rank's cell is noise and falls back to the
# position's deep-rank pool. Set after seeing RB rank 6 come back at 0.694 on
# four events, which would have priced a fifth-string back like a starter.
MIN_EVENTS = 25

# What a rank absorbs when its cell is too thin to trust. Not zero: somebody
# gets the touches, we just cannot say it is this man.
THIN_CELL_ABSORPTION = 0.05

FIT_FIRST, FIT_LAST = 2016, 2025

# How many seasons after the draft a man with no usage still counts as a
# prospect rather than a washout. Two: a rookie who never played is normal, a
# third-year player who never played has answered the question.
DRAFT_PRIOR_SEASONS = 2

from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())


# ----------------------------------------------------------------- the panel

def _stats(seasons):
    """Concatenated player_stats for `seasons`, or None if none are on disk."""
    import polars as pl
    frames = []
    for yr in seasons:
        p = PARQUET / f"player_stats_{yr}.parquet"
        if not p.exists():
            continue
        try:
            df = pl.read_parquet(p)
        except Exception:
            continue
        keep = ["season", "week", "player_id", "player_display_name", "position",
                "team", "carries", "targets", "attempts"]
        if any(c not in df.columns for c in keep):
            continue
        df = df.filter(pl.col("position").is_in(list(OPPORTUNITY)))
        if "season_type" in df.columns:
            df = df.filter(pl.col("season_type") == "REG")
        frames.append(df.select(keep).fill_null(0))
    return pl.concat(frames) if frames else None


def panel(seasons, window: int = WINDOW):
    """Player-week shares of team positional opportunity, with trailing rank.

    THE GRID MATTERS MORE THAN THE ARITHMETIC. player_stats has no row for a
    player who did not play, so "did not play" reads as ABSENT, never as zero.
    Detecting vacancies off `opp == 0` therefore found 52 events in ten seasons;
    reconstructing the full grid first finds 1,242. Rows are only invented
    between a player's first and last appearance for that team in that season,
    so we do not credit him with weeks before he was signed or after he was
    traded, and only for weeks the team actually played -- a bye is not a
    benching.
    """
    import polars as pl
    d = _stats(seasons)
    if d is None or d.height == 0:
        return None
    d = d.with_columns(
        pl.when(pl.col("position") == "RB").then(pl.col("carries") + pl.col("targets"))
          .when(pl.col("position") == "QB").then(pl.col("attempts") + pl.col("carries"))
          .otherwise(pl.col("targets")).alias("opp"))

    team_weeks = d.select(["season", "team", "week"]).unique()
    span = d.group_by(["season", "team", "position", "player_id",
                       "player_display_name"]).agg(
        pl.col("week").min().alias("w0"), pl.col("week").max().alias("w1"))
    grid = span.join(team_weeks, on=["season", "team"]).filter(
        (pl.col("week") >= pl.col("w0")) & (pl.col("week") <= pl.col("w1")))
    full = grid.join(d.select(["season", "week", "team", "player_id", "opp"]),
                     on=["season", "week", "team", "player_id"],
                     how="left").with_columns(pl.col("opp").fill_null(0))

    full = full.with_columns(
        pl.col("opp").sum().over(["season", "week", "team", "position"]).alias("team_opp"))
    full = full.filter(pl.col("team_opp") > 0).with_columns(
        (pl.col("opp") / pl.col("team_opp")).alias("share"))
    full = full.sort(["season", "team", "position", "player_id", "week"]).with_columns(
        pl.col("share").shift(1).rolling_mean(window, min_samples=2)
          .over(["season", "team", "position", "player_id"]).alias("prior"))
    full = full.drop_nulls("prior").with_columns(
        pl.col("prior").rank("ordinal", descending=True)
          .over(["season", "week", "team", "position"]).alias("rank"))
    return full


# -------------------------------------------------------------------- the fit

def fit(first: int = FIT_FIRST, last: int = FIT_LAST, write: bool = True) -> dict:
    """Measure how a vacated role's share is really redistributed, by rank."""
    import polars as pl
    full = panel(range(first, last + 1))
    if full is None:
        return {"error": "no player_stats parquet found", "curve": {}}

    # Every week somebody held an established role -- the denominator for how
    # often such a role actually comes open. bench.py carried this as invented
    # constants (MISS_RATE = {RB .50, WR .38, TE .35, QB .28}, per season); it is
    # sitting right here in the same panel, per week, and can just be counted.
    held = full.filter((pl.col("rank") == 1) & (pl.col("prior") >= MIN_ESTABLISHED_SHARE))
    held_by_pos = {r["position"]: int(r["len"]) for r in held.group_by("position").len().to_dicts()}

    vac = full.filter((pl.col("rank") == 1)
                      & (pl.col("prior") >= MIN_ESTABLISHED_SHARE)
                      & (pl.col("opp") == 0)).select(
        ["season", "week", "team", "position", pl.col("prior").alias("vacated")])
    j = full.join(vac, on=["season", "week", "team", "position"]).filter(pl.col("rank") > 1)
    # The share a man gained, as a fraction of the share that came free. Summed
    # across ranks this lands near 1 by construction, which is a check that the
    # panel is complete rather than a reassurance about the model.
    j = j.with_columns(((pl.col("share") - pl.col("prior")) / pl.col("vacated")).alias("absorbed"))
    agg = j.group_by(["position", "rank"]).agg(
        pl.len().alias("n"), pl.col("absorbed").mean().alias("mean"),
        pl.col("absorbed").median().alias("median"),
        pl.col("absorbed").std().alias("sd")).sort(["position", "rank"])

    curve, deep = {}, {}
    for r in agg.iter_rows(named=True):
        pos, rk = r["position"], int(r["rank"])
        curve.setdefault(pos, {})[str(rk)] = {
            "n": int(r["n"]), "mean": round(float(r["mean"]), 4),
            "median": round(float(r["median"]), 4),
            "sd": round(float(r["sd"] or 0.0), 4)}
        if rk >= 4 and int(r["n"]) >= MIN_EVENTS:
            deep.setdefault(pos, []).append(float(r["mean"]))
    out = {"schema": SCHEMA, "fitted": time.time(),
           "seasons": [first, last], "window": WINDOW,
           "min_established_share": MIN_ESTABLISHED_SHARE,
           "events": int(vac.height),
           "events_by_pos": {r["position"]: int(r["len"]) for r in
                             vac.group_by("position").len().to_dicts()},
           "deep_pool": {p: round(sum(v) / len(v), 4) for p, v in deep.items()},
           "held_weeks": held_by_pos,
           "miss_rate": {p: round(n / held_by_pos[p], 4)
                         for p, n in {r["position"]: int(r["len"]) for r in
                                      vac.group_by("position").len().to_dicts()}.items()
                         if held_by_pos.get(p)},
           "curve": curve}
    if write:
        FIT_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


@lru_cache(maxsize=1)
def load_fit() -> dict:
    """The cached curve, refitting only if there is nothing usable on disk.

    Never refits just because the cache is old: a decade of finished seasons
    does not move, and a silent ten-file parquet scan inside a scheduled task is
    how a two-second job becomes a two-minute one.
    """
    if FIT_FILE.exists():
        try:
            d = json.loads(FIT_FILE.read_text(encoding="utf-8"))
            if d.get("schema") == SCHEMA and d.get("curve"):
                return d
        except Exception:
            pass
    return fit()


def miss_rate(pos: str) -> float:
    """Chance per week that an established starter at this position sits out.

    Counted, not assumed: vacancy events over weeks-a-role-was-held, on the same
    panel the absorption curve comes from. It is a PER-WEEK rate, which is the
    unit a rest-of-season sum needs -- bench.py's constants were per-season and
    would overstate this by an order of magnitude if dropped in unchanged.
    """
    return float((load_fit().get("miss_rate") or {}).get(pos, 0.0))


def absorption(pos: str, rank: int) -> tuple[float, str]:
    """(expected fraction of a vacated role this rank absorbs, why).

    Rank 1 is the man holding the job, so he absorbs nothing -- there is nothing
    to inherit from himself.
    """
    if rank <= 1:
        return 0.0, "holds the job"
    f = load_fit()
    cell = ((f.get("curve") or {}).get(pos) or {}).get(str(rank))
    if cell and cell["n"] >= MIN_EVENTS:
        return cell["mean"], f"fitted, n={cell['n']}"
    n = cell["n"] if cell else 0
    pooled = (f.get("deep_pool") or {}).get(pos)
    if pooled is not None:
        return pooled, f"thin cell (n={n}), pooled deep ranks"
    # No deep-rank pool exists for a position when every rank past the starter
    # is thin -- true of QB, where a third quarterback almost never takes a
    # snap, so there is nothing to measure and near-nothing to inherit.
    return THIN_CELL_ABSORPTION, f"thin cell (n={n}), no deep-rank pool for {pos}"


# ------------------------------------------------------------- the crosswalk

@lru_cache(maxsize=1)
def _by_gsis() -> dict:
    """gsis_id -> sleeper_id. The panel speaks gsis; every caller speaks Sleeper."""
    return {v["gsis"]: sid for sid, v in _crosswalk().items() if v.get("gsis")}


@lru_cache(maxsize=1)
def _by_espn() -> dict:
    """espn_id -> sleeper_id, for robo/injuries.py.

    ESPN's injury feed identifies a player by his ESPN id and his display name,
    and only one of those is safe: this repo already learned that "Josh Allen"
    is a quarterback and a linebacker. 6,239 rows carry both ids.
    """
    return {v["espn"]: sid for sid, v in _crosswalk().items() if v.get("espn")}


@lru_cache(maxsize=1)
def _crosswalk() -> dict:
    """sleeper_id -> {gsis, espn, name, pos, draft_year, draft_round, draft_pick}."""
    import polars as pl
    p = PARQUET / "ff_playerids.parquet"
    if not p.exists():
        return {}
    try:
        df = pl.read_parquet(p)
    except Exception:
        return {}
    out = {}
    for r in df.select(["sleeper_id", "gsis_id", "espn_id", "name", "position",
                        "draft_year", "draft_round", "draft_pick"]).iter_rows(named=True):
        sid = r["sleeper_id"]
        if sid is None or str(sid) == "":
            continue
        # espn_id arrives as a float in the parquet and as a string in ESPN's
        # URLs. Normalising here rather than at the join keeps the two
        # vocabularies from meeting anywhere else -- the same trap the NFL Model
        # hit comparing an integer sleeper_id against Sleeper's string one.
        espn = r["espn_id"]
        out[str(sid)] = {"gsis": r["gsis_id"],
                         "espn": None if espn is None else str(int(espn)),
                         "name": r["name"],
                         "pos": r["position"], "draft_year": r["draft_year"],
                         "draft_round": r["draft_round"], "draft_pick": r["draft_pick"]}
    return out


def freshness(season_yr) -> dict:
    """Whether the current season's usage data exists and how old it is.

    Reported rather than worked around. nflverse only publishes a season's
    player_stats once games have been played, and refreshing it is the NFL
    Model's ingest rather than ours -- so "no current usage" is an ordinary
    state in September and a problem in November, and only a timestamp can tell
    those apart.
    """
    p = PARQUET / f"player_stats_{int(season_yr)}.parquet"
    if not p.exists():
        return {"ok": False, "why": f"no player_stats_{int(season_yr)}.parquet yet",
                "age_h": None}
    age = (time.time() - p.stat().st_mtime) / 3600.0
    return {"ok": True, "why": "", "age_h": round(age, 1)}


# ---------------------------------------------------------------- the runtime

@lru_cache(maxsize=8)
def _rooms(season_yr: int, upto_week: int) -> dict:
    """{(team, pos): [players, best share first]} from each man's LATEST week.

    EACH PLAYER'S OWN LAST WEEK, not the room's. Taking the globally latest week
    silently drops anyone who did not play in it -- which lost Nico Collins, a
    genuine WR1, to the draft-capital fallback because he missed one late game.
    In season that failure is worse than cosmetic: a man who missed this week is
    precisely who a roster question is being asked about, and dropping him out
    of his own position room answers a different question confidently.

    Rank is then recomputed within the room, because ranks carried over from
    different weeks are not comparable.
    """
    import polars as pl
    full = panel([season_yr])
    if full is None:
        return {}
    seen = full.filter(pl.col("week") <= upto_week)
    if seen.height == 0:
        return {}
    latest = seen.sort("week").group_by(["team", "position", "player_id"]).last()
    out = {}
    for r in latest.iter_rows(named=True):
        out.setdefault((r["team"], r["position"]), []).append(
            {"gsis": r["player_id"], "name": r["player_display_name"],
             "prior": round(float(r["prior"]), 4), "week": int(r["week"])})
    for room in out.values():
        room.sort(key=lambda m: (-m["prior"], m["gsis"] or ""))
        for i, m in enumerate(room, 1):
            m["rank"] = i
    return out


def role(sleeper_id: str, team: str, pos: str, season_yr, week: int,
         record: dict | None = None) -> dict:
    """This player's standing in his own position room, and what he inherits.

    THE TIER IS PART OF THE ANSWER. A number built from four games of real usage
    and a number built from where a man was drafted are not the same claim, and
    a caller that cannot tell them apart will trust both equally.

    `record` keeps the whole POSITION ROOM and every rung of the cold-start
    ladder that was tried before one matched. The room is the most explanatory
    thing this module computes -- every man at the position with his rolling
    share of the team's work -- and until now it was built and thrown away on
    every call.
    """
    season_yr = int(season_yr)
    xw = _crosswalk().get(str(sleeper_id)) or {}
    tm, pos = vegas.team_code(team), (pos or "").upper()
    if record is not None:
        record.update({"sleeper_id": str(sleeper_id), "gsis": xw.get("gsis"),
                       "team_asked": team, "team": tm, "pos": pos,
                       "draft_year": xw.get("draft_year"),
                       "draft_round": xw.get("draft_round"),
                       "draft_pick": xw.get("draft_pick"),
                       "crosswalk": str(PARQUET / "ff_playerids.parquet"),
                       "panel": str(PARQUET / f"player_stats_{season_yr}.parquet"),
                       "tried": []})
    base = {"player_id": str(sleeper_id), "team": tm, "pos": pos,
            "rank": None, "share": 0.0, "ahead_of": None, "ahead_id": None,
            "absorbs": 0.0, "tier": "none", "why": ""}
    if pos not in OPPORTUNITY:
        base["why"] = f"{pos or 'unknown'} has no opportunity model"
        if record is not None:
            record["outcome"] = base["why"]
        return base

    gsis = xw.get("gsis")
    for yr, tier in ((season_yr, "usage"), (season_yr - 1, "prior-season")):
        rooms = _rooms(yr, week if yr == season_yr else 99)
        room = rooms.get((tm, pos)) or []
        me = next((r for r in room if r["gsis"] and r["gsis"] == gsis), None)
        if record is not None:
            record["tried"].append(
                {"season": yr, "tier": tier, "room_size": len(room),
                 "found": bool(me),
                 "why": "" if me else ("no room on file for this team and position"
                                       if not room else "not in this room")})
        if me:
            ahead = [r for r in room if r["rank"] < me["rank"]]
            frac, why = absorption(pos, me["rank"])
            if record is not None:
                record.update({"room": room, "room_season": yr, "me": me,
                               "absorb_why": why, "miss_rate": miss_rate(pos),
                               "fit": str(FIT_FILE),
                               "cell": ((load_fit().get("curve") or {})
                                        .get(pos) or {}).get(str(me["rank"])),
                               "outcome": tier})
            base.update({"rank": me["rank"], "share": me["prior"],
                         "ahead_of": ahead[0]["name"] if ahead else None,
                         "ahead_id": _by_gsis().get(ahead[0]["gsis"]) if ahead else None,
                         "absorbs": round(frac, 4), "tier": tier,
                         "why": f"{tier} through week {me['week']}; {why}"})
            return base

        # Same man, different team. His old share still says what he is, but it
        # says nothing about who is in front of him now, so he inherits nothing
        # here and the reason says why. Without this branch a traded veteran
        # falls through to draft capital and gets priced off a rookie prior from
        # five years ago -- which is how Jaylen Waddle read as a prospect.
        elsewhere = [(t, r) for (t, p), rm in rooms.items() if p == pos
                     for r in rm if r["gsis"] and r["gsis"] == gsis]
        if elsewhere:
            old_team, old = elsewhere[0]
            if record is not None:
                record.update({"room": rooms.get((old_team, pos)) or [],
                               "room_season": yr, "me": old,
                               "old_team": old_team, "outcome": "changed-team"})
            base.update({"share": old["prior"], "tier": "changed-team",
                         "why": f"{old['prior']:.0%} of {old_team} {pos} work in "
                                f"{yr}; no {tm} usage yet, so no line of succession"})
            return base

    # Cold start: no usage on record anywhere. Draft capital is the only thing
    # left, and it is a prior about talent rather than about a job -- so it says
    # he MIGHT be next in line, never that he is. Halved for exactly that, and
    # only for men young enough that no usage means "not yet" rather than
    # "never": a round-one pick from five years ago with no snaps on record is a
    # washout, and pricing him as a prospect is the depth-chart error again.
    rd, dy = xw.get("draft_round"), xw.get("draft_year")
    if rd and dy and season_yr - int(dy) <= DRAFT_PRIOR_SEASONS:
        rank = 2 if rd <= 2 else (3 if rd <= 4 else 4)
        frac, why = absorption(pos, rank)
        # WHO HE IS BEHIND STILL HAS TO BE NAMED. Without it the caller has an
        # absorption fraction and nobody to apply it to, so it multiplies
        # nothing and the rising-role premium silently evaluates to zero -- for
        # precisely the players it exists to protect, since a rookie with no
        # usage yet is the whole reason this branch is here. Taken from last
        # season's room for the team he is now on, which is the best available
        # guess at whose job he is behind.
        room = (_rooms(season_yr - 1, 99).get((tm, pos)) or [])
        top = room[0] if room else None
        if record is not None:
            record.update({"room": room, "room_season": season_yr - 1,
                           "me": None, "assumed_rank": rank,
                           "absorb_why": why, "miss_rate": miss_rate(pos),
                           "fit": str(FIT_FILE), "halved": True,
                           "outcome": "draft"})
        base.update({"absorbs": round(frac * 0.5, 4), "tier": "draft",
                     "ahead_of": top["name"] if top else None,
                     "ahead_id": _by_gsis().get(top["gsis"]) if top else None,
                     "why": f"no usage on record; {dy} round {rd} prior, {why}, halved"})
        return base
    base["why"] = "no usage on record and no recent draft capital"
    if record is not None:
        record["outcome"] = base["why"]
    return base


# ------------------------------------------------- the room the market expects

# The same question OPPORTUNITY asks of history, asked of the forecast. A back's
# receptions are part of his job, and a quarterback's carries are part of his, so
# counting only the headline key would call a receiving back a backup and read a
# running quarterback as a lesser passer.
PROJ_OPPORTUNITY = {"QB": ("pass_att", "rush_att"), "RB": ("rush_att", "rec"),
                    "WR": ("rec",), "TE": ("rec",)}


@lru_cache(maxsize=1)
def _projected_rooms() -> dict:
    """{(team, pos): [members, most opportunity first]} from the SEASON file.

    WHY THE FORECAST AND NOT THE DEPTH CHART. A listed rank is a roster
    formality that says nothing about a position battle, and draft round -- what
    role()'s cold-start branch falls back on -- says nothing about depth at all;
    it put a third-round quarterback third on his own team's chart and, because
    the QB absorption curve cliffs from 0.840 at rank 2 to 0.05 at rank 3, cost
    him a factor of thirty-four for a reason that was never about Arizona.

    Projected opportunity has none of those faults. It is CONTINUOUS, so there
    is no cliff to fall off; it is made by people reading beat reporters, so it
    moves on competition news; it exists in August for rookies and men who
    changed teams, which is precisely the cold start; and it is CURRENT BY
    CONSTRUCTION, which the nflverse rooms are not -- theirs are last season's
    rosters, so Arizona's still contains Kyler Murray, who plays for Minnesota.
    """
    from robo import rankings
    out: dict = {}
    for r in rankings.load_projections():
        p = r.get("player") or {}
        pid = str(r.get("player_id") or p.get("player_id") or "")
        pos = (p.get("fantasy_positions") or [None])[0]
        tm = p.get("team")
        if not pid or not tm or pos not in PROJ_OPPORTUNITY:
            continue
        st = r.get("stats") or {}
        opp = sum(float(st.get(k) or 0) for k in PROJ_OPPORTUNITY[pos])
        if opp <= 0:
            continue
        out.setdefault((tm, pos), []).append(
            {"player_id": pid, "name": f"{p.get('first_name')} {p.get('last_name')}",
             "opp": round(opp, 1)})
    for room in out.values():
        total = sum(m["opp"] for m in room) or 1.0
        room.sort(key=lambda m: (-m["opp"], m["player_id"]))
        for i, m in enumerate(room, 1):
            m["rank"], m["share"] = i, round(m["opp"] / total, 4)
    return out


def projected_role(sleeper_id: str, team: str, pos: str,
                   record: dict | None = None) -> dict:
    """Where the market expects this man to stand in his own position room.

    Returns the same shape role() does, so the two are interchangeable to a
    caller, and carries tier "projected" so they are never mistaken for each
    other. A share is reported alongside the rank because the rank is the lossy
    part: Brissett 355 / Beck 124 / Minshew 60 and a three-way dead heat are
    both "ranks 1, 2, 3", and only one of them is a settled job.
    """
    from robo import vegas
    tm, pos = vegas.team_code(team), (pos or "").upper()
    base = {"player_id": str(sleeper_id), "team": tm, "pos": pos, "rank": None,
            "share": 0.0, "ahead_of": None, "ahead_id": None,
            "lead_of": None, "lead_id": None, "absorbs": 0.0,
            "tier": "projected", "why": ""}
    if pos not in PROJ_OPPORTUNITY:
        base["why"] = f"{pos or 'unknown'} has no opportunity model"
        return base
    room = _projected_rooms().get((tm, pos)) or []
    me = next((m for m in room if m["player_id"] == str(sleeper_id)), None)
    if record is not None:
        record.update({"room": room, "me": me, "team": tm, "pos": pos,
                       "source": "season projection"})
    if not me:
        base["why"] = f"no projected {pos} opportunity for {tm}"
        return base
    frac, why = absorption(pos, me["rank"])
    ahead = room[me["rank"] - 2] if me["rank"] > 1 else None
    # THE LEAD, NOT THE MAN ONE RUNG UP, IS WHAT `absorbs` IS MEASURED AGAINST.
    # fit() defines a vacancy as the RANK-1 man's opportunity going to zero and
    # divides the share each rank gained by HIS vacated share, so `absorbs` is a
    # fraction of the lead's workload. Pricing it against the rank above is a
    # mismatched numerator and denominator, and it silently guts every deep
    # bench player: Kaelon Black is SF's rank 3, so he was priced as inheriting
    # from Jordan James at 0.41 points a week when the bet is McCaffrey. It is
    # identical for rank 2, which is why that half always looked right.
    lead = room[0] if room else None
    if record is not None:
        record.update({"absorb_why": why, "miss_rate": miss_rate(pos),
                       "cell": ((load_fit().get("curve") or {}).get(pos)
                                or {}).get(str(me["rank"]))})
    base.update({"rank": me["rank"], "share": me["share"],
                 "ahead_of": ahead["name"] if ahead else None,
                 "ahead_id": ahead["player_id"] if ahead else None,
                 "lead_of": lead["name"] if lead and lead is not me else None,
                 "lead_id": lead["player_id"] if lead and lead is not me else None,
                 "absorbs": round(frac, 4),
                 "why": f"projected {me['share']:.0%} of the {tm} {pos} room "
                        f"({me['opp']:g} opportunities); {why}"})
    return base


# ------------------------------------------------------------------- reports

def report() -> str:
    f = load_fit()
    if f.get("error"):
        return f"cannot fit: {f['error']}"
    L = [f"ROLE INHERITANCE - fitted on {f['seasons'][0]}-{f['seasons'][1]}, "
         f"{f['events']} vacancy events",
         f"  a role counts as established at {f['min_established_share']:.0%} "
         f"of team opportunity over {f['window']} weeks", "",
         f"  {'pos':<5}{'rank':>5}{'n':>7}{'absorbs':>9}{'median':>9}{'sd':>8}"]
    for pos in sorted(f["curve"]):
        for rk in sorted(f["curve"][pos], key=int):
            c = f["curve"][pos][rk]
            thin = "   <- thin, pooled" if c["n"] < MIN_EVENTS else ""
            L.append(f"  {pos:<5}{rk:>5}{c['n']:>7}{c['mean']:>9.3f}"
                     f"{c['median']:>9.3f}{c['sd']:>8.3f}{thin}")
        L.append("")
    L.append("  read as: fraction of a vacated role's opportunity this rank picks up")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--player", default=None)
    ap.add_argument("--freshness", action="store_true")
    args = ap.parse_args()

    if args.fit:
        f = fit()
        load_fit.cache_clear()
        print(f"fitted {f.get('events', 0)} vacancy events "
              f"{f.get('events_by_pos', {})} -> {FIT_FILE.name}\n")
    if args.freshness:
        from robo import season as _season
        print(freshness(_season.SEASON))
        return
    if args.player:
        from robo import season as _season
        from robo import sleeper_read as api
        pmap = api.players()
        hits = [(pid, p) for pid, p in pmap.items()
                if args.player.lower() in (p.get("full_name") or "").lower()]
        if not hits:
            print(f"no player matching {args.player!r}")
            return
        for pid, p in hits[:5]:
            r = role(pid, p.get("team") or "", p.get("position") or "",
                     _season.SEASON, _season.current_week())
            print(f"  {(p.get('full_name') or '?'):<26} {r['pos']:<3} {r['team']:<4} "
                  f"rank={str(r['rank']):<5} share={r['share']:.3f}  "
                  f"absorbs={r['absorbs']:.3f}  [{r['tier']}]")
            print(f"    behind: {r['ahead_of'] or '-'}    {r['why']}")
        return
    print(report())


if __name__ == "__main__":
    main()
