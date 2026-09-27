"""The Player Quality Engine (PQI) for waiver valuation and bidding.

Transforms player talent, immediate projection, ceiling, rest-of-season
expectations, crowd velocity (buzz), role inheritance, and pedigree into a
normalized Quality Score Q in [0.0, 1.0] and discrete Quality Tiers.

Used in two directions:
  1. Market Competition: High-Q players face a higher probability of being
     contested and a shifted rival bid distribution (Puka, Kyren, Mike Davis).
  2. Asset Valuation: High-Q players carry option/denial equity beyond static
     starting lineup delta, preventing reservation bids from collapsing when
     our current starters happen to be healthy.
"""

from __future__ import annotations

import bisect
import math
import statistics
from functools import lru_cache
from typing import Any

from robo import DATA, settings

# Production, ceiling and ROS are scored as a PERCENTILE WITHIN POSITION over
# the pool the league actually trades in: every rostered man plus the best
# POOL_WIRE_DEPTH unrostered. Hand-set wire/starter baselines put a 7.1-point
# TE at 0.63 and a 7.2-point WR at 0.0 on the same week; this league's own
# distribution puts them at 0.47 and 0.29. The simulator's wire floor cannot
# anchor the scale instead -- at QB it is one backup's single spot start.
POOL_WIRE_DEPTH = 15
# Without a believable roster read, the pool is the top FALLBACK_POOL per
# position by rate -- about what the league holds plus the wire's best.
MIN_ROSTERED_SEEN = 100
FALLBACK_POOL = 60
# Weeks ahead the production rate is read over. A bye or a one-week
# Questionable dip is a week, not the player.
FORWARD_WEEKS = 5

# Quality weights summing to 1.0.
Q_WEIGHT_PROJ = 0.25
Q_WEIGHT_ROS = 0.20
Q_WEIGHT_CEIL = 0.15
Q_WEIGHT_BUZZ = 0.20
Q_WEIGHT_ROLE = 0.15
Q_WEIGHT_PEDIGREE = 0.05

# Tier boundary thresholds.
TIER_T1 = 0.70  # League-winning breakout / bellcow promotion
TIER_T2 = 0.45  # Strong multi-week contributor / spot starter
TIER_T3 = 0.25  # Speculative bench ticket / contingent stash
# Below T3 is T4_REPLACEMENT

# Scaling parameters for auction dynamics.
Q_BID_MULT_BETA = 2.2     # Controls exponential scaling on rival bids: exp(beta * (Q - 0.45))
Q_CONTEST_GAMMA = 0.70    # Controls additive shift on P(contested): gamma * (Q - 0.40)
Q_OPTION_WEIGHT = 8.0     # Baseline points of asset option value at max Q

settings.apply(__name__, globals())


@lru_cache(maxsize=1)
def _draft_capital_map() -> dict[str, dict[str, Any]]:
    """Map of sleeper_id -> draft capital dict from ff_playerids.parquet."""
    path = DATA / "nflmodel" / "parquet" / "ff_playerids.parquet"
    try:
        if not path.exists():
            return {}
        import polars as pl
        df = pl.read_parquet(path)
        sub = df.filter(pl.col("sleeper_id").is_not_null()).select(
            ["sleeper_id", "draft_round", "draft_pick", "draft_year", "position"]
        )
        out = {}
        for r in sub.to_dicts():
            out[str(r["sleeper_id"])] = r
        return out
    except Exception:
        return {}


def pedigree_score(player_id: str) -> float:
    """0.1 to 1.0 score based on NFL draft round and pedigree."""
    info = _draft_capital_map().get(str(player_id))
    if not info:
        return 0.20
    rnd = info.get("draft_round")
    if rnd is None:
        return 0.15  # Undrafted free agent
    try:
        r = int(rnd)
        if r == 1:
            return 1.00
        if r == 2:
            return 0.80
        if r == 3:
            return 0.65
        if r in (4, 5):
            return 0.45
        if r in (6, 7):
            return 0.25
        return 0.15
    except (TypeError, ValueError):
        return 0.20


@lru_cache(maxsize=1)
def _players_dump() -> dict[str, dict[str, Any]]:
    """Cached full player dump from sleeper_read."""
    try:
        from robo import sleeper_read as api
        return api.players()
    except Exception:
        return {}


def _fwd_rate(row: dict, week: int) -> float:
    """Median points in the weeks he plays over the next FORWARD_WEEKS.

    Read through `marginal.weekly_points`, the simulator's own per-week figure.
    A man out for the whole window falls back to the median of every week he
    is projected to play -- his healthy rate, not his best week.
    """
    from robo import marginal
    by_w = row.get("by_week") or {}

    def pts(w: int) -> float:
        d = by_w.get(str(w)) or {}
        cell = (float(d.get("s1") or 0.0), 0.0, 0.0, 0.0, 0.0, 0.0, None,
                float(d.get("provider") or 0.0))
        return marginal.weekly_points({"weeks": {w: cell}}, w)

    near = [v for v in (pts(w) for w in range(week, week + FORWARD_WEEKS)) if v > 0]
    if near:
        return statistics.median(near)
    every = [v for v in (pts(int(w)) for w in by_w) if v > 0]
    return statistics.median(every) if every else 0.0


def _ros_rate(row: dict, week: int, weeks_left: int) -> float:
    """ROS points per week he is expected to be available."""
    ros_pts = float(row.get("ros") or 0.0)
    active = sum(1 for w, b in (row.get("by_week") or {}).items()
                 if int(w) >= week and float(b.get("a") or 0.0) > 0.2)
    return ros_pts / active if active else ros_pts / max(1, weeks_left)


_POOL: dict = {}


def _pool(table: dict, week: int, weeks_left: int, model_week: dict) -> dict:
    """{pos: {"rate"|"ros"|"p90": sorted values}} over rostered + top of the wire."""
    # Identity, not id(): the cache holds both objects, so a recycled id can
    # never hand one table's pool to another.
    hit = _POOL.get("key")
    if hit and hit[0] is table and hit[1] is model_week and hit[2] == week:
        return _POOL["out"]
    try:
        from robo import season
        held = {str(p) for p in season.rostered_ids()}
    except Exception:
        held = set()
    # A roster read that misses most of a 12-team league would shrink the pool
    # to a handful of men and inflate every percentile; rank by rate instead.
    if len(held & set(table.get("players") or {})) < MIN_ROSTERED_SEEN:
        held = set()
    by_pos: dict[str, list[tuple]] = {}
    for pid, row in (table.get("players") or {}).items():
        pos = row.get("pos")
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        p90 = (model_week.get(pid) or {}).get("p90")
        by_pos.setdefault(pos, []).append(
            (pid in held, _fwd_rate(row, week), _ros_rate(row, week, weeks_left),
             None if p90 is None else float(p90)))
    out = {}
    for pos, rows in by_pos.items():
        if held:
            wire = sorted((r for r in rows if not r[0]), key=lambda r: -r[1])
            members = [r for r in rows if r[0]] + wire[:POOL_WIRE_DEPTH]
        else:
            members = sorted(rows, key=lambda r: -r[1])[:FALLBACK_POOL]
        out[pos] = {"rate": sorted(r[1] for r in members),
                    "ros": sorted(r[2] for r in members),
                    "p90": sorted(r[3] for r in members if r[3] is not None)}
    _POOL.update(key=(table, model_week, week), out=out)
    return out


def _pct(vals: list[float], x: float) -> float:
    """Share of the pool strictly below x."""
    return bisect.bisect_left(vals, x) / len(vals) if vals else 0.0


def score(player_id: str,
          week: int | None = None,
          league_id: str | None = None,
          ctx: dict | None = None,
          table: dict | None = None,
          model_week: dict | None = None,
          buzz_signal: float | None = None) -> dict[str, Any]:
    """Compute comprehensive quality metrics for one player.

    Returns:
      dict with:
        q: float in [0.0, 1.0]
        tier: "T1_BREAKOUT" | "T2_CONTRIBUTOR" | "T3_SPECULATIVE" | "T4_REPLACEMENT"
        components: {proj, ceil, ros, buzz, role, pedigree}
        option_value: float (points to add to starting lineup gain)
        rival_bid_mult: float (multiplier on rival bid distribution)
        p_contested_boost: float (shift to P(contested))
        reason: plain-English summary
    """
    pid = str(player_id)
    week = int(week or (ctx.get("week") if ctx else 2) or 2)

    # 1. Stored rest-of-season / expected data & player metadata
    if table is None:
        try:
            from robo import expected
            table = expected.load()
        except Exception:
            table = {}
    row = (table.get("players") or {}).get(pid) or {}
    p_meta = _players_dump().get(pid) or (ctx.get("players", {}).get(pid) if ctx else {}) or {}
    pos = row.get("pos") or p_meta.get("position") or (ctx.get("players", {}).get(pid, {}).get("position") if ctx else "?")
    name = row.get("name") or p_meta.get("full_name") or pid
    team = row.get("team") or p_meta.get("team") or ""

    inj_status = row.get("injury_status") or p_meta.get("injury_status")
    is_injured = inj_status in ("IR", "IR-R", "PUP-P", "PUP-R", "NFI-R", "Out", "OUT", "SUS")
    # Structured markers only. A substring match on "season" caught a scout
    # basis reading "out-for-season prose conflicts with ACTIVE" -- a man
    # explicitly NOT ruled out -- and a "4-6 week rehab ... season" note.
    try:
        from robo import injuries
        feed_out = injuries.out_for_season(pid)
    except Exception:
        feed_out = False
    is_out_for_season = feed_out or row.get("floor_source") == "espn (out for the season)"

    by_w = row.get("by_week") or {}
    remaining = sum(1 for w in by_w if int(w) >= week)
    weeks_left = remaining or max(1, 17 - week + 1)

    # 2. Weekly projection & ceiling from model_week
    if model_week is None:
        try:
            from robo import model_proj
            art, _ = model_proj.load()
            model_week = art.get("players", {}) if art else {}
        except Exception:
            model_week = {}
    m_info = model_week.get(pid) or {}
    pool = _pool(table, week, weeks_left, model_week).get(pos) or {}

    # 3. Buzz & Ownership Combo
    # Search rank as global ownership proxy (100% owned for top 120, gradual decay)
    sr = p_meta.get("search_rank")
    if sr is not None and sr > 0:
        if sr <= 120:
            owned_est = 1.0
        elif sr <= 240:
            owned_est = round(1.0 - (sr - 120) * 0.005, 3)
        elif sr <= 400:
            owned_est = round(max(0.0, 0.40 - (sr - 240) * 0.002), 3)
        else:
            owned_est = 0.05
    else:
        owned_est = 0.10

    if buzz_signal is None:
        try:
            from robo import buzz
            f_net_adds = float(buzz.signal(pid))
        except Exception:
            f_net_adds = 0.0
    else:
        f_net_adds = max(0.0, min(1.0, float(buzz_signal)))

    # Combined market demand: universal ownership base + trending add velocity on remaining unowned share
    f_buzz = min(1.0, owned_est + f_net_adds * (1.0 - owned_est))
    f_buzz = round(f_buzz, 3)

    # Component A: production rate over the next FORWARD_WEEKS, percentile
    # within position.
    eval_proj = 0.0 if is_out_for_season else _fwd_rate(row, week)
    f_proj = _pct(pool.get("rate") or [], eval_proj) if eval_proj > 0 else 0.0

    # Component B: this week's p90, percentile within position. No game or no
    # model row this week falls back to the production percentile.
    p90 = m_info.get("p90")
    if is_out_for_season:
        eval_ceil, f_ceil = 0.0, 0.0
    elif p90 is not None and float(p90) > 0 and not is_injured:
        eval_ceil = float(p90)
        f_ceil = _pct(pool.get("p90") or [], eval_ceil)
    else:
        eval_ceil, f_ceil = None, f_proj

    # Component C: ROS per available week, percentile within position.
    ros_pts = float(row.get("ros") or 0.0)
    ros_per_wk = _ros_rate(row, week, weeks_left)
    f_ros = (0.0 if is_out_for_season or ros_pts <= 0
             else _pct(pool.get("ros") or [], ros_per_wk))

    # Component D: Role, inheritance & takeover potential
    rank = row.get("rank")
    absorbs = float(row.get("absorbs") or 0.0)
    wk_cell = by_w.get(str(week)) or {}
    s2 = float(wk_cell.get("s2") or 0.0)

    if is_out_for_season:
        f_role = 0.15
    elif rank == 1 or (rank is None and is_injured and f_proj >= 0.5):
        f_role = 1.0
    elif pos == "WR" and rank in (2, 3):
        # Three-receiver sets: WR2 and WR3 are jobs, not a wait for a vacancy.
        f_role = 1.0
    elif rank is None and is_injured and f_proj > 0:
        f_role = 0.65
    elif s2 > 3.0:
        # High inherited opportunity from an injured lead
        f_role = min(1.0, 0.45 + s2 / 12.0)
    elif rank == 2:
        f_role = 0.35 + 0.40 * absorbs
    else:
        f_role = 0.15
    # His own takeover prior (draft capital, experience, usage), called the way
    # the simulator calls it and for the same heirs only.
    takeover = 0.0
    try:
        from robo import marginal, roles, season as _season
        if (rank is not None and 2 <= rank <= marginal.TAKEOVER_MAX_RANK
                and pos in roles.OPPORTUNITY):
            takeover = float(roles.takeover_prior(
                pid, pos, _season.SEASON, team=team or None, week=week).get("p") or 0.0)
    except Exception:
        takeover = 0.0
    if takeover > 0.10:
        f_role = min(1.0, f_role + 0.20 * (takeover / 0.20))

    # Component E: Draft capital pedigree
    f_pedigree = pedigree_score(pid)

    # Weighted Composite Score Q
    q = (
        Q_WEIGHT_PROJ * f_proj +
        Q_WEIGHT_ROS * f_ros +
        Q_WEIGHT_CEIL * f_ceil +
        Q_WEIGHT_BUZZ * f_buzz +
        Q_WEIGHT_ROLE * f_role +
        Q_WEIGHT_PEDIGREE * f_pedigree
    )
    q = round(max(0.0, min(1.0, q)), 4)

    # Categorical Tier
    if q >= TIER_T1:
        tier = "T1_BREAKOUT"
    elif q >= TIER_T2:
        tier = "T2_CONTRIBUTOR"
    elif q >= TIER_T3:
        tier = "T3_SPECULATIVE"
    else:
        tier = "T4_REPLACEMENT"

    # Auction Multipliers
    # rival_bid_mult scales rival bids up for high-Q breakouts, down for low-Q fodder
    mult = math.exp(Q_BID_MULT_BETA * (q - 0.45))
    rival_bid_mult = round(max(0.30, min(4.50, mult)), 3)

    # p_contested_boost shifts contest probability
    p_contested_boost = round(max(-0.20, min(0.55, Q_CONTEST_GAMMA * (q - 0.40))), 4)

    # Asset Option Value for Robowner's own reservation price
    if q >= TIER_T3:
        # Scales with weeks remaining and excess quality above speculative floor
        opt_val = Q_OPTION_WEIGHT * (q - 0.35) * (weeks_left / 16.0)
        option_value = round(max(0.0, opt_val), 3)
    else:
        option_value = 0.0

    ceil_txt = f"p90 {eval_ceil:.1f}" if eval_ceil is not None else "p90 n/a"
    reason = (
        f"{tier} (Q={q:.2f}): rate {eval_proj:.1f}pts ({f_proj:.2f}), "
        f"{ceil_txt} ({f_ceil:.2f}), ROS {ros_per_wk:.1f}/wk ({f_ros:.2f}), "
        f"buzz {f_buzz:.2f}, role {f_role:.2f}; rival bid mult {rival_bid_mult:.2f}x"
    )

    return {
        "player_id": pid,
        "name": name,
        "pos": pos,
        "team": team,
        "q": q,
        "tier": tier,
        "components": {
            "proj": round(f_proj, 3),
            "ceil": round(f_ceil, 3),
            "ros": round(f_ros, 3),
            "buzz": round(f_buzz, 3),
            "role": round(f_role, 3),
            "pedigree": round(f_pedigree, 3),
        },
        "option_value": option_value,
        "rival_bid_mult": rival_bid_mult,
        "p_contested_boost": p_contested_boost,
        "reason": reason,
    }
