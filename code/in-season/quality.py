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

import math
from functools import lru_cache
from typing import Any

from robo import DATA, settings

# Benchmark baselines by position for superflex / 2QB scoring.
# Wire base is roughly what a replacement-level free agent provides.
# Starter base is roughly what an established starter provides.
WIRE_BASE = {"QB": 2.0, "RB": 4.0, "WR": 5.0, "TE": 3.0, "K": 6.0, "DEF": 5.0}
STARTER_BASE = {"QB": 17.0, "RB": 13.0, "WR": 13.0, "TE": 9.5, "K": 8.0, "DEF": 8.0}

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
    floor_src = str(row.get("floor_source") or "").lower()
    scout_basis = str(row.get("scout_basis") or "").lower()
    is_out_for_season = "season" in floor_src or "season" in scout_basis

    by_w = row.get("by_week") or {}
    s1_vals = [float(b.get("s1") or 0.0) for b in by_w.values() if float(b.get("s1") or 0.0) > 0]
    healthy_s1 = max(s1_vals) if s1_vals else 0.0

    # 2. Weekly projection & ceiling from model_week
    if model_week is None:
        try:
            from robo import model_proj
            art, _ = model_proj.load()
            model_week = art.get("players", {}) if art else {}
        except Exception:
            model_week = {}
    m_info = model_week.get(pid) or {}

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

    w_base = WIRE_BASE.get(pos, 4.0)
    s_base = STARTER_BASE.get(pos, 12.0)
    denom = max(1.0, s_base - w_base)

    # Component A: Current week projection (with healthy baseline fallback for non-season-ending injuries)
    wk_cell = by_w.get(str(week)) or {}
    wk_pts = m_info.get("mean")
    if wk_pts is None:
        wk_pts = wk_cell.get("pts") or wk_cell.get("final") or 0.0
    wk_pts = float(wk_pts)

    if is_out_for_season:
        eval_proj = 0.0
    elif is_injured and healthy_s1 > 0:
        eval_proj = healthy_s1
    else:
        eval_proj = wk_pts
    f_proj = max(0.0, min(1.0, (eval_proj - w_base) / denom))

    # Component B: Distributional ceiling (p90)
    p90 = m_info.get("p90")
    if is_out_for_season:
        eval_ceil = 0.0
    elif is_injured and healthy_s1 > 0:
        eval_ceil = float(p90) if p90 is not None and float(p90) > healthy_s1 else healthy_s1 * 1.5
    else:
        if p90 is not None:
            eval_ceil = float(p90)
        else:
            eval_ceil = wk_pts * 1.5 if wk_pts > 0 else 0.0
    f_ceil = max(0.0, min(1.0, (eval_ceil - 9.0) / 14.0))

    # Component C: Rest-of-season baseline (normalized across active projected games)
    total_weeks = 17
    weeks_left = max(1, total_weeks - week + 1)
    ros_pts = float(row.get("ros") or 0.0)
    ros_per_wk = ros_pts / weeks_left

    active_weeks = sum(1 for w, b in by_w.items() if int(w) >= week and float(b.get("a") or 0.0) > 0.2)
    if is_out_for_season or ros_pts <= 0:
        f_ros = 0.0
    elif active_weeks > 0:
        ros_rate = ros_pts / active_weeks
        f_ros = max(0.0, min(1.0, (ros_rate - w_base) / denom))
    else:
        f_ros = max(0.0, min(1.0, (ros_per_wk - w_base) / denom))

    # Component D: Role, inheritance & takeover potential
    rank = row.get("rank")
    absorbs = float(row.get("absorbs") or 0.0)
    s2 = float(wk_cell.get("s2") or 0.0)
    takeover = 0.0
    try:
        from robo import roles
        takeover = float(roles.takeover_rate(pos, 0, None)[0])
    except Exception:
        takeover = 0.0

    if is_out_for_season:
        f_role = 0.15
    elif rank == 1 or (rank is None and is_injured and healthy_s1 >= s_base):
        f_role = 1.0
    elif rank is None and is_injured and healthy_s1 > w_base:
        f_role = 0.65
    elif s2 > 3.0:
        # High inherited opportunity from an injured lead
        f_role = min(1.0, 0.45 + s2 / 12.0)
    elif rank == 2:
        f_role = 0.35 + 0.40 * absorbs
    else:
        f_role = 0.15
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

    reason = (
        f"{tier} (Q={q:.2f}): proj {eval_proj:.1f}pts ({f_proj:.2f}), "
        f"p90 {eval_ceil:.1f} ({f_ceil:.2f}), ROS {ros_per_wk:.1f}/wk ({f_ros:.2f}), "
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
