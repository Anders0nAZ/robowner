"""What the market is doing RIGHT NOW, because ADP and depth charts lag.

Sleeper's depth_chart_order is a roster formality. It says MarShawn Lloyd is the
number two back, and it said that before and after the camp reports that had
264,987 people add him in a single day. ADP is worse: measured 27 Aug 2026, the
median player moved 0.6 picks between our locked FFC snapshot and the live feed,
and exactly one player out of 229 moved more than fifteen. Beat reporters and
position coaches know things in August that the consensus prices in September.

Trending adds are the fastest public proxy for that. They are not analysis --
they are hundreds of thousands of people reacting to local radio, camp tweets,
and practice reports, aggregated hourly and free. Pre-draft that is almost
entirely role news, which is exactly the signal we want; in season it would be
contaminated by streaming and bye-week churn, so this module is for August.

Used two ways:

  * `signal()` lifts P(opportunity) in bench.py for a backup the market is
    hammering, so the bot stops taking a stale depth chart at face value.
  * `python -m robo.buzz` prints where the crowd and our board DISAGREE, which
    is the part a human should read before the draft.

Never fatal: any failure returns a neutral signal, because this runs on the
clock and a dead endpoint must not cost us a pick.
"""

import json
import math
import time

from robo import DATA
from robo import sleeper_read as api

CACHE = DATA / "buzz.json"
STALE_AFTER = 6 * 3600  # refetch if the cache is older than this
WINDOW_HOURS = 72       # long enough to smooth a slow news day, short enough to be news

# data/settings.json overrides the constants above. Import-time, so a change
# there takes effect on the next run of this module -- see robo/settings.py.
from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())


def fetch(hours: int = WINDOW_HOURS) -> dict:
    """{player_id: net adds} -- adds minus drops, floored at zero.

    Net, because a player with huge adds AND huge drops is churn (a bye-week
    stream, a panic add reversed), not a role change.
    """
    adds = {t["player_id"]: t["count"] for t in api.trending("add", hours, 100)}
    drops = {t["player_id"]: t["count"] for t in api.trending("drop", hours, 100)}
    return {pid: max(0, n - drops.get(pid, 0)) for pid, n in adds.items()}


def load(refresh: bool = False) -> dict:
    """Cached net-adds map. Falls back to a stale cache, then to nothing."""
    cached = None
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    fresh_enough = cached and (time.time() - cached.get("ts", 0)) < STALE_AFTER
    if cached and fresh_enough and not refresh:
        return cached["net"]
    try:
        net = fetch()
        CACHE.write_text(json.dumps({"ts": time.time(), "hours": WINDOW_HOURS,
                                     "net": net}), encoding="utf-8")
        return net
    except Exception:
        # a stale number beats no number, and no number beats a crash mid-draft
        return cached["net"] if cached else {}


_SCALED: dict | None = None


def signal(player_id: str) -> float:
    """0..1 buzz for one player. Log-scaled: the top add is 5x the tenth, not 5%.

    Rank would throw away that the leader is an order of magnitude clear of the
    field, and raw counts would make everyone below the leader indistinguishable
    from zero.
    """
    global _SCALED
    if _SCALED is None:
        net = load()
        top = max(net.values(), default=0)
        _SCALED = ({pid: math.log1p(n) / math.log1p(top) for pid, n in net.items()}
                   if top > 0 else {})
    return _SCALED.get(player_id, 0.0)


def reset() -> None:
    """Drop the memoised scaling (tests, or after a deliberate refresh)."""
    global _SCALED
    _SCALED = None


def divergence(board: list[dict], players: dict, limit: int = 25) -> list[dict]:
    """Players the crowd is chasing that our board is not. Sorted by buzz.

    "Our board is not" is the point: a hot player who is already a first-round
    pick tells us nothing. A hot player our board ranks 160th, or does not rank
    at all, is either the market being stupid or our projections being three
    weeks behind a position battle. Those are worth a human's attention.
    """
    by_id = {r["player_id"]: r for r in board}
    net = load()
    out = []
    for pid, n in sorted(net.items(), key=lambda kv: -kv[1]):
        if n <= 0:
            continue
        v = players.get(pid) or {}
        r = by_id.get(pid)
        out.append({
            "player_id": pid,
            "name": v.get("full_name") or pid,
            "pos": (r or v).get("pos") or v.get("position") or "?",
            "team": v.get("team"),
            "adds": n,
            "buzz": signal(pid),
            "depth": v.get("depth_chart_order"),
            "adp_live": (r or {}).get("adp_live"),
            "adp_locked": (r or {}).get("adp_ffc"),
            "vorp": (r or {}).get("vorp"),
            "on_board": r is not None,
        })
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    from robo.rankings import build_board

    board = build_board()
    players = api.players()
    rows = divergence(board, players)
    print(f"Sleeper net adds over the last {WINDOW_HOURS}h, against our board.")
    print("Our board is built on projections + locked ADP; both lag camp news.\n")
    print(f"{'net adds':>9}  {'buzz':>5}  {'player':<22} {'pos':<4} {'dep':>3}  "
          f"{'live adp':>8}  {'vorp':>7}")
    for d in rows:
        adp = f"{d['adp_live']:.0f}" if d["adp_live"] else ("—" if d["on_board"] else "n/a")
        vorp = f"{d['vorp']:.0f}" if d["vorp"] is not None else "—"
        flag = ""
        if not d["on_board"]:
            flag = "  << not on our board at all"
        elif d["adp_live"] is None:
            flag = "  << no live ADP: market has not priced him"
        elif d["adp_live"] > 120:
            flag = "  << late/undrafted by our board"
        print(f"{d['adds']:>9,}  {d['buzz']:>5.2f}  {d['name']:<22} {d['pos']:<4} "
              f"{str(d['depth'] or '—'):>3}  {adp:>8}  {vorp:>7}{flag}")
