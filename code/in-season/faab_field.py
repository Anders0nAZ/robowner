"""Opponent-by-opponent waiver demand and bid distributions.

The pooled auction curve in :mod:`robo.faab` answers what an anonymous rival
historically bid.  This module answers the question that exists on waiver
night: which of the eleven actual managers needs this player, how often that
manager enters an auction, and what that manager bids when he does.

Every runtime output is a distribution, not a guessed rival.  Thin manager
samples shrink toward the league and the caller can fall back to the pooled
curve if the historical panel cannot be built.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from functools import lru_cache

from robo import DATA, ROBOWNER_USER_ID, lineup, season

DB = DATA / "history.db"
MODEL_VERSION = 3
SKILL = {"QB", "RB", "WR", "TE"}
STARTERS = {"QB": 2, "RB": 2, "WR": 2, "TE": 1}
DEPTH = {"QB": 3, "RB": 4, "WR": 4, "TE": 2}
PRIOR_EVENTS = 24.0

# How far a historical bid may be rescaled onto a different budget. _bid_pmf
# carries a bid across by the ratio of the two budgets, which asserts a bid is
# a FRACTION of what is left rather than an amount -- and that premise holds:
# within a week bucket, managers with roughly 2.5x the budget bid roughly 2.7x
# as much (weeks 4-7), 6.3x against 7.1x (weeks 8-11).
#
# WHAT BREAKS IS THE TAIL, because the ratio divides by a drained historical
# budget. Uncapped it runs 1.32x at the median, 8.3x at p90 and 100x at the
# extreme, and an all-in bid maps to an all-in bid: $45 with $45 left becomes
# $100. 114 of 305 positive bids were at least doubled.
#
# The direction also depends on WHEN we forecast -- budgets average $92 in
# weeks 1-3 and $53 by week 12, so history is scaled UP early and DOWN late.
# Early is where waiver night lives.
#
# Measured by backtest, 307 archived auctions, leave-one-season-out, predicting
# the field maximum and scoring it against the bid that actually won (the
# winner IS the maximum, so it is observable). Uncapped scored 12.009 CRPS
# against 10.007 with no rescaling at all -- worse in every one of the four
# week buckets. At 2.0 it scores 10.223 and its week 1-3 median lands on the
# observed $12, where uncapped says $16 and no rescaling says $7. So the cap
# keeps the premise, drops the tail, and is the only variant that hits the
# middle of the distribution it is trying to describe.
BID_SCALE_CAP = 2.0


def _loads(value, default):
    try:
        return json.loads(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _bucket(week: int) -> str:
    if week <= 3:
        return "1-3"
    if week <= 7:
        return "4-7"
    if week <= 11:
        return "8-11"
    return "12-17"


def _logit(p: float) -> float:
    p = max(0.005, min(0.995, p))
    return math.log(p / (1.0 - p))


def _logistic(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _smooth(success: int, total: int, base: float,
            prior: float = PRIOR_EVENTS) -> float:
    return (float(success) + prior * base) / max(1.0, float(total) + prior)


def _need_tier(count: int, pos: str) -> str:
    if count < STARTERS.get(pos, 1):
        return "shortage"
    if count < DEPTH.get(pos, 2):
        return "thin"
    return "stocked"


@lru_cache(maxsize=1)
def historical_panel() -> tuple[dict, ...]:
    """One manager/candidate row for every archived waiver opportunity.

    Rosters are reconstructed at the start of each transaction week by walking
    backward from Sleeper's final roster snapshot.  This avoids pretending the
    final roster was the roster on which an October bid was made.
    """
    if not DB.exists():
        return ()
    from robo import sleeper_read as api
    players = api.players()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("pragma table_info(transactions)")}
        created = "created" if "created" in cols else "0 as created"
        notes = "notes" if "notes" in cols else "'' as notes"
        tx = list(con.execute(
            "select season,week,type,status,roster_ids,adds,drops,waiver_bid,"
            f"{created},{notes} from transactions"))
        owners = {(str(r["season"]), int(r["roster_id"])): str(r["owner_id"])
                  for r in con.execute("select season,roster_id,owner_id from rosters")
                  if r["owner_id"]}
        finals: dict[str, dict[int, set[str]]] = defaultdict(dict)
        for r in con.execute("select season,roster_id,players from rosters"):
            finals[str(r["season"])][int(r["roster_id"])] = {
                str(p) for p in (_loads(r["players"], []) or [])}

        # Budget available before a week's claims. Same-week winning claims are
        # not charged until processing and therefore do not reduce one another.
        spent_before: dict[tuple[str, int, int], int] = defaultdict(int)
        for r in tx:
            if r["type"] != "waiver" or r["status"] != "complete":
                continue
            rids = _loads(r["roster_ids"], []) or []
            if not rids:
                continue
            for future_week in range(int(r["week"] or 0) + 1, 18):
                spent_before[(str(r["season"]), int(rids[0]), future_week)] += int(
                    r["waiver_bid"] or 0)

        @lru_cache(maxsize=None)
        def roster_at(season_key: str, week: int) -> tuple:
            state = {rid: set(ids) for rid, ids in finals.get(season_key, {}).items()}
            later = [r for r in tx if str(r["season"]) == season_key
                     and r["status"] == "complete" and int(r["week"] or 0) >= week]
            later.sort(key=lambda r: int(r["created"] or 0), reverse=True)
            for r in later:
                for pid, rid in (_loads(r["adds"], {}) or {}).items():
                    state.setdefault(int(rid), set()).discard(str(pid))
                for pid, rid in (_loads(r["drops"], {}) or {}).items():
                    state.setdefault(int(rid), set()).add(str(pid))
            return tuple((rid, tuple(sorted(ids))) for rid, ids in sorted(state.items()))

        auctions: dict[tuple[str, int, str], dict[int, int]] = defaultdict(dict)
        for r in tx:
            if r["type"] != "waiver" or r["waiver_bid"] is None:
                continue
            # Only an executed claim or a claim that lost on price represents
            # actual competition.  Roster-full, locked-drop and over-budget
            # failures never could have beaten another manager and otherwise
            # make the field look busier than it was.
            price_loss = str(r["notes"] or "").strip().lower().startswith(
                "this player was claimed by another owner")
            if r["status"] != "complete" and not price_loss:
                continue
            rids = _loads(r["roster_ids"], []) or []
            adds = _loads(r["adds"], {}) or {}
            if not rids:
                continue
            rid = int(rids[0])
            for pid in adds:
                key = (str(r["season"]), int(r["week"] or 0), str(pid))
                auctions[key][rid] = max(auctions[key].get(rid, -1),
                                         int(r["waiver_bid"] or 0))

        out = []
        for (yr, week, pid), bids in sorted(auctions.items()):
            pos = (players.get(pid) or {}).get("position") or (pid if pid in {"K", "DEF"} else "?")
            if pos not in SKILL:
                continue
            rosters = dict(roster_at(yr, week))
            for rid, held in rosters.items():
                uid = owners.get((yr, rid))
                if not uid:
                    continue
                count = sum(((players.get(q) or {}).get("position") == pos)
                            for q in held)
                bid = bids.get(rid)
                left = max(0, 100 - spent_before[(yr, rid, week)])
                out.append({"season": yr, "week": week, "bucket": _bucket(week),
                            "player_id": pid, "pos": pos, "roster_id": rid,
                            "manager_id": uid, "need": _need_tier(count, pos),
                            "pos_count": count, "faab_left": left,
                            "claimed": bid is not None, "bid": bid})
        return tuple(out)
    finally:
        con.close()


def _rate(rows: list[dict], base: float, predicate=lambda r: True,
          field: str = "claimed") -> tuple[float, int]:
    cell = [r for r in rows if predicate(r)]
    return _smooth(sum(bool(r.get(field)) for r in cell), len(cell), base), len(cell)


def _claim_probability(panel: list[dict], manager_id: str, week: int,
                       pos: str, need: str) -> tuple[float, dict]:
    base = sum(bool(r["claimed"]) for r in panel) / max(1, len(panel))
    mgr, nm = _rate(panel, base, lambda r: r["manager_id"] == manager_id)
    wp, nw = _rate(panel, base, lambda r: r["bucket"] == _bucket(week)
                   and r["pos"] == pos)
    nd, nn = _rate(panel, base, lambda r: r["need"] == need and r["pos"] == pos)
    score = _logit(base)
    score += _logit(mgr) - _logit(base)
    score += _logit(wp) - _logit(base)
    score += _logit(nd) - _logit(base)
    p = max(0.005, min(0.95, _logistic(score)))
    return p, {"manager_samples": nm, "week_position_samples": nw,
               "need_samples": nn, "league_rate": round(base, 4)}


def _bid_pmf(panel: list[dict], manager_id: str, week: int, pos: str,
             need: str, faab_left: int) -> tuple[dict[int, float], dict]:
    claimed = [r for r in panel if r["claimed"] and r["bid"] is not None]
    if not claimed:
        return {0: 1.0}, {"bid_samples": 0, "manager_bid_samples": 0}
    base_positive = sum(r["bid"] > 0 for r in claimed) / len(claimed)
    mgr = [r for r in claimed if r["manager_id"] == manager_id]
    poscell = [r for r in claimed if r["pos"] == pos and r["bucket"] == _bucket(week)]
    needcell = [r for r in claimed if r["pos"] == pos and r["need"] == need]
    mp = _smooth(sum(r["bid"] > 0 for r in mgr), len(mgr), base_positive)
    pp = _smooth(sum(r["bid"] > 0 for r in poscell), len(poscell), base_positive)
    np = _smooth(sum(r["bid"] > 0 for r in needcell), len(needcell), base_positive)
    positive = _logistic(_logit(base_positive)
                         + _logit(mp) - _logit(base_positive)
                         + _logit(pp) - _logit(base_positive)
                         + _logit(np) - _logit(base_positive))

    base = [r for r in poscell if r["bid"] > 0] or [r for r in claimed if r["bid"] > 0]
    personal = [r for r in mgr if r["bid"] > 0]
    need_rows = [r for r in needcell if r["bid"] > 0]
    wm = len(personal) / (len(personal) + PRIOR_EVENTS)
    wn = 0.5 * len(need_rows) / (len(need_rows) + PRIOR_EVENTS)
    wb = max(0.0, 1.0 - wm - wn)
    weighted = [(r, wb / len(base)) for r in base]
    if personal:
        weighted += [(r, wm / len(personal)) for r in personal]
    if need_rows:
        weighted += [(r, wn / len(need_rows)) for r in need_rows]
    pmf: dict[int, float] = defaultdict(float)
    pmf[0] = 1.0 - positive
    for r, weight in weighted:
        hist_left = max(1, int(r["faab_left"] or 0))
        scale = max(1.0 / BID_SCALE_CAP,
                    min(BID_SCALE_CAP, faab_left / hist_left))
        amount = min(faab_left, max(1, int(round(r["bid"] * scale))))
        pmf[amount] += positive * weight
    total = sum(pmf.values()) or 1.0
    return {int(k): v / total for k, v in pmf.items()}, {
        "bid_samples": len(base), "manager_bid_samples": len(mgr),
        "positive_bid_probability": round(positive, 4)}


def _player_row(table: dict, pid: str) -> dict:
    return (table.get("players") or {}).get(str(pid)) or {}


def _deterministic_need(table: dict, roster: dict, candidate_id: str) -> dict:
    """Current-model lineup gain for one opponent, without new random worlds."""
    rows = table.get("players") or {}
    weights = {int(k): float(v) for k, v in (table.get("weights") or {}).items()}
    held = [str(p) for p in (roster.get("players") or []) if str(p) in rows]
    reserve = {str(p) for p in (roster.get("reserve") or [])}
    candidate_id = str(candidate_id)
    if candidate_id not in rows:
        return {"gain": 0.0, "starts": 0, "need": "unknown", "drop": None}
    pos = rows[candidate_id].get("pos")
    count = sum(rows[p].get("pos") == pos for p in held)
    starts, gain = 0, 0.0
    skill_slots = [s for s in lineup.SLOTS if s not in ("K", "DEF")]
    for week, weight in weights.items():
        def score(ids, mark=False):
            cands = []
            for pid in ids:
                row = rows.get(pid) or {}
                cell = (row.get("by_week") or {}).get(str(week)) or {}
                if not row or pid in reserve or not cell:
                    continue
                cands.append({"player_id": pid, "name": row.get("name") or pid,
                              "pos": row.get("pos"), "pts": float(cell.get("final") or 0),
                              "has_game": True, "locked": False, "injury": None})
            saved = lineup.SLOTS
            try:
                lineup.SLOTS = skill_slots
                chosen, _ = lineup.optimize(cands)
            finally:
                lineup.SLOTS = saved
            picked = {c.get("player_id") for c in chosen if c}
            return sum(float(c.get("pts") or 0) for c in chosen if c), candidate_id in picked
        before, _ = score(held)
        after, used = score(held + [candidate_id])
        gain += weight * (after - before)
        starts += int(used)
    # Keep the historical and live feature on the same definition.  `starts`
    # and `gain` are richer current evidence shown separately; relabeling an
    # eight-WR roster "shortage" merely because the candidate wins three bye
    # weeks would feed a category into the model that meant something different
    # during training.
    tier = _need_tier(count, pos)
    protected = {str(p) for p in (roster.get("starters") or [])}
    droppable = [p for p in held if p not in protected and p not in reserve
                 and rows[p].get("pos") in SKILL]
    drop = min(droppable, key=lambda p: float(rows[p].get("ros") or 0)) if droppable else None
    return {"gain": round(gain, 3), "starts": starts, "need": tier,
            "pos_count": count, "drop": drop}


def _quantile(pmf: dict[int, float], q: float) -> int:
    running = 0.0
    for value, prob in sorted(pmf.items()):
        running += prob
        if running >= q:
            return int(value)
    return int(max(pmf, default=0))


def _crps(pmf: dict[int, float], observed: int) -> float:
    """Discrete ranked-probability error for one conditional bid forecast."""
    top = max([int(observed), 0] + [int(k) for k in pmf])
    forecast = 0.0
    score = 0.0
    for amount in range(0, top + 1):
        forecast += float(pmf.get(amount, 0.0))
        actual = 1.0 if amount >= int(observed) else 0.0
        score += (forecast - actual) ** 2
    return score


@lru_cache(maxsize=1)
def validation() -> dict:
    """Leave-one-season-out comparison with the pooled production fallback.

    Claim entry is scored with Brier loss.  Bid amount is scored with CRPS,
    because production consumes the full CDF at every legal dollar rather than
    a point estimate.  MAE of the distribution mean rides along as a useful
    warning: it is not allowed to masquerade as the metric the optimizer uses.
    """
    panel = list(historical_panel())
    years = sorted({r["season"] for r in panel})
    demand_model, demand_pool = [], []
    bid_model, bid_pool = [], []
    mean_model, mean_pool = [], []
    for year in years:
        train = [r for r in panel if r["season"] != year]
        test = [r for r in panel if r["season"] == year]
        if not train or not test:
            continue
        base = sum(bool(r["claimed"]) for r in train) / len(train)
        for row in test:
            predicted, _ = _claim_probability(
                train, row["manager_id"], row["week"], row["pos"], row["need"])
            actual = float(bool(row["claimed"]))
            demand_model.append((predicted - actual) ** 2)
            demand_pool.append((base - actual) ** 2)
            if not row["claimed"] or row["bid"] is None:
                continue
            personal, _ = _bid_pmf(
                train, row["manager_id"], row["week"], row["pos"],
                row["need"], row["faab_left"])
            pooled, _ = _bid_pmf(
                train, "__pooled__", row["week"], row["pos"],
                "__pooled__", row["faab_left"])
            observed = int(row["bid"])
            bid_model.append(_crps(personal, observed))
            bid_pool.append(_crps(pooled, observed))
            mean_model.append(abs(sum(k * p for k, p in personal.items()) - observed))
            mean_pool.append(abs(sum(k * p for k, p in pooled.items()) - observed))

    def avg(values):
        return round(sum(values) / len(values), 6) if values else None

    db, pb = avg(demand_model), avg(demand_pool)
    dc, pc = avg(bid_model), avg(bid_pool)
    demand_pass = db is not None and pb is not None and db <= pb
    bid_pass = dc is not None and pc is not None and dc <= pc
    return {
        "method": "leave-one-season-out", "seasons": years,
        "demand_rows": len(demand_model), "bid_rows": len(bid_model),
        "demand_brier": db, "pooled_demand_brier": pb,
        "bid_crps": dc, "pooled_bid_crps": pc,
        "bid_mean_mae": avg(mean_model), "pooled_bid_mean_mae": avg(mean_pool),
        "demand_pass": demand_pass, "bid_distribution_pass": bid_pass,
        "passes": bool(demand_pass and bid_pass),
    }


def predict(ctx: dict, candidate_id: str, table: dict | None = None) -> dict:
    """Predict the full rival field and our integer-bid win curve."""
    from robo import expected
    panel = list(historical_panel())
    if not panel:
        return {"available": False, "reason": "historical manager panel unavailable"}
    checked = validation()
    if not checked.get("passes"):
        return {"available": False,
                "reason": "opponent model did not beat its pooled holdout baseline",
                "version": MODEL_VERSION, "validation": checked}
    table = table or expected.load()
    current = season.live_rosters(ctx["league_id"])
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        names = {str(u): d or str(u) for u, d in con.execute(
            "select user_id,max(display_name) from managers group by user_id")}
    finally:
        con.close()
    ours = next((r for r in current if str(r.get("owner_id")) == ROBOWNER_USER_ID), {})
    our_priority = int((ours.get("settings") or {}).get("waiver_position") or 999)
    week = int(ctx["week"])
    pos = _player_row(table, candidate_id).get("pos") or "?"
    opponents = []
    for roster in current:
        uid = str(roster.get("owner_id") or "")
        if not uid or uid == ROBOWNER_USER_ID:
            continue
        settings = roster.get("settings") or {}
        left = max(0, season.FAAB_BUDGET - int(settings.get("waiver_budget_used") or 0))
        priority = int(settings.get("waiver_position") or 999)
        need = _deterministic_need(table, roster, candidate_id)
        pclaim, evidence = _claim_probability(panel, uid, week, pos, need["need"])
        bid_given_claim, bid_evidence = _bid_pmf(panel, uid, week, pos,
                                                 need["need"], left)
        pmf = {-1: 1.0 - pclaim}
        for bid, prob in bid_given_claim.items():
            pmf[int(bid)] = pmf.get(int(bid), 0.0) + pclaim * prob
        total = sum(pmf.values()) or 1.0
        pmf = {k: v / total for k, v in pmf.items()}
        opponents.append({"roster_id": roster.get("roster_id"), "manager_id": uid,
                          "manager": names.get(uid, uid), "faab_left": left,
                          "waiver_position": priority, "need": need,
                          "p_claim": round(pclaim, 4),
                          # These are conditional bid tendencies.  P(claim) is
                          # already shown separately; mixing the no-claim mass
                          # into a field labelled "bid p50" rendered many
                          # managers as -$1, which was mathematically useful
                          # internally and nonsensical to a reviewer.
                          "bid_p50": _quantile(bid_given_claim, .5),
                          "bid_p75": _quantile(bid_given_claim, .75),
                          "bid_p90": _quantile(bid_given_claim, .9),
                          "evidence": {**evidence, **bid_evidence}, "pmf": pmf})

    max_bid = max([ctx.get("faab", 0)] + [o["faab_left"] for o in opponents])
    max_cdf = {}
    for b in range(-1, max_bid + 1):
        prob = 1.0
        for o in opponents:
            prob *= sum(p for amount, p in o["pmf"].items() if amount <= b)
        max_cdf[b] = prob
    max_pmf, prev = {}, 0.0
    for b in range(-1, max_bid + 1):
        max_pmf[b] = max(0.0, max_cdf[b] - prev)
        prev = max_cdf[b]
    expected_max = sum(max(0, b) * p for b, p in max_pmf.items())
    field_quantiles = {f"p{int(q * 100)}": max(0, _quantile(max_pmf, q))
                       for q in (.5, .75, .9)}
    win_curve = []
    for our_bid in range(0, int(ctx.get("faab", 0)) + 1):
        win = 1.0
        for o in opponents:
            beat = 0.0
            for their_bid, prob in o["pmf"].items():
                if their_bid < our_bid or (their_bid == our_bid
                                           and our_priority < o["waiver_position"]):
                    beat += prob
            win *= beat
        win_curve.append(round(win, 6))
    return {"available": True, "version": MODEL_VERSION, "candidate_id": str(candidate_id),
            "position": pos, "our_waiver_position": our_priority,
            "opponents": opponents, "expected_highest": round(expected_max, 2),
            "highest_quantiles": field_quantiles, "max_pmf": max_pmf,
            "win_curve": win_curve, "training_rows": len(panel),
            "validation": checked}
