"""Build the 2026 draft value board: data/board_2026.csv.

Blend of three free sources:
- Sleeper season projections scored with OUR league's exact scoring settings
  (5pt pass TD, 0.05/pass yd, -0.5/sack, 0.5 PPR, bonuses...) -> VORP over
  2QB replacement level (captures our scoring quirks).
- FantasyPros half-PPR superflex ECR, 105+ experts (captures expert judgment
  a single projection set misses).
- Market ADP: locked FFC 2QB (keeper-cost source) + Sleeper 2QB ADP - the
  draft agent uses market ADP to decide WHEN. It decides WHO on blend_PTS
  (below), not blend_rank; blend_rank orders this page and the best_available
  tool, and breaks ties in the endgame.

blend_rank = mean(VORP order, ECR); unranked-by-experts players fall back to
VORP order + a small penalty. That averages two RANKS and so discards magnitude,
which is fine for ordering a page and wrong for choosing between two players for
one lineup slot -- hence blend_pts, which converts the experts' ORDER into points
by permuting each position's own projection curve, and blends in points instead.
"""

import csv
import json

from robo import DATA, RAW
from robo import adp as adp_mod
from robo import adp_live
from robo import fantasypros
from robo.keeper import norm

# The LAST STARTER at each position, which is the right baseline for a BOARD:
# VORP here measures scarcity against what you are forced to start. QB is 25
# because 24 quarterbacks start every week in this format (12 dedicated + 12
# superflex, and QB28 still beats the receiver the superflex would otherwise
# hold), so the old 21 understated every quarterback by ~44 points.
#
# This is NOT the waiver wire and bench.py must not use it as one: 204 players
# are rostered in a 12x17 league, so WR32 is nowhere near freely available.
# bench.py derives its own waiver baseline -- see bench.waiver_pts.
REPLACEMENT_RANK = {"QB": 21, "RB": 30, "WR": 32, "TE": 13, "K": 12, "DEF": 12}

# How much the expert field is worth against our own projection when choosing
# between two players for the same lineup slot. 0.0 is projection alone, which
# is what the draft agent used to do; 1.0 is the experts' order with our
# projections supplying only the magnitudes. See blend_pts below.
ECR_WEIGHT = 0.5

# data/settings.json overrides the constants above. Import-time, so a change
# there takes effect on the next run of this module -- see robo/settings.py.
from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())


def load_projections() -> list[dict]:
    return json.loads((RAW / "projections_2026.json").read_text(encoding="utf-8"))


def custom_points(stats: dict, scoring: dict) -> float:
    return round(sum(v * scoring[k] for k, v in stats.items() if k in scoring and v), 1)


# ---- corrections for scoring keys the projections never include ----
# Sleeper's projections carry only 23 of our 57 scoring keys. Most misses are
# K/DEF noise we don't care about, but two classes materially shift skill
# players: sacks taken (-0.5 each; 25-45/yr per QB, and statue-vs-quick-release
# differences are real points) and the yardage/long-TD bonuses (2 pts a pop for
# big-play guys). We estimate both from each player's 2025 actuals scaled to
# projected volume; rookies (no 2025 stats) get no adjustment.

_BONUS_VOLUME = {  # missing scoring key -> the volume stat that drives it
    "bonus_rush_yd_100": "rush_yd", "bonus_rush_yd_200": "rush_yd",
    "bonus_rec_yd_100": "rec_yd", "bonus_rec_yd_200": "rec_yd",
    "bonus_pass_yd_400": "pass_yd",
    "rush_td_40p": "rush_yd", "rec_td_40p": "rec_yd", "pass_td_40p": "pass_yd",
}
_LEAGUE_SACK_RATE = 0.065  # fallback sacks-per-dropback for QBs with no 2025 sample

# Weekly pass attempts that mark a genuine starter, for the per-week sack
# correction. The season-scale gate is 100 attempts; a starter throws about
# thirty in a game and a mop-up backup throws a handful.
_WEEK_QB_ATT_MIN = 8


def stats_2025() -> dict:
    """2025 season actuals, cached to disk for offline resilience."""
    cache = RAW / "stats_2025.json"
    try:
        from robo import sleeper_read as api
        data = api.get("stats/nfl/regular/2025")
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data
    except Exception:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        return {}


def missing_key_points(pid: str, proj: dict, scoring: dict, s25_all: dict) -> float:
    s25 = s25_all.get(pid) or {}
    extra = 0.0
    proj_att = proj.get("pass_att") or 0
    if proj_att > 100:  # QBs: projected sacks = 2025 sack rate x projected attempts
        att25 = s25.get("pass_att") or 0
        rate = ((s25.get("pass_sack") or 0) / att25) if att25 > 100 else _LEAGUE_SACK_RATE
        extra += rate * proj_att * scoring.get("pass_sack", 0.0)
    for key, vol in _BONUS_VOLUME.items():
        pts = scoring.get(key) or 0.0
        c25, v25, pv = s25.get(key) or 0, s25.get(vol) or 0, proj.get(vol) or 0
        if pts and c25 and v25 > 50 and pv:
            extra += c25 * min(pv / v25, 2.0) * pts
    return round(extra, 1)


def missing_key_rate(pid: str, week_proj: dict, scoring: dict,
                     s25_all: dict) -> float:
    """The same 22-key correction, for ONE WEEK's projection rather than a season.

    The bonus half needs no change and that is not a coincidence: it already
    scales a player's 2025 bonus count by his projected volume over his 2025
    volume, so handing it a week's volume returns a week's worth of bonus.

    The sack half does need a different gate. It fires on `pass_att > 100`,
    which is a season starter and which no weekly blob will ever reach, so
    reusing it unchanged would silently drop a starting quarterback's sack
    penalty from every rest-of-season number -- the exact class of quiet
    omission the 57-key correction exists to stop.
    """
    s25 = s25_all.get(pid) or {}
    extra = 0.0
    week_att = week_proj.get("pass_att") or 0
    if week_att > _WEEK_QB_ATT_MIN:
        att25 = s25.get("pass_att") or 0
        rate = ((s25.get("pass_sack") or 0) / att25) if att25 > 100 else _LEAGUE_SACK_RATE
        extra += rate * week_att * scoring.get("pass_sack", 0.0)
    for key, vol in _BONUS_VOLUME.items():
        pts = scoring.get(key) or 0.0
        c25, v25, pv = s25.get(key) or 0, s25.get(vol) or 0, week_proj.get(vol) or 0
        if pts and c25 and v25 > 50 and pv:
            extra += c25 * min(pv / v25, 2.0) * pts
    return round(extra, 2)


def build_board() -> list[dict]:
    scoring = json.loads((DATA / "league_kb.json").read_text(encoding="utf-8"))["scoring_settings"]
    s25 = stats_2025()
    live = adp_live.load()
    ffc = {}
    for r in adp_mod.load():
        key = r["team"] if r["pos"] == "DEF" else norm(r["name"])
        ffc[key] = r

    rows = []
    for pr in load_projections():
        p = pr["player"]
        stats = pr["stats"] or {}
        pos = (p.get("fantasy_positions") or [None])[0]
        if pos not in REPLACEMENT_RANK:
            continue
        pts = custom_points(stats, scoring)
        if pts <= 0:
            continue
        name = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        pid = pr.get("player_id") or p.get("player_id")
        pts = round(pts + missing_key_points(pid, stats, scoring, s25), 1)
        is_def = pos == "DEF"
        key = pid if is_def else norm(name)
        ffc_row = ffc.get(key)
        live_row = live.get(key)
        rows.append({
            "player_id": pid,
            "name": name,
            "pos": pos,
            "team": p.get("team"),
            "proj_pts": pts,
            "adp_ffc": ffc_row["adp"] if ffc_row else None,
            "bye": ffc_row["bye"] if ffc_row else None,
            "adp_sleeper_2qb": stats.get("adp_2qb"),
            "adp_live": live_row["adp"] if live_row else None,
            "adp_stdev": live_row.get("stdev") if live_row else None,
            "injury_status": p.get("injury_status"),
        })

    # VORP against positional replacement
    by_pos = {}
    for r in rows:
        by_pos.setdefault(r["pos"], []).append(r)
    for pos, lst in by_pos.items():
        lst.sort(key=lambda r: -r["proj_pts"])
        repl_i = min(REPLACEMENT_RANK[pos], len(lst)) - 1
        repl = lst[repl_i]["proj_pts"]
        for i, r in enumerate(lst, 1):
            r["pos_rank"] = i
            r["vorp"] = round(r["proj_pts"] - repl, 1)

    rows.sort(key=lambda r: -r["vorp"])
    for i, r in enumerate(rows, 1):
        r["value_rank"] = i

    # blend in FantasyPros ECR (pos 'DST' there = our 'DEF')
    try:
        ecr_players = fantasypros.load()["players"]
    except FileNotFoundError:
        ecr_players = []
    ecr_by_name = {}
    for p in ecr_players:
        key = (norm(p["name"]), "DEF" if p["pos"] == "DST" else p["pos"])
        ecr_by_name[key] = p
    for r in rows:
        hit = ecr_by_name.get((norm(r["name"]), r["pos"]))
        r["ecr"] = hit["ecr"] if hit else None
        r["tier"] = hit["tier"] if hit else None
        r["blend_rank"] = round((r["value_rank"] + r["ecr"]) / 2, 1) if hit else r["value_rank"] + 25

    # EXPERT CONSENSUS, EXPRESSED IN POINTS.
    #
    # blend_rank averages two RANKS, which throws away magnitude: the gap from
    # QB1 to QB2 is 45 points and the gap from QB8 to QB9 is 2, and a rank
    # average calls both "one place". That is fine for ordering a page and wrong
    # for choosing between two players for a committed lineup slot, where points
    # are the whole payoff.
    #
    # So convert the experts' ORDER into points instead of averaging ranks. The
    # positional projection curve -- how value actually falls away at a position
    # -- is real information and is kept; the experts only get to say who sits
    # where on it. Concretely this permutes the ranked players' own projections
    # into ECR order, so the distribution is preserved exactly and only the
    # assignment changes.
    #
    # Where the two agree it does nothing at all: Josh Allen projects 404 and the
    # field has him QB1, so he blends to 404. It moves a player only where there
    # is real disagreement, which is what a guardrail should do.
    for pos, lst in by_pos.items():
        ranked = sorted((r for r in lst if r.get("ecr")), key=lambda r: r["ecr"])
        curve = sorted((r["proj_pts"] for r in ranked), reverse=True)
        for i, r in enumerate(ranked):
            r["expert_pts"] = curve[i]
        for r in lst:
            # Unranked by the experts is NEUTRAL, not a penalty. blend_rank
            # charges +25 ranks for it; in points space that would double-count,
            # since an unranked player almost always projects low already, and
            # it would bury exactly the late-round finds this is meant to price.
            r.setdefault("expert_pts", r["proj_pts"])
            r["blend_pts"] = round((1 - ECR_WEIGHT) * r["proj_pts"]
                                   + ECR_WEIGHT * r["expert_pts"], 1)

    rows.sort(key=lambda r: r["blend_rank"])
    return rows


def write_csv(rows: list[dict]) -> None:
    out = DATA / "board_2026.csv"
    fields = ["blend_rank", "value_rank", "ecr", "tier", "name", "pos", "team", "pos_rank",
              "proj_pts", "expert_pts", "blend_pts", "vorp", "adp_ffc", "adp_live",
              "adp_stdev", "adp_sleeper_2qb", "bye", "injury_status", "player_id"]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows([{k: r.get(k) for k in fields} for r in rows])
    print(f"wrote {out} ({len(rows)} players)")


if __name__ == "__main__":
    board = build_board()
    write_csv(board)
    print(f"{'blnd':>5} {'vrk':>3} {'ecr':>3} {'name':<24} pos prank {'proj':>6} {'vorp':>6} {'ffc':>6}")
    for r in board[:40]:
        print(f"{r['blend_rank']:>5} {r['value_rank']:>3} {r['ecr'] or '':>3} {r['name']:<24} "
              f"{r['pos']:<3} {r['pos_rank']:>4} {r['proj_pts']:>6} {r['vorp']:>6} {r['adp_ffc'] or '':>6}")
