"""Earliest-vs-latest draft slot comparison (constitution §4.3.4.1).

For each candidate slot, walk the 17-round snake: other teams pick by market
ADP (FFC locked, falling back to Sleeper 2QB), we pick best VORP from the
board honoring roster targets. Our keeper rounds are dead (no pick). Output:
total expected VORP per slot.

Usage: python -m robo.draft_sim [--keeper-rounds 2 3] [--slots 7 12]
"""

import argparse

from robo.rankings import build_board

TEAMS = 12
ROUNDS = 17
# roster construction targets for a 17-round 2QB build (1 K + 1 DEF in last 2)
MAX_AT_POS = {"QB": 3, "RB": 6, "WR": 6, "TE": 2, "K": 1, "DEF": 1}


def snake_pick_slots(slot: int) -> list[int]:
    """Overall pick numbers for a slot (1-based) across all rounds."""
    picks = []
    for rd in range(1, ROUNDS + 1):
        pos = slot if rd % 2 == 1 else TEAMS + 1 - slot
        picks.append((rd - 1) * TEAMS + pos)
    return picks


def market_order(board: list[dict]) -> list[dict]:
    def key(r):
        for k in ("adp_live", "adp_ffc", "adp_sleeper_2qb"):
            if r.get(k) is not None:
                return r[k]
        return 999 + r["value_rank"]
    return sorted(board, key=key)


def simulate(slot: int, keeper_rounds: set[int], board: list[dict],
             keeper_picks: set[int] | None = None) -> tuple[float, list]:
    """Walk the snake once and total the VORP we end up with.

    `keeper_picks` is every pick number the league has already spent on a
    keeper. Those picks consume nothing from the pool: the player is off the
    board but the pick does not draft anyone new. Without it the sim burned all
    204 picks against a pool already 24 players short, double-counting every
    keeper and making our late rounds look barrener than they will be.
    """
    keeper_picks = keeper_picks or set()
    market = market_order(board)
    taken = set()
    my_counts = {p: 0 for p in MAX_AT_POS}
    my_picks = []
    total = 0.0
    pick_slots = snake_pick_slots(slot)
    my_pick_set = {pk: rd + 1 for rd, pk in enumerate(pick_slots)
                   if (rd + 1) not in keeper_rounds and pk not in keeper_picks}

    for overall in range(1, TEAMS * ROUNDS + 1):
        if overall in keeper_picks:
            continue
        if overall in my_pick_set:
            rd = my_pick_set[overall]
            rounds_left = sum(1 for pk, r in my_pick_set.items() if pk >= overall)
            candidates = [
                r for r in board
                if r["player_id"] not in taken
                and my_counts[r["pos"]] < MAX_AT_POS[r["pos"]]
                # K/DEF only when nothing but the final K/DEF slots remain
                and not (r["pos"] in ("K", "DEF") and rounds_left > (2 - my_counts["K"] - my_counts["DEF"]))
            ]
            best = min(candidates, key=lambda r: r["blend_rank"])
            taken.add(best["player_id"])
            my_counts[best["pos"]] += 1
            my_picks.append((rd, overall, best["name"], best["pos"], best["vorp"]))
            total += best["vorp"]
        else:
            for r in market:
                if r["player_id"] not in taken:
                    taken.add(r["player_id"])
                    break
    return round(total, 1), my_picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keeper-rounds", type=int, nargs="*", default=[2, 3])
    ap.add_argument("--slots", type=int, nargs="*", default=[7, 12])
    ap.add_argument("--ignore-keepers", action="store_true",
                    help="don't remove league keepers from the pool")
    args = ap.parse_args()

    board = build_board()
    keeper_picks: set[int] = set()
    if not args.ignore_keepers:
        from robo.league_keepers import board_keepers, landscape
        land = landscape()
        gone = set(land["off_board"])
        keeper_picks = {r["pick_no"] for r in board_keepers()}
        before = len(board)
        board = [r for r in board if r["player_id"] not in gone]
        print(f"removed {before - len(board)} kept players from the pool "
              f"({len(land['board'])} teams on the board, "
              f"{len(land['declared'])} declared, {len(land['predicted'])} predicted); "
              f"{len(keeper_picks)} pick slots already spent")
        if land["freed"]:
            print("back in the pool: "
                  + ", ".join(land["names"][p] for p in land["freed"]))
    results = {}
    for slot in args.slots:
        total, picks = simulate(slot, set(args.keeper_rounds), board, keeper_picks)
        results[slot] = total
        print(f"\n=== slot {slot} (keeper rounds {sorted(args.keeper_rounds)} skipped) -> total VORP {total} ===")
        for rd, overall, name, pos, vorp in picks:
            print(f"  R{rd:>2} (pick {overall:>3}): {name:<24} {pos:<3} vorp={vorp}")
    best = max(results, key=results.get)
    print(f"\nbest slot: {best}  ({results})")


if __name__ == "__main__":
    main()
