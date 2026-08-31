"""Roster moves: free-agent adds and FAAB waiver claims. One policy, two channels.

Adding a free agent and claiming a player off waivers are the SAME decision --
"is this man worth more to us than the worst player we hold" -- differing only
in whether Sleeper lets us just take him or makes us bid. Splitting them into
two modules would fork that judgement in two places, which is the thing this
project keeps saying not to do. So there is one evaluator and two submission
channels.

    python -m robo.moves --free      # instant adds off the wire
    python -m robo.moves --claims    # the Tuesday-night FAAB slate

NOTHING IS SUBMITTED TODAY. robo/value.py's gate is shut because the
rest-of-season valuation has not been designed, and --apply is refused while it
is. Everything else -- discovery, the waiver/wire partition, drop selection,
slate construction, the exact GraphQL payloads -- is finished, runs on schedule,
and prints what it would do so it can be read before it is trusted.

THE WAIVER MECHANIC THIS IS BUILT AROUND. A losing claim costs nothing: no FAAB,
no penalty, and FAAB leagues have no rolling priority to burn. Measured on this
league's own 2025 season, 65 claims that named a drop all succeeded and 153 of
181 that named none failed -- because once the roster fills, every later claim
bounces. Those failures are not a defect, they are the tail of deliberate
priority lists. The consequence is that BREADTH IS FREE: several claims naming
the SAME drop form a priority list, the first one that wins consumes the slot,
and the rest fail harmlessly. Submitting one claim instead of a ranked slate
throws that away for nothing.

We are at 17/17, so every claim names a drop -- not to dodge a penalty, but
because a no-drop claim on a full roster is a guaranteed no-op.
"""

import argparse
import json

from robo import LEAGUE_ID_2026, season, settings, value
from robo import sleeper_read as api

# How much better a candidate must be than the man he replaces before it is
# worth a transaction at all. In the valuation's units, so it will need
# retuning the moment the valuation is real.
MIN_GAIN_TO_ADD = 15.0

# Never drop anyone valued above this. A floor, not a judgement: it exists so a
# broken valuation cannot cut a genuine starter, and it is checked in addition
# to the hard rule that nobody in the current starting lineup is droppable.
DROP_FLOOR = 120.0

# How many roster spots we are willing to turn over in one waiver run. This caps
# SLOTS, never claims -- capping claims would throw away the free optionality
# that makes a priority list worth submitting in the first place.
MAX_SLOTS_TO_TURN_OVER = 2

# How deep each slot's priority list goes. Losing costs nothing, so this is
# bounded by how many candidates are plausibly worth the slot, not by risk.
SLATE_DEPTH = 5

# Bid shaping. INERT until the valuation is real, because `gain` is its output.
FAAB_AGGRESSION = 0.35
FAAB_MAX_BID_PCT = 0.5

settings.apply(__name__, globals())


# ------------------------------------------------------------------ evaluation

def _context(league_id: str = LEAGUE_ID_2026) -> dict:
    from robo.rankings import build_board
    board = build_board()
    by_id = {r["player_id"]: r for r in board}
    r = season.mine(league_id)
    reserve = set(r.get("reserve") or [])
    starters = set(r.get("starters") or [])
    week = season.current_week()
    return {
        "board": board, "by_id": by_id, "roster": r, "week": week,
        "reserve": reserve, "starters": starters,
        "players": api.players(),
        "available": season.free_agents(board, league_id),
        "on_waivers": season.on_waivers(league_id),
        "faab": season.faab_left(league_id),
        "slots": season.slots(league_id),
    }


def droppables(ctx: dict) -> list[dict]:
    """Who we could cut, worst first.

    Two independent guards, because they fail differently. A current starter is
    excluded outright -- if the optimizer is starting him this week, cutting him
    is incoherent regardless of what any number says. DROP_FLOOR is the backstop
    for the number itself being wrong.
    """
    out = []
    for pid in ctx["roster"].get("players") or []:
        if pid in ctx["starters"] or pid in ctx["reserve"]:
            continue
        row = ctx["by_id"].get(pid)
        if not row:
            continue
        v, _ = value.value_of(row, ctx["week"])
        if v > DROP_FLOOR:
            continue
        out.append({"row": row, "value": v})
    return sorted(out, key=lambda d: d["value"])


def candidates(ctx: dict, waivers: bool) -> list[dict]:
    """Available players, best first, restricted to one channel.

    `waivers=True` returns only players still sitting on waivers; False returns
    only those free for the taking. Getting this partition right is what stops
    the bot bidding FAAB on somebody it could have had for nothing -- and in
    2025 this league ran 306 free-agent adds against 93 waiver wins, so the free
    side is where most of the volume actually is.
    """
    onw = ctx["on_waivers"]
    out = []
    for row in ctx["available"]:
        if (row["player_id"] in onw) != waivers:
            continue
        v, real = value.value_of(row, ctx["week"])
        out.append({"row": row, "value": v, "real": real})
    return sorted(out, key=lambda d: (-d["value"], d["row"]["player_id"]))


def bid_for(gain: float, ctx: dict) -> int:
    """FAAB to offer. Inert while the valuation is a stand-in, since `gain` is
    the valuation's output -- the shape is here so it can be tuned against real
    numbers rather than invented alongside them."""
    weeks_left = max(1, season.SEASON_WEEKS - ctx["week"] + 1)
    raw = FAAB_AGGRESSION * gain / weeks_left
    cap = FAAB_MAX_BID_PCT * ctx["faab"]
    return max(0, min(int(round(raw)), int(cap)))


# -------------------------------------------------------------------- channels

def plan_free(ctx: dict) -> list[dict]:
    """Instant adds. One proposal per roster slot we are willing to turn over."""
    drops = droppables(ctx)
    pool = candidates(ctx, waivers=False)
    used, out = set(), []
    for d in drops[:MAX_SLOTS_TO_TURN_OVER]:
        for c in pool:
            if c["row"]["player_id"] in used:
                continue
            gain = round(c["value"] - d["value"], 1)
            if gain < MIN_GAIN_TO_ADD:
                break  # pool is sorted, so nothing below this clears either
            used.add(c["row"]["player_id"])
            out.append({"add": c["row"], "drop": d["row"], "gain": gain,
                        "add_value": c["value"], "drop_value": d["value"],
                        "real": c["real"]})
            break
    return out


def plan_claims(ctx: dict) -> list[dict]:
    """The FAAB slate: a ranked priority list per slot, not a single claim.

    Every claim in one slot's list names the SAME drop. Sleeper works them in
    seq order; the first winner takes the slot and the rest bounce off a player
    who is no longer on our roster, at no cost. That is the whole point -- we
    get our best AVAILABLE outcome instead of our best guess.
    """
    drops = droppables(ctx)
    pool = candidates(ctx, waivers=True)
    slates, used = [], set()
    for d in drops[:MAX_SLOTS_TO_TURN_OVER]:
        picks = []
        for c in pool:
            if len(picks) >= SLATE_DEPTH:
                break
            if c["row"]["player_id"] in used:
                continue
            gain = round(c["value"] - d["value"], 1)
            if gain < MIN_GAIN_TO_ADD:
                break
            picks.append({"add": c["row"], "gain": gain, "bid": bid_for(gain, ctx),
                          "add_value": c["value"], "real": c["real"]})
        if not picks:
            continue
        used |= {p["add"]["player_id"] for p in picks}
        slates.append({"drop": d["row"], "drop_value": d["value"], "claims": picks})

    # seq is assigned across ALL slates by descending bid, matching what this
    # league's 2025 transactions actually show. Sleeper works our claims in that
    # order, so the most valuable one gets first refusal on the budget.
    flat = [(s, c) for s in slates for c in s["claims"]]
    flat.sort(key=lambda sc: (-sc[1]["bid"], -sc[1]["gain"],
                              sc[1]["add"]["player_id"]))
    for i, (_, c) in enumerate(flat):
        c["seq"] = i
    return slates


# ------------------------------------------------------------------- payloads

def free_payload(add_id: str, drop_id: str, roster_id: int) -> dict:
    """Exactly what sleeper_write.free_agent_transaction would send."""
    return {"k_adds": [add_id], "v_adds": [roster_id],
            "k_drops": [drop_id], "v_drops": [roster_id]}


def claim_payload(add_id: str, drop_id: str, roster_id: int, bid: int) -> dict:
    """Exactly what sleeper_write.submit_waiver_claim would send.

    The `waiver_bid` key is confirmed -- it was read off 93 completed 2025
    waiver transactions in this league. What has never executed is whether this
    parallel-array form reaches Sleeper's settings blob intact.
    """
    return {"k_adds": [add_id], "v_adds": [roster_id],
            "k_drops": [drop_id], "v_drops": [roster_id],
            "k_settings": ["waiver_bid"], "v_settings": [bid]}


# --------------------------------------------------------------------- output

def _tag(real: bool) -> str:
    return "" if real else "  [PROVISIONAL VALUATION]"


def render_free(ctx: dict, plans: list[dict]) -> str:
    sl, L = ctx["slots"], []
    L.append(f"FREE AGENTS - week {ctx['week']}, "
             f"{len(ctx['available']) - len(ctx['on_waivers'])} available now, "
             f"{len(ctx['on_waivers'])} still on waivers")
    L.append(f"  roster {sl['active']}/{sl['roster_max']} ({sl['open']} open), "
             f"IR {sl['ir_used']}/{sl['ir_slots']}")
    if not plans:
        L.append("  no add clears the bar (gain >= "
                 f"{MIN_GAIN_TO_ADD} over the worst droppable player)")
    for p in plans:
        L.append(f"  ADD  {p['add']['name']:<24} {p['add']['pos']:<4} "
                 f"{p['add_value']:>7.1f}{_tag(p['real'])}")
        L.append(f"  DROP {p['drop']['name']:<24} {p['drop']['pos']:<4} "
                 f"{p['drop_value']:>7.1f}   gain {p['gain']:+.1f}")
    return "\n".join(L)


def render_claims(ctx: dict, slates: list[dict]) -> str:
    L = [f"WAIVER SLATE - week {ctx['week']}, {ctx['faab']} FAAB left, "
         f"{len(ctx['on_waivers'])} player(s) on waivers"]
    if not slates:
        L.append("  no claim clears the bar")
    for s in slates:
        L.append(f"  slot freed by dropping {s['drop']['name']} "
                 f"({s['drop_value']:.1f}) - priority list, first winner takes it:")
        for c in s["claims"]:
            L.append(f"    seq {c['seq']:<3} ${c['bid']:<4} "
                     f"{c['add']['name']:<24} {c['add']['pos']:<4} "
                     f"{c['add_value']:>7.1f}  gain {c['gain']:+.1f}{_tag(c['real'])}")
    total = sum(c["bid"] for s in slates for c in s["claims"])
    if slates:
        L.append(f"  worst case if every list's top claim wins: "
                 f"{len(slates)} move(s); a single slot can only ever spend once, "
                 f"so the ${total} above is not a total commitment")
    return "\n".join(L)


# ------------------------------------------------------------------------ run

def run(channel: str, apply: bool = False, league_id: str = LEAGUE_ID_2026,
        verbose: bool = True) -> dict:
    ctx = _context(league_id)
    if channel == "free":
        plans = plan_free(ctx)
        text = render_free(ctx, plans)
        payloads = [free_payload(p["add"]["player_id"], p["drop"]["player_id"],
                                 ctx["roster"]["roster_id"]) for p in plans]
    else:
        plans = plan_claims(ctx)
        text = render_claims(ctx, plans)
        payloads = [claim_payload(c["add"]["player_id"], s["drop"]["player_id"],
                                  ctx["roster"]["roster_id"], c["bid"])
                    for s in plans for c in s["claims"]]

    if verbose:
        print(text)
    out = {"channel": channel, "week": ctx["week"], "plans": plans,
           "payloads": payloads, "applied": False, "gated": not value.ready()}

    if not value.ready():
        # The gate. Not a flag, not a setting -- a constant in value.py, so
        # turning this bot loose on the roster takes a commit.
        if verbose:
            print("\n  ** " + value.GATE_MESSAGE)
            if apply:
                print("  ** --apply was requested and is REFUSED.")
        return out

    if not apply:
        return out
    raise NotImplementedError(
        "submission path is written but unreachable until VALUATION_READY; "
        "wire it up in the same change that flips the gate, so the first live "
        "move is reviewed alongside the numbers driving it")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--free", action="store_true", help="instant wire adds")
    g.add_argument("--claims", action="store_true", help="the FAAB slate")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-mean-it", action="store_true")
    ap.add_argument("--league", default=LEAGUE_ID_2026)
    ap.add_argument("--payloads", action="store_true",
                    help="print the exact GraphQL variables that would be sent")
    args = ap.parse_args()
    res = run("free" if args.free else "claims", apply=args.apply,
              league_id=args.league)
    if args.payloads:
        print("\nGraphQL variables that would be sent:")
        print(json.dumps(res["payloads"], indent=1))


if __name__ == "__main__":
    main()
