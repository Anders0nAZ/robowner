"""Live draft agent: poll the Sleeper draft, pick on our turn.

Pick policy (17-round 2QB build):
- a DP across ALL our remaining picks, scored in POINTS: our projection blended
  half-and-half with the expert field (rankings.blend_pts). Market ADP decides
  WHEN, via P(gone) = normal(adp, sigma). The board's blend_RANK is a different
  thing and is not what the policy reads.
- positional caps; QB urgency in a superflex league; exactly 1 K + 1 DEF and
  only in the final two rounds; avoid injured-out/suspended players
- dry-run mode prints the recommendation instead of submitting. The
  draft_pick_player mutation is verified: 60 of 60 picks submitted and accepted
  across four live Sleeper mocks on 30 Aug 2026.

Usage:
  python -m robo.draft_agent --dry-run          # poll + recommend only
  python -m robo.draft_agent                    # poll + submit picks
  python -m robo.draft_agent --once             # single recommendation, no loop
"""

import argparse
import json
import math
import time

from robo import DATA, DRAFT_ID_2026, ROBOWNER_USER_ID
from robo import sleeper_read as api
from robo import bench as _bench
from robo.rankings import build_board

TEAMS = 12
ROUNDS = 17
MAX_AT_POS = {"QB": 3, "RB": 6, "WR": 7, "TE": 2, "K": 1, "DEF": 1}
# Minimum viable roster we must be able to complete.
# TE is 2, not 1. Once bench value was measured against the WAIVER WIRE rather
# than the last starter, a backup tight end priced out: the 25th-best TE
# projects 101.8, so TE2 is worth ~34 points over the wire where a spare
# receiver is worth ~50-67. The model then accepted a guaranteed one-week bye
# hole (BYE_HOLE_COST 4.0) to buy that receiver, and every run came back with a
# single TE and no margin at all against MIN_AT_POS. The arithmetic is right and
# the roster is still wrong, so the floor -- not the valuation -- says so.
# Verified free: identical starting points across four seeds either way.
MIN_AT_POS = {"QB": 2, "RB": 4, "WR": 4, "TE": 2, "K": 1, "DEF": 1}
BAD_STATUS = {"Out", "Suspended", "IR", "PUP", "NA", "Doubtful"}
# How often to re-pull the player dump DURING the draft. The board bakes
# injury_status in when it is built, and sleeper_read caches the dump for 24h,
# so without this a knee blown out on Saturday night is invisible at Sunday's
# 3pm draft -- the one failure that cannot be undone afterwards.
STATUS_REFRESH_SECS = 600
_refresh_soon: set = set()
# How deep to keep our autopick queue, and how often to push it.
QUEUE_DEPTH = 15
# Sleeper accepts an unbounded queue -- the full 631-player board went in fine --
# so depth is free and the fallback should never run dry. It cannot simply be the
# board in rank order, though: kickers and defences sit at board rank 76-114, and
# our eighth pick is overall 91, so a naive dump autopicks a defence in round 7
# and wastes it. Skill players first to unlimited depth, K and DEF appended last
# where they can only be reached once everything else is gone.
QUEUE_PAD_TO = 0  # 0 = no cap
QUEUE_REFRESH_SECS = 45
# Stamped every poll so the draft guard can tell "running" from "working".
# Checking that a process exists is not a liveness check: a hung agent satisfies
# it forever, which is the same failure that has now bitten this project four
# times (Ollama holding the port on CPU, the chat cursor, the refresh task, and
# the guard itself). 90s is generous -- the off-clock injury refresh pulls 16MB
# and can stall a loop for ~30s, and a false restart costs one pick at most.
DRAFT_HEARTBEAT = DATA / "draft_heartbeat.json"
HEARTBEAT_STALE_SECS = 90
# Hard ceilings on what the QUEUE may contain, per position, regardless of depth.
# Waves cap a position per wave, but our top entries get taken by other teams
# fast, so the pointer runs into later waves early and they cheerfully offer more
# quarterbacks: a full contingency draft ended round 9 with FIVE QBs and one WR.
# A frozen queue cannot know we already drafted three, so the ceiling has to be
# absolute. RB and WR stay deep -- they fill the flex and most of the bench, and
# an extra one costs nothing.
QUEUE_MAX_AT_POS = {"QB": 5, "TE": 4, "RB": 99, "WR": 99}


def draft_state(draft_id: str = DRAFT_ID_2026):
    d = api.draft(draft_id)
    picks = api.draft_picks(draft_id)
    return d, picks


def my_slot(draft: dict) -> int | None:
    order = draft.get("draft_order") or {}
    return order.get(ROBOWNER_USER_ID)


def pick_to_slot(overall: int) -> int:
    rd = (overall - 1) // TEAMS + 1
    pos = (overall - 1) % TEAMS + 1
    return pos if rd % 2 == 1 else TEAMS + 1 - pos


# Rounds we forfeit to keepers (Collins R2, Hampton R3). Only a fallback for
# running the policy offline -- the live agent derives this from the board.
KEEPER_ROUNDS = {2, 3}


def next_pick_no(picks: list[dict]) -> int:
    """Lowest pick number still unfilled -- i.e. the pick actually on the clock.

    NOT len(picks) + 1. Keeper assignments occupy their own pick numbers on the
    board BEFORE the draft opens (24 of them, scattered from #8 to #135), so a
    count of picks made runs 24 ahead of reality on day one and never
    reconverges. That number is also what gets submitted to draft_pick(), so
    the old version would have picked at the wrong slot and stamped the pick
    with the wrong number all draft.
    """
    filled = {p["pick_no"] for p in picks}
    for n in range(1, TEAMS * ROUNDS + 1):
        if n not in filled:
            return n
    return TEAMS * ROUNDS + 1


def my_pick_numbers(slot: int, filled: set[int] | None = None,
                    keeper_rounds: set[int] = KEEPER_ROUNDS) -> list[int]:
    """Overall pick numbers we actually own and have not spent yet.

    Pass `filled` (every pick number already on the board) and the keepers and
    our completed picks drop out on their own -- no hardcoded round list to go
    stale if a keeper moves or we trade a pick. The keeper_rounds fallback is
    only for calling this without live draft state.
    """
    out = []
    for rd in range(1, ROUNDS + 1):
        pos = slot if rd % 2 == 1 else TEAMS + 1 - slot
        n = (rd - 1) * TEAMS + pos
        if filled is None:
            if rd in keeper_rounds:
                continue
        elif n in filled:
            continue
        out.append(n)
    return out


def _market(r: dict) -> float:
    """Where the market takes this player NOW (live FFC first — the locked ADP
    is the keeper-cost snapshot and ages through draft week)."""
    for k in ("adp_live", "adp_ffc", "adp_sleeper_2qb"):
        if r.get(k) is not None:
            return r[k]
    return 999.0


def _score(r: dict) -> float:
    """What a player is worth for a COMMITTED lineup slot.

    Our projection and the expert field, blended in points (rankings.blend_pts):
    the positional projection curve is kept and the experts only say who sits
    where on it. Used by BOTH shelf() and the starter selection, deliberately --
    if the DP planned a slot's value on one number and then filled it with a
    different one, every published "plan value" would be computed from a
    projection the pick then ignored.

    Falls back to the raw projection for a board built before blend_pts existed.
    """
    return r.get("blend_pts", r["proj_pts"])


def _sigma(r: dict) -> float:
    """Per-player ADP spread. FFC publishes it; the fallback matches the
    observed shape (tight early rounds, wide late)."""
    s = r.get("adp_stdev")
    if s:
        return max(float(s), 1.0)
    return max(4.0, 0.15 * _market(r))


def p_gone(r: dict, by_pick: int) -> float:
    """P(player is drafted before `by_pick`), normal model on ADP.

    Replaces the old fixed +6 buffer, which was too loose in round 1 (sigma
    there is ~1, so ADP 5 'surviving' to pick 11 was never real) and far too
    tight in round 8+ (sigma 10-20, so plenty of ADP-100 players genuinely
    last to pick 115).
    """
    return 0.5 * (1.0 + math.erf((by_pick - _market(r)) / (_sigma(r) * math.sqrt(2))))


P_GONE_TAKE = 0.40  # take-now threshold: meaningful risk he doesn't come back

# Marginal lineup value of the Nth player at a position (index = current count).
# Starters: 1 QB + SF, 2 RB, 2 WR, 1 TE, FLEX shared by RB/WR/TE. The 3rd
# RB/WR fights for flex; deeper is bench insurance.
START_WEIGHT = {
    "QB": [1.0, 1.0, 0.30],
    "RB": [1.0, 1.0, 0.75, 0.40, 0.25, 0.20],
    "WR": [1.0, 1.0, 0.75, 0.40, 0.25, 0.20, 0.20],
    "TE": [1.0, 0.30],
    "K": [1.0], "DEF": [1.0],
}
STARTER_NEEDS = {"QB": 2, "RB": 2, "WR": 2, "TE": 1}
FLEX_POS = ("RB", "WR", "TE")
BENCH_WEIGHT = 0.30

# data/settings.json overrides the constants above. Import-time, so a change
# there takes effect on the next run of this module -- see robo/settings.py.
from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())


def beat(draft_id: str, picks_n: int, next_no: int, status: str) -> None:
    """Stamp liveness. Carries the draft_id so the guard cannot be fooled by a
    heartbeat left behind by a mock run against a different draft."""
    try:
        DRAFT_HEARTBEAT.write_text(json.dumps({
            "ts": time.time(), "draft_id": draft_id, "picks": picks_n,
            "next_pick": next_no, "status": status}), encoding="utf-8")
    except Exception:
        pass


def live_status(force: bool = False, block: bool = True, _cache: dict = {}) -> dict:
    """player_id -> CURRENT injury status, re-pulled on a timer.

    Deliberately independent of the board: the board's copy is frozen when the
    agent starts, which is hours before the draft ends.
    """
    now = time.time()
    due = force or now - _cache.get("at", 0) > STATUS_REFRESH_SECS
    if due and not block and _cache.get("map"):
        # We are on the clock. The dump is ~16MB and re-pulling it here would
        # spend the pick timer on a download; take the slightly stale map and
        # let the next off-clock poll refresh it.
        _refresh_soon.add(1)
        return _cache["map"]
    if due:
        try:
            dump = api.players(refresh=True)
            _cache["at"] = now
            _cache["map"] = {pid: (v.get("injury_status") or "")
                             for pid, v in dump.items()}
        except Exception as e:
            print(f"!! could not refresh injury status ({e}); using last known",
                  flush=True)
    return _cache.get("map", {})


def build_queue(board: list[dict], taken: set[str], counts: dict[str, int],
                roster: list[dict], future: list[int], status: dict | None = None,
                depth: int = QUEUE_DEPTH, own_league: bool = True) -> list[str]:
    """Our autopick queue: the plan, in order, as player_ids.

    Sleeper autopicks from this queue in preference to its own rankings, so it
    is the graceful-degradation layer for the agent dying, the machine dropping
    off, or a pick simply being missed. Without it, an absent Robowner drafts
    off Sleeper ADP -- which in the 28 Aug mock spent our 1.01 on a receiver we
    would never have taken.

    Built by running the real pick policy forward, taking each choice and asking
    again, so the queue is what we would actually have done rather than a flat
    best-available list that ignores roster construction.
    """
    q: list[str] = []
    t, c, r = set(taken), dict(counts), list(roster)
    # Never queue a player the league has kept. In the real draft they are
    # already on the board so `taken` covers it, but a practice mock on Sleeper
    # has no keepers, and the queue happily led with Josh Allen and Jahmyr
    # Gibbs -- names that cannot come to us in the draft that matters.
    if own_league:
        try:
            from robo.league_keepers import kept_ids
            t |= kept_ids()
        except Exception:
            pass
    for i in range(min(depth, len(future))):
        pick, _ = choose_pick(board, t, c, len(future) - i, overall=future[i],
                              next_overall=future[i + 1] if i + 1 < len(future) else 0,
                              future_picks=future[i:], roster=r, status=status)
        if not pick:
            break
        q.append(pick["player_id"])
        t.add(pick["player_id"])
        c[pick["pos"]] = c.get(pick["pos"], 0) + 1
        r = r + [pick]

    # Pad with alternates: best board value that still fits a position we are
    # allowed to add, skipping anyone hurt. These only ever get used if the
    # plan above has been picked out from under us.
    # room comes from what we ACTUALLY hold, not from `c` after the simulation:
    # the plan fills every position to its cap, so measuring there left no room
    # and the padding added three names instead of thirty. These are alternates
    # FOR the plan's picks, not extra picks on top of them.
    # K/DEF placement is the whole endgame. A queue is consumed in ORDER, not by
    # round, so leaving them at plan position 14-15 lets a dead agent autopick a
    # defence in round 4. But burying them at the bottom of a 500-name queue is
    # the opposite failure: with only 15 picks the pointer never gets near them,
    # and a permanently-dead agent finishes the season with no kicker and no
    # defence at all. So it depends on how many picks are actually left -- once
    # they are the only thing we still need, they belong at the TOP.
    kd_short = (1 - counts.get("K", 0)) + (1 - counts.get("DEF", 0))
    if len(future) <= kd_short + 2:
        return q            # endgame: the planner already put K/DEF first
    q = [pid for pid in q
         if (board_by_id := {r["player_id"]: r for r in board}).get(pid, {}).get("pos")
         not in ("K", "DEF")]
    t |= set(q)   # NOT `set(taken) | set(q)`: that reset the kept-player
                  # exclusion added above and put all 24 back in the padding

    ranked = sorted(board, key=lambda x: x["blend_rank"])

    def usable(cand):
        if cand["player_id"] in t:
            return False
        hurt = (status or {}).get(cand["player_id"], cand.get("injury_status"))
        return (hurt or "") not in BAD_STATUS

    # Pad by least-filled position, NOT in flat board order. Sleeper does not
    # enforce anything useful on a queue autopick -- with enforce_position_limits
    # on, a rank-ordered queue still drafted SEVEN consecutive quarterbacks in a
    # mock, because seven fit on the bench. blend_rank ranks QBs high in a 2QB
    # league, so a flat list is a QB pileup. Always take the best remaining
    # player at whichever position is furthest from its cap, which keeps the
    # queue balanced however deep it runs.
    pool: dict[str, list] = {}
    for cand in ranked:
        if cand["pos"] in ("K", "DEF") or not usable(cand):
            continue
        pool.setdefault(cand["pos"], []).append(cand)
    # Strict waves, not just a ratio. Ranking by fill/cap deprioritises a full
    # position but never stops it, which put a THIRD tight end (cap 2) in the
    # queue early enough to be autopicked in round 8. Wave 1 is a complete legal
    # roster and nothing more; later waves are pure depth, and by the time a
    # frozen queue reaches them the draft is long past caring.
    fill = {pos: c.get(pos, 0) for pos in pool}
    wave = 1
    while any(pool.values()):
        if QUEUE_PAD_TO and len(q) >= QUEUE_PAD_TO:
            break
        avail = [p for p in pool if pool[p] and fill[p] < MAX_AT_POS[p] * wave
                 and fill[p] < QUEUE_MAX_AT_POS.get(p, 99)]
        if not avail:
            # Bumping the wave only helps if some position is still under its
            # ABSOLUTE ceiling. Without this check the loop spins forever once
            # RB/WR are exhausted and QB/TE are capped.
            if not any(pool[x] and fill[x] < QUEUE_MAX_AT_POS.get(x, 99)
                       for x in pool):
                break
            wave += 1
            continue
        pos = min(avail, key=lambda p: (fill[p] / MAX_AT_POS[p], p))
        cand = pool[pos].pop(0)
        fill[pos] += 1
        q.append(cand["player_id"])
        t.add(cand["player_id"])

    # Exactly one of each, at the very bottom: reachable only if the skill board
    # is exhausted, which is the last two rounds and nowhere else.
    for pos in ("DEF", "K"):
        for cand in ranked:
            if cand["pos"] == pos and usable(cand):
                q.append(cand["player_id"])
                t.add(cand["player_id"])
                break
    return q


def choose_pick(board: list[dict], taken: set[str], counts: dict[str, int],
                picks_left: int, overall: int = 0, next_overall: int = 0,
                future_picks: list[int] | None = None,
                roster: list[dict] | None = None,
                status: dict | None = None) -> tuple[dict | None, str]:
    """Best available honoring roster construction. Returns (player, reason).

    Ranks on _score() -- our projection blended half-and-half with the expert
    field, in points -- NOT on VORP and NOT on blend_rank. It used to say VORP,
    and said so while ranking on raw projected points, which is how a 0.7-point
    projection edge took a quarterback the field ranked 15th over one it ranked
    3rd. The blend is what now guards against a projection outlier the whole
    expert field hates.

    Points rather than VORP is deliberate for the STARTER branches: the slot is
    already committed, and a quarterback in the superflex outscores any receiver
    by ~110 real points. Scoring the whole DP on VORP drafts two quarterbacks
    and then force-picks a -130 backup in round 15.

    The bench branch asks a different question entirely -- see robo/bench.py.
    """
    kd_needed = (1 - counts["K"]) + (1 - counts["DEF"])
    kd_only = picks_left <= kd_needed
    # rounds we must reserve for K/DEF at the end
    candidates = []
    for r in board:
        if r["player_id"] in taken:
            continue
        pos = r["pos"]
        if counts[pos] >= MAX_AT_POS[pos]:
            continue
        if pos in ("K", "DEF"):
            if not kd_only:
                continue  # K/DEF strictly in the final needed rounds
        elif kd_only:
            continue
        # live status wins over the board's frozen copy
        hurt = (status or {}).get(r["player_id"], r.get("injury_status"))
        if (hurt or "") in BAD_STATUS:
            continue
        candidates.append(r)
    if not candidates:
        return None, "no candidates (caps hit?)"

    # urgency: if remaining picks are barely enough to satisfy minimums, restrict
    deficits = {p: max(0, MIN_AT_POS[p] - counts[p]) for p in MIN_AT_POS}
    must_fill = sum(deficits.values())
    if must_fill >= picks_left:
        needed = [r for r in candidates if deficits.get(r["pos"], 0) > 0]
        if needed:
            best = min(needed, key=lambda r: r["blend_rank"])
            short = ", ".join(f"{n}x{p}" if n > 1 else p
                              for p, n in sorted(deficits.items()) if n)
            return best, (f"Roster requirement: we still need {short} and have only "
                          f"{picks_left} pick{'' if picks_left == 1 else 's'} left, so this "
                          f"slot has to be filled now rather than taken on merit.")

    # Plan across ALL our remaining picks, not just the next one. One-step
    # value-over-next-available procrastinates fatally: "the best QB survives
    # one more hop" is true at every single hop while the shelf erodes 5-10
    # points each time, and the QB room ends up Penix + Mac Jones. The DP
    # assigns unfilled starter needs (2QB/2RB/2WR/1TE + flex) to our actual
    # future pick numbers using positional shelf curves (best projected player
    # whose market ADP is after that pick), then takes NOW whatever the plan
    # says is costliest to defer. Surplus picks score as bench depth.
    my_future = future_picks if future_picks else [overall]

    # candidates and `overall` are fixed for this call, so shelf is a pure
    # function of (pos, pick) and the cache is behaviour-identical -- it just
    # stops the DP rescanning 600 players for every state it revisits.
    from functools import lru_cache as _lru

    @_lru(maxsize=None)
    def shelf(pos, pick):
        """Best projected points still on the shelf at `pick` for this position."""
        best_pts = 0.0
        for r in candidates:
            if r["pos"] == pos and (pick <= overall or _market(r) + _sigma(r) / 2 > pick):
                best_pts = max(best_pts, _score(r))
        return best_pts

    needs = []
    for pos, want in STARTER_NEEDS.items():
        needs += [pos] * max(0, want - counts[pos])
    flex_filled = sum(max(0, counts[p] - STARTER_NEEDS[p]) for p in FLEX_POS)
    if flex_filled < 1:
        needs.append("FLEX")

    from functools import lru_cache
    npicks = my_future

    @lru_cache(maxsize=None)
    def V(i, needs_key):
        if i >= len(npicks):
            return 0.0
        rem = list(needs_key)
        options = []
        pick = npicks[i]
        if rem:
            for pos in set(rem):
                r2 = list(rem)
                r2.remove(pos)
                pts = (max(shelf(p, pick) for p in FLEX_POS) if pos == "FLEX"
                       else shelf(pos, pick))
                options.append(pts + V(i + 1, tuple(sorted(r2))))
        # spending this pick on bench depth instead
        options.append(BENCH_WEIGHT * max(shelf(p, pick) for p in STARTER_NEEDS)
                       + V(i + 1, needs_key))
        return max(options)

    needs_key = tuple(sorted(needs))
    pick_now = npicks[0]
    best_choice, best_total = None, float("-inf")
    # Deterministic evaluation order. This used to iterate a bare set of
    # strings, whose order changes between PROCESSES under Python's hash
    # randomisation, so on an exact tie the identical board could produce a
    # different pick run to run -- unreproducible mocks, and a bot that
    # publishes its reasoning contradicting itself. Ties now go to the most
    # specific claim: a named starter need beats FLEX (which leftovers can fill
    # later), and both beat bench depth.
    TIE_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "FLEX": 4, "BENCH": 5}
    for pos in sorted(set(needs) | {"BENCH"}, key=lambda p: TIE_ORDER[p]):
        if pos == "BENCH":
            total = BENCH_WEIGHT * max(shelf(p, pick_now) for p in STARTER_NEEDS) \
                    + V(1, needs_key)
            pool = candidates
        else:
            r2 = list(needs)
            r2.remove(pos)
            poss = FLEX_POS if pos == "FLEX" else (pos,)
            pool = [r for r in candidates if r["pos"] in poss]
            if not pool:
                continue
            total = max(r["proj_pts"] for r in pool) + V(1, tuple(sorted(r2)))
        if total > best_total:
            best_total, best_choice, best_pool = total, pos, pool
    # A starter branch has already committed to a slot, so the best player for
    # it is simply the highest scorer in that pool. The BENCH branch has not:
    # its pool is every position at once, and ranking that by raw points was the
    # bug -- it drafted the highest-scoring body on the board every time, which
    # in 2QB scoring is a quarterback we would never start.
    # A starter branch has already committed to a lineup slot, so raw points is
    # the right ranking there -- and genuinely right, not merely conventional: a
    # QB in the superflex slot outscores any WR by ~110 real points, which is
    # why scoring the whole DP on VORP is a mistake (it drafts two QBs and then
    # force-picks a -130 backup in round 15).
    #
    # The BENCH branch never committed to a slot, so neither points nor VORP
    # asks the right question -- see robo/bench.py. Its pool is every position
    # at once, and what we want from it is insurance on our own starters,
    # tickets on other people's, and bye weeks that add up.
    bench_note = ""
    if best_choice == "BENCH" and roster is not None:
        players, depth = _bench.context()
        by_id = {r["player_id"]: r for r in board}
        scored = []
        for r in best_pool:
            val, why = _bench.score(r, roster, by_id, players, depth, picks_left)
            # Spending THIS pick on him only buys anything he would not have
            # given us for free later. A handcuff going at ADP 263 is still
            # there in five rounds; without this the bench logic reached ~170
            # picks past market for insurance and drafted an undrafted QB.
            urgency = p_gone(r, next_overall) if next_overall else 1.0
            scored.append((val * urgency, why, val, r))
        pick_val, bench_note, raw, best = max(scored, key=lambda t: (t[0], t[3]["vorp"]))
        if raw:
            gone_pct = 100 * pick_val / raw
            bench_note += ("; and he would almost certainly be gone by our next pick"
                           if gone_pct >= 85 else
                           f"; roughly a {gone_pct:.0f}% chance he is gone by our next pick")
    elif best_choice == "BENCH":
        best = max(best_pool, key=lambda r: (r["vorp"], r["proj_pts"]))
    else:
        best = max(best_pool, key=lambda r: (_score(r), r["vorp"]))
    # This string is PUBLISHED VERBATIM as the rationale on the league's decision
    # log -- draft_agent passes it straight to decisions.record(). It is the only
    # account the league gets of why a pick happened, so it is written for them
    # and not for us. What went out before was debug output: "plan value 1932
    # over picks [6, 43, 54, 67, 78]" is an internal DP score with no units, on
    # every entry, and it told a reader nothing at all.
    #
    # ASCII ONLY. This same string is print()ed to draft-agent.log, which the
    # guard opens with the console encoding; an em dash or a plus-minus is one
    # UnicodeEncodeError away from killing the agent on its own explanation.
    if bench_note:
        body = "Bench: " + bench_note
        aside = ""
    elif best_choice == "BENCH":
        body, aside = "Bench depth", ""
    else:
        where = ("our flex slot, taking the best of the remaining running backs, "
                 "receivers and tight ends" if best_choice == "FLEX"
                 else f"a starting {best_choice} slot")
        body = f"Fills {where}, projected {best['proj_pts']:.0f} points"
        # Only mention the margin when it is a real one. A one-point edge is
        # noise dressed up as a reason. Measured on the number the decision was
        # actually made with, which is the projection blended with the experts.
        runner_up = max((r for r in best_pool if r["player_id"] != best["player_id"]),
                        key=_score, default=None)
        if runner_up is not None:
            edge = _score(best) - _score(runner_up)
            if edge >= 5:
                body += f", {edge:.0f} clear of the next man up for it"
        # Its own sentence. Stacked on as a third comma clause it read as a
        # splice, and this is the part a reader is most likely to care about.
        #
        # ATTRIBUTION MATTERS HERE. This gap is between our two INPUTS -- the
        # Sleeper projections and the expert consensus -- not between us and the
        # experts. Our own view is the blend, which sits exactly between them.
        # Saying "we like him more than the expert field does" claimed a stance
        # the bot does not hold, on a page the league reads.
        ep = best.get("expert_pts")
        gap = (ep - best["proj_pts"]) if ep is not None else 0.0
        aside = (" The expert field is higher on him than our projections are;"
                 " this pick splits the difference." if gap >= 5
                 else " Our projections are higher on him than the expert field is;"
                      " this pick splits the difference." if gap <= -5
                 else "")
    note = (f"{body}.{aside} The market usually takes him around "
            f"pick {_market(best):.0f}.")
    return best, note


def run(dry_run: bool, once: bool, poll_secs: float = 3.0,
        draft_id: str = DRAFT_ID_2026, allow_real: bool = False):
    """Poll the draft and pick on our turn.

    `draft_id` exists so the whole live path can be exercised against a throwaway
    mock. Submitting into the real draft therefore needs --i-mean-it as well as
    the absence of --dry-run: a test run that quietly picks in the league draft
    is not recoverable, and the real draft is the one place we get no second try.
    """
    if draft_id == DRAFT_ID_2026 and not dry_run and not allow_real:
        raise SystemExit("refusing to submit picks into the REAL 2026 draft "
                         "without --i-mean-it (use --dry-run, or --draft-id <mock>)")
    board = build_board()
    from robo.decisions import record

    # Say out loud what changed since the board was built, so a human watching
    # can sanity-check it rather than discovering it in the pick log.
    st = live_status(force=True)
    newly = [r for r in board
             if (st.get(r["player_id"], "") or "") in BAD_STATUS
             and (r.get("injury_status") or "") not in BAD_STATUS]
    newly.sort(key=lambda r: r["blend_rank"])
    if newly:
        print(f"!! {len(newly)} players hurt since the board was built; "
              f"they will not be drafted:", flush=True)
        for r in newly[:10]:
            print(f"     board {r['blend_rank']:>6.1f}  {r['name']:<22} {r['pos']:<4} "
                  f"-> {st.get(r['player_id'])}", flush=True)
    else:
        print("injury check: nothing new since the board was built", flush=True)

    # Sleeper's draft room has an autopick toggle tied to the queue, and a
    # claimed slot with a queue set defaults to ON. With it on, Sleeper drafts
    # from the queue the instant our clock opens and every live submission comes
    # back "this pick could not be processed" -- the fallback silently replacing
    # the agent on every pick. Off, the agent picks and the queue still covers a
    # clock that actually expires. Note draft_autopickers() reports an empty
    # list either way, so it cannot be used to check this.
    if not dry_run:
        try:
            from robo.sleeper_write import gql
            gql("remove_user_from_autopick",
                'mutation remove_user_from_autopick { remove_user_from_autopick('
                'draft_id: "%s") }' % draft_id)
            print("autopick disabled for our slot (queue remains as fallback)", flush=True)
        except Exception as e:
            print(f"!! could not disable autopick: {e}", flush=True)

    _queue = {"ids": [], "at": 0.0, "n": -1}
    # Pick numbers we watched go by. Anything at our slot that appears here
    # WITHOUT us having submitted it was made by autopick, which is the signal
    # that the agent is alive but not actually drafting.
    _ours_submitted: set[int] = set()
    _alerted: set[int] = set()

    consecutive_errors = 0
    while True:
        try:
            draft, picks = draft_state(draft_id)
            beat(draft_id, len(picks), next_pick_no(picks), draft.get("status", "?"))
            slot = my_slot(draft)
            if slot is None:
                print("draft_order not set yet; waiting...")
            else:
                taken = {p["player_id"] for p in picks}
                filled = {p["pick_no"] for p in picks}

                # Did a pick at OUR slot get made without us? That is the failure
                # the operator cannot see: the process is healthy, the log looks
                # normal, and the queue is quietly drafting. Alert once per pick.
                # OUR KEEPERS SIT AT OUR SLOT and are on the board before the
                # draft opens, so without this they read as picks made without
                # us: the bot publicly announced "pick 19 was made without me --
                # Nico Collins" about its own keeper. is_keeper is not reliable
                # (it comes back null for some), so the frozen pre-draft board is
                # the test.
                from robo.league_keepers import board_keepers as _bk
                try:
                    _keeper_picks = {k["pick_no"] for k in _bk()}
                except Exception:
                    _keeper_picks = set()
                for p in picks:
                    n = p["pick_no"]
                    if (pick_to_slot(n) != slot or n in _ours_submitted
                            or n in _alerted or n in _keeper_picks):
                        continue
                    _alerted.add(n)
                    if draft_id != DRAFT_ID_2026 or dry_run:
                        continue  # never shout about a mock
                    m = p.get("metadata") or {}
                    who = f"{m.get('first_name','')} {m.get('last_name','')}".strip()
                    from robo import alerts
                    res = alerts.blast(
                        f"HEADS UP: pick {n} (round {(n - 1) // TEAMS + 1}) was made "
                        f"without me — {who} ({m.get('position','?')}). That came off "
                        f"my autopick queue, not my live pick. I am still running; "
                        f"something is stopping me reaching the clock in time.",
                        key="missed-pick", draft_id=draft_id)
                    print(f"!! ALERT missed pick {n}: {res}", flush=True)
                # our keeper assignments sit at our slot too, so this counts Collins
                # and Hampton against the roster exactly as it should
                my_picks = [p for p in picks if p.get("picked_by") == ROBOWNER_USER_ID
                            or pick_to_slot(p["pick_no"]) == slot]
                counts = {p: 0 for p in MAX_AT_POS}
                for p in my_picks:
                    meta_pos = (p.get("metadata") or {}).get("position")
                    if meta_pos in counts:
                        counts[meta_pos] += 1
                next_overall = next_pick_no(picks)
                # /draft/picks is CDN-cached for ~10s, so a pick we just made still
                # reads as open. Measured over a full draft: 3-4 needless resubmits
                # per pick, every one rejected -- 60 wasted writes, and 11s per pick
                # where the agent thinks it is on a clock it has already answered.
                # What we submitted is more current than what Sleeper serves back.
                stale = next_overall in _ours_submitted
                on_clock = (pick_to_slot(next_overall) == slot
                            and draft["status"] == "drafting" and not stale)
                picks_left = ROUNDS - len(my_picks)
                if on_clock:
                    future = my_pick_numbers(slot, filled)
                    future = [next_overall] + [pk for pk in future if pk > next_overall]
                    # bench valuation needs the actual roster, not just counts
                    by_id = {r["player_id"]: r for r in board}
                    mine = [by_id[p["player_id"]] for p in my_picks
                            if p["player_id"] in by_id]
                    player, reason = choose_pick(board, taken, counts, picks_left,
                                                 overall=next_overall,
                                                 next_overall=future[1] if len(future) > 1 else 0,
                                                 future_picks=future, roster=mine,
                                                 status=live_status(block=False))
                    if player:
                        msg = f"pick {next_overall}: {player['name']} ({player['pos']}) — {reason}"
                        print(("DRY RUN " if dry_run else "") + msg)
                        if not dry_run:
                            from robo.sleeper_write import draft_pick  # verified pre-draft
                            try:
                                draft_pick(draft_id, player["player_id"], next_overall)
                            except Exception as e:
                                # A rejected pick is NOT fatal. It usually means the
                                # clock ran out and autopick took the slot while we
                                # were deciding, and the right response is to look at
                                # the board again next poll -- not to die and hand
                                # the remaining 15 rounds to autopick, which is what
                                # an uncaught raise did in the 28 Aug mock.
                                print(f"!! pick {next_overall} rejected: {e}", flush=True)
                                time.sleep(poll_secs)
                                continue
                            # Only the REAL draft goes in the public log. A mock
                            # run published three fake first-round picks to the
                            # league's site on 28 Aug before this existed: record()
                            # commits and pushes, so a test wrote to GitHub Pages.
                            _ours_submitted.add(next_overall)
                            if draft_id == DRAFT_ID_2026:
                                # title / decision / rationale are three fields on
                                # the public page, rendered one under the other.
                                # `msg` is the console line and CONTAINS the reason,
                                # so recording it as the decision printed the whole
                                # explanation twice, word for word: "Decision: pick
                                # 43: Jordan Love (QB) - Fills a starting QB slot..."
                                # then "Why: Fills a starting QB slot...". The
                                # decision is WHAT we did; the rationale is why.
                                record("draft-pick",
                                       f"Round {(next_overall-1)//TEAMS+1}, pick {next_overall}",
                                       f"Drafted {player['name']}, {player['pos']}, "
                                       f"{player.get('team') or 'FA'}.",
                                       reason, data={"board_row": player})
                            else:
                                print(f"   (mock draft {draft_id}: not logged publicly)",
                                      flush=True)
                else:
                    # NO QUEUE MAINTENANCE HERE. A set draft queue makes Sleeper
                    # autopick for us the instant the clock opens and rejects every
                    # live submission -- verified: identical pick REJECTED with a queue
                    # set, SUCCESS with it cleared. The queue and the agent cannot both
                    # be armed. RobonerDraftGuard sets a queue only once it has given
                    # up on restarting the agent; see --set-queue.
                    if stale:
                        print(f"   pick {next_overall} already submitted; waiting for the "
                              f"board to catch up", flush=True)
                    else:
                        print(f"pick {next_overall}, slot {pick_to_slot(next_overall)} on clock "
                              f"(we are {slot}); status={draft['status']}")
            consecutive_errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # The read that drives everything used to sit outside any handler, so a
            # Sleeper 5xx or a 30s timeout ended the draft for us. Transient errors
            # are normal on a Sunday afternoon; keep polling. The queue covers the
            # picks we miss while this is happening.
            consecutive_errors += 1
            print(f"!! poll failed ({consecutive_errors}): {e}", flush=True)
            if consecutive_errors == 3 and not dry_run and draft_id == DRAFT_ID_2026:
                from robo import alerts
                alerts.blast(
                    f"I have failed to read the draft board {consecutive_errors} times "
                    f"running ({type(e).__name__}). Still trying. My autopick queue is "
                    f"set, so picks will come off my plan if I cannot reach the clock.",
                    key="poll-failure", draft_id=draft_id)
            time.sleep(min(poll_secs * consecutive_errors, 15))
            continue
        if once:
            break
        time.sleep(poll_secs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--draft-id", default=DRAFT_ID_2026,
                    help="target a mock draft instead of the real one")
    ap.add_argument("--i-mean-it", action="store_true",
                    help="required to submit real picks into the 2026 league draft")
    ap.add_argument("--set-queue", action="store_true",
                    help="push the autopick queue and exit. LAST RESORT: a set queue "
                         "blocks live picks, so only the guard should call this, and "
                         "only after it has given up restarting the agent.")
    args = ap.parse_args()

    if args.set_queue:
        from robo.rankings import build_board as _bb
        from robo.sleeper_write import set_draft_queue
        d, picks = draft_state(args.draft_id)
        slot = my_slot(d)
        board = _bb()
        by_id = {r["player_id"]: r for r in board}
        mine = [by_id[p["player_id"]] for p in picks
                if pick_to_slot(p["pick_no"]) == slot and p["player_id"] in by_id]
        counts = {x: 0 for x in MAX_AT_POS}
        for r in mine:
            counts[r["pos"]] += 1
        q = build_queue(board, {p["player_id"] for p in picks}, counts, mine,
                        my_pick_numbers(slot, {p["pick_no"] for p in picks}),
                        live_status(force=True),
                        own_league=(args.draft_id == DRAFT_ID_2026))
        set_draft_queue(args.draft_id, q)
        print(f"queue set ({len(q)}): "
              + ", ".join(by_id[i]["name"] for i in q[:5] if i in by_id))
        raise SystemExit(0)

    run(args.dry_run, args.once, draft_id=args.draft_id, allow_real=args.i_mean_it)
