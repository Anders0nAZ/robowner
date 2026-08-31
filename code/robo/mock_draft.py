"""Mock the 2026 draft with the REAL pick policy, over the real keeper board.

This exists because draft_sim.py is not a preview and was never meant to be one.
draft_sim answers "which draft slot is worth more" and deliberately picks with a
dumb rule -- best blend_rank, ignoring roster construction -- which in a 2QB
league drafts zero quarterbacks. That is a property of the question it asks, not
of the bot. It ran before the slot was known and its job is now finished.

What will actually be on the clock is draft_agent.choose_pick(): a DP across all
our remaining picks that assigns starter needs to future pick numbers. Until
now, nothing in the repo ran it except the live draft, so the only mock you
could execute was the one that doesn't drive anything. Now:

  python -m robo.mock_draft              # one board, every pick explained
  python -m robo.mock_draft --runs 25    # 25 noisy boards, distribution

With --runs the board is jittered by each player's own published ADP stdev, so
you see how the policy holds up when the draft does not come to us the way the
median board says it will -- one deterministic run tells you nothing about
robustness.

Two opponent models, because it changes the answer:

  --opponents adp    every other pick is the next name off one shared jittered
                     ADP list. No team identity, no roster needs. Simple, and
                     wrong here in a specific way.
  --opponents needs  each of the 11 other teams starts from ITS OWN keepers and
                     drafts to fill its own starters (2QB/2RB/2WR/1TE+flex, K
                     and DEF last) taking the best ADP that fits. (default)

The distinction looked like it would matter, because keepers are not spread
evenly across positions: 10 of the 24 are quarterbacks. Two teams have both
their starting QBs already; four (us included) kept none and need two apiece.
Redraft ADP prices a world where all 12 teams need two QBs, which is not this
draft, so a straight countdown is wrong in both directions at once.

It turns out to wash. Measured over 50 boards, QBs off the board by our round-4
pick: 9 either way. Our roster shape is unchanged (3 QB / 5 RB / 5.5 WR / 2 TE
in both), and the needs model costs us about 15 projected starter points, under
1%. Worth knowing the plan does not rest on the opponent model; keep both so
that stays checkable when the keeper mix changes next season.

Neither model is a claim that rivals draft well. Note the needs model still ends
every team at exactly 3 QBs: once a team's two starters are set it falls through
to best-available, and in 2QB ADP that is usually another quarterback.

Keeper picks consume nobody: those 24 pick numbers are already spent, and the
players are off the board from the start.
"""

import argparse
import pathlib
import random
import statistics
from collections import Counter

from robo import DRAFT_ID_2026, ROSTER_ID
from robo import draft_agent as da
from robo.league_keepers import board_keepers
from robo.rankings import build_board

TEAMS, ROUNDS = da.TEAMS, da.ROUNDS
FALLBACK_SLOT = 6  # our 2026 slot; only used if Sleeper is unreachable

# What we have to field every week: 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 SUPERFLEX.
LINEUP = [("QB", ("QB",)), ("RB", ("RB",)), ("RB", ("RB",)),
          ("WR", ("WR",)), ("WR", ("WR",)), ("TE", ("TE",)),
          ("FLEX", ("RB", "WR", "TE")), ("SFLEX", ("QB", "RB", "WR", "TE"))]

# What we assume a rival is trying to field before it starts taking bench darts.
OPP_TARGETS = {"QB": 2, "RB": 2, "WR": 2, "TE": 1}
OPP_FLEX = ("RB", "WR", "TE")


def slot_to_roster(keepers: list[dict]) -> dict[int, int]:
    """slot -> roster_id, read off the keeper picks themselves.

    Every team has keeper picks on the board, so this covers all 12 slots
    without another Sleeper call and keeps the mock runnable offline.
    """
    return {da.pick_to_slot(k["pick_no"]): k["roster_id"] for k in keepers}


def opponent_pick(order: list[dict], taken: set[str], counts: dict[str, int],
                  picks_left: int) -> dict | None:
    """What a needs-aware rival takes: best ADP that fits what it still needs.

    Deliberately the same shape as our own policy's guardrails (starter targets
    first, then flex, then bench; K and DEF only when the remaining picks are
    exactly the K/DEF slots) but WITHOUT the lookahead DP. Rivals are assumed
    competent, not clairvoyant -- giving them our planner would flatter us by
    making the board behave exactly as our model expects.
    """
    kd_left = (1 - counts["K"]) + (1 - counts["DEF"])
    kd_only = picks_left <= kd_left
    if kd_only:
        want = {p for p in ("K", "DEF") if counts[p] < 1}
    else:
        want = {p for p in OPP_TARGETS if counts[p] < OPP_TARGETS[p]}
        if not want:
            flex_filled = sum(max(0, counts[p] - OPP_TARGETS[p]) for p in OPP_FLEX)
            want = set(OPP_FLEX) if flex_filled < 1 else set(OPP_TARGETS)
    for r in order:
        if r["player_id"] in taken:
            continue
        pos = r["pos"]
        if pos in ("K", "DEF") and not kd_only:
            continue
        if counts.get(pos, 0) >= da.MAX_AT_POS.get(pos, 0):
            continue
        if pos in want:
            return r
    # nothing that fits the plan -- fall back to best available
    for r in order:
        if r["player_id"] in taken or r["pos"] in ("K", "DEF"):
            continue
        if counts.get(r["pos"], 0) < da.MAX_AT_POS.get(r["pos"], 0):
            return r
    return None


def our_slot() -> int:
    try:
        d, _ = da.draft_state()
        return da.my_slot(d) or FALLBACK_SLOT
    except Exception:
        return FALLBACK_SLOT


def starters(roster: list[dict]) -> tuple[float, list]:
    """Best legal starting lineup and its projected points.

    Greedy in LINEUP order, which is safe here because the flex slots come last
    and take whatever the dedicated slots left behind.
    """
    pool = sorted(roster, key=lambda r: -r["proj_pts"])
    used, out, total = set(), [], 0.0
    for label, allowed in LINEUP:
        for r in pool:
            if r["player_id"] in used or r["pos"] not in allowed:
                continue
            used.add(r["player_id"])
            out.append((label, r))
            total += r["proj_pts"]
            break
    return round(total, 1), out


def resolve(board: list[dict], names: str) -> list[dict]:
    """Board rows for a comma-separated list of names. Raises on a miss.

    Loud on a typo on purpose: silently drafting the plan you meant to rule out
    is the one outcome that makes a contingency run worthless.
    """
    from robo.keeper import norm
    by_name = {norm(r["name"]): r for r in board}
    out = []
    for raw in (n.strip() for n in names.split(",") if n.strip()):
        row = by_name.get(norm(raw))
        if not row:
            raise SystemExit(f"no board player matches {raw!r}")
        out.append(row)
    return out


def run_once(board: list[dict], keepers: list[dict], slot: int,
             rng: random.Random | None = None, opponents: str = "needs",
             gone: set[str] | None = None) -> dict:
    by_id = {r["player_id"]: r for r in board}
    kp_no = {r["pick_no"] for r in keepers}
    taken = {r["player_id"] for r in keepers} | (gone or set())
    counts = {p: 0 for p in da.MAX_AT_POS}
    roster, log = [], []

    # every rival starts the draft holding its own keepers, so a team that kept
    # two QBs is not shopping for a starter at one. It may still take a third
    # later as bench depth -- `opp` below is only maintained under the needs
    # model, and is returned so that stays auditable rather than assumed.
    s2r = slot_to_roster(keepers)
    opp: dict[int, dict[str, int]] = {r: {p: 0 for p in da.MAX_AT_POS}
                                      for r in set(s2r.values())}
    opp_left: dict[int, int] = {r: ROUNDS for r in opp}
    for k in keepers:
        if k["pos"] in opp[k["roster_id"]]:
            opp[k["roster_id"]][k["pos"]] += 1
        opp_left[k["roster_id"]] -= 1
    qb_gone: list[int] = []  # overall pick numbers where a QB came off the board

    for k in sorted(keepers, key=lambda r: r["pick_no"]):
        if k["roster_id"] != ROSTER_ID:
            continue
        row = by_id.get(k["player_id"])
        if row:
            roster.append(row)
        if k["pos"] in counts:
            counts[k["pos"]] += 1
        log.append((k["pick_no"], k["name"], k["pos"], None, "keeper (pick forfeited)"))

    ours = set(da.my_pick_numbers(slot, kp_no))

    # One board realization for the rest of the league. Drawing the noise once
    # per run (not per comparison) is the point: a run is a possible draft, and
    # inside it the other owners have to behave consistently.
    def key(r):
        m = da._market(r)
        return m + rng.gauss(0, da._sigma(r)) if rng else m
    order = sorted(board, key=key)
    cursor = 0

    for overall in range(1, TEAMS * ROUNDS + 1):
        if overall in kp_no:
            continue
        if overall in ours:
            future = sorted(p for p in ours if p >= overall)
            pick, why = da.choose_pick(board, taken, counts, len(future),
                                       overall=overall,
                                       next_overall=future[1] if len(future) > 1 else 0,
                                       future_picks=future, roster=roster)
            if pick is None:
                log.append((overall, "(none)", "-", None, why))
                continue
            taken.add(pick["player_id"])
            counts[pick["pos"]] += 1
            roster.append(pick)
            if pick["pos"] == "QB":
                qb_gone.append(overall)
            log.append((overall, pick["name"], pick["pos"], pick["vorp"], why))
        elif opponents == "needs":
            rid = s2r[da.pick_to_slot(overall)]
            got = opponent_pick(order, taken, opp[rid], opp_left[rid])
            opp_left[rid] -= 1
            if got:
                taken.add(got["player_id"])
                opp[rid][got["pos"]] += 1
                if got["pos"] == "QB":
                    qb_gone.append(overall)
        else:
            while cursor < len(order) and order[cursor]["player_id"] in taken:
                cursor += 1
            if cursor < len(order):
                if order[cursor]["pos"] == "QB":
                    qb_gone.append(overall)
                taken.add(order[cursor]["player_id"])
                cursor += 1

    pts, lineup = starters(roster)
    return {"log": log, "roster": roster, "counts": dict(counts),
            "starter_pts": pts, "lineup": lineup, "qb_gone": qb_gone,
            "opp": opp,
            "vorp": round(sum(r["vorp"] for r in roster), 1)}


def _detail(res: dict, slot: int) -> None:
    print(f"slot {slot} — {len(res['log'])} slots, "
          f"{sum(1 for r in res['log'] if r[4] != 'keeper (pick forfeited)')} picks made\n")
    # keepers are logged first but sit at rounds 2 and 3; read it as a draft
    for overall, name, pos, vorp, why in sorted(res["log"]):
        rd = (overall - 1) // TEAMS + 1
        v = f"{vorp:>6.1f}" if vorp is not None else "     -"
        print(f"  R{rd:>2} #{overall:>3}  {name:<22} {pos:<4} vorp={v}   {why}")
    print(f"\nroster: {res['counts']}   total VORP {res['vorp']}")
    print(f"\nstarting lineup — {res['starter_pts']} projected points")
    for label, r in res["lineup"]:
        print(f"  {label:<6} {r['name']:<22} {r['pos']:<4} {r['proj_pts']:>6.1f}")


def _summary(runs: list[dict]) -> None:
    pts = [r["starter_pts"] for r in runs]
    print(f"\n{len(runs)} runs — starting-lineup projected points")
    print(f"  median {statistics.median(pts):.0f}   "
          f"mean {statistics.mean(pts):.0f}   "
          f"min {min(pts):.0f}   max {max(pts):.0f}")
    print("\npositions drafted (median across runs)")
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        vals = sorted(r["counts"][pos] for r in runs)
        print(f"  {pos:<4} median {statistics.median(vals):>4.1f}  "
              f"range {vals[0]}-{vals[-1]}")
    # The assumption the whole plan leans on: that QBs are still there when we
    # get to picks 43 and 54. This is where the opponent model earns its keep.
    print("\nQBs off the board by our pick (median across runs)")
    for pk in (6, 43, 54, 67, 91):
        vals = sorted(sum(1 for g in r["qb_gone"] if g < pk) for r in runs)
        print(f"  by #{pk:<4} median {statistics.median(vals):>4.1f}  "
              f"range {vals[0]}-{vals[-1]}")

    freq = Counter()
    for r in runs:
        for p in r["roster"]:
            freq[(p["name"], p["pos"])] += 1
    print(f"\nmost often on our roster")
    for (name, pos), n in freq.most_common(18):
        print(f"  {100 * n / len(runs):>5.0f}%  {name:<22} {pos}")


def save_report(runs: list[dict], slot: int, path, header: str = "") -> None:
    """Write a mock as something a human reads, not as JSON.

    The earlier habit of dumping raw JSON meant the only way to see what the bot
    drafted was to parse it, which defeats the point of running a mock at all.
    """
    from collections import Counter
    lines = ["ROBONER MOCK DRAFT", "=" * 66]
    if header:
        lines += [header]
    lines += [f"slot {slot}   {len(runs)} board(s)", ""]

    for n, res in enumerate(runs, 1):
        if len(runs) > 1:
            lines += [f"--- board {n} ---"]
        lines += [f"{'RD':>3} {'PICK':>5}  {'PLAYER':<24} {'POS':<4} {'VORP':>7}  WHY"]
        for overall, name, pos, vorp, why in sorted(res["log"]):
            rd = (overall - 1) // TEAMS + 1
            v = f"{vorp:7.1f}" if vorp is not None else "      -"
            # the reasoning strings carry planner internals; keep the human half
            short = why.split(";")[0] if why else ""
            lines += [f"{rd:>3} {overall:>5}  {name:<24} {pos:<4} {v}  {short}"]
        c = Counter(r["pos"] for r in res["roster"])
        lines += ["",
                  "  roster    " + "  ".join(f"{k} {c[k]}" for k in
                                             ("QB", "RB", "WR", "TE", "K", "DEF") if c[k]),
                  f"  starters  {res['starter_pts']:.0f} projected points", ""]
        for label, r in res["lineup"]:
            lines += [f"    {label:<6} {r['name']:<24} {r['pos']:<4} {r['proj_pts']:>6.1f}"]
        lines += [""]

    if len(runs) > 1:
        import statistics
        pts = [r["starter_pts"] for r in runs]
        lines += ["=" * 66,
                  f"ACROSS {len(runs)} BOARDS",
                  f"  starting points   median {statistics.median(pts):.0f}   "
                  f"range {min(pts):.0f}-{max(pts):.0f}"]
        for pos in ("QB", "RB", "WR", "TE"):
            vals = sorted(r["counts"][pos] for r in runs)
            lines += [f"  {pos:<17} median {statistics.median(vals):.1f}   "
                      f"range {vals[0]}-{vals[-1]}"]
        freq = Counter()
        for r in runs:
            for x in r["roster"]:
                freq[(x["name"], x["pos"])] += 1
        lines += ["", "  most often ours"]
        for (nm, ps), k in freq.most_common(12):
            lines += [f"    {100 * k / len(runs):>4.0f}%  {nm:<24} {ps}"]

    pathlib.Path(path).write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1,
                    help="noisy boards to simulate (1 = deterministic, full detail)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--slot", type=int, default=None)
    ap.add_argument("--opponents", choices=("needs", "adp"), default="needs",
                    help="how the other 11 teams pick (default: needs-aware)")
    ap.add_argument("--detail", action="store_true",
                    help="with --runs, print every pick of every run, not just the summary")
    ap.add_argument("--save", default="",
                    help="write a readable report to this path")
    ap.add_argument("--gone", default="",
                    help="comma-separated players to treat as already drafted -- "
                         "contingency runs, e.g. --gone 'Jordan Love'")
    args = ap.parse_args()

    board = build_board()
    keepers = board_keepers()
    slot = args.slot or our_slot()
    if not keepers:
        print("!! no keeper board found — running as if nobody keeps anyone")
    print(f"opponent model: {args.opponents}")
    gone = set()
    if args.gone:
        rows = resolve(board, args.gone)
        gone = {r["player_id"] for r in rows}
        print("off the board before we pick: "
              + ", ".join(f"{r['name']} ({r['pos']})" for r in rows))

    if args.runs == 1:
        res = run_once(board, keepers, slot, opponents=args.opponents, gone=gone)
        _detail(res, slot)
        if args.save:
            save_report([res], slot, args.save,
                        header=f"opponents={args.opponents}"
                               + (f"; gone: {args.gone}" if args.gone else ""))
        return
    rng = random.Random(args.seed)
    runs = []
    for i in range(args.runs):
        runs.append(run_once(board, keepers, slot, rng, args.opponents, gone))
        if args.detail:
            print(f"\n{'=' * 78}\nRUN {i + 1} of {args.runs}\n{'=' * 78}")
            _detail(runs[-1], slot)
        else:
            print(f"  run {i + 1}/{args.runs}  "
                  f"starters {runs[-1]['starter_pts']:.0f}", flush=True)
    _summary(runs)
    if args.save:
        save_report(runs, slot, args.save,
                    header=f"opponents={args.opponents}"
                           + (f"; gone: {args.gone}" if args.gone else ""))


if __name__ == "__main__":
    main()
