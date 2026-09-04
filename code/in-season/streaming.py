"""Streaming defences: who to start, and who to pick up, weeks before kickoff.

WHY THIS IS SEPARATE FROM ros.py. The rest-of-season spine is Sleeper's weekly
projection feed, and for a defence that feed is unusable twice over. It carries
no matchup at all -- the same team reads the same number in week 5 and week 14 --
and every one of the six pts_allow tiers is among the 22 scoring keys it omits,
so scoring it under our settings returns nearly nothing. A defence's week is
almost entirely a question of who they play, which is exactly the part that
feed does not answer.

The market does answer it. Fitted on 2,106 real RURFFL-scored defence weeks
joined to the closing line:

    opponent implied total     mean DEF points
        10.5 - 17.5                 11.39
        17.5 - 19.5                 10.55
        19.5 - 20.8                  9.35
        20.8 - 22.0                  8.74
        22.0 - 23.2                  7.73
        23.2 - 24.8                  6.39
        24.8 - 26.2                  5.76
        26.2 - 32.2                  4.72

Monotone across every bucket, and a 6.7-point spread end to end -- wider than
the gap between most rostered defences and the wire. That is worth planning
around.

KICKERS ARE NOT STREAMABLE AND THIS MODULE SAYS SO. The same fit on 1,478
kicker weeks against their own team's implied total runs 6.95 to 8.16 with a
correlation of +0.051 and no monotone ordering. There is no signal here to
trade on: a kicker's week is field-goal distance luck, not game environment. So
this module ranks defences and explicitly declines to rank kickers, rather than
shipping a kicker streamer that would churn a roster spot every week to chase
1.2 points of noise. If that ever changes, it changes because this fit changes.

THE HORIZON IS WHATEVER THE BOOKS HAVE POSTED. See robo/vegas.py -- a week with
no line is reported as unpriced and dropped from the plan, never imputed.

    python -m robo.streaming --weeks 4
    python -m robo.streaming --fit
"""

import argparse
import json
import time

from robo import DATA, vegas

FIT_FILE = DATA / "streaming_fit.json"
SCHEMA = 1
POINTS = vegas.PARQUET.parent / "kdef_points_rurffl.parquet"

# Buckets for the fitted curve. Eight equal-count bins over the observed range:
# enough to show the curvature at both ends, few enough that each holds ~260
# defence weeks.
BUCKETS = 8

# Positions this module will rank, and the implied total that drives each. K is
# absent on purpose and the docstring says why -- an empty entry here would read
# as an oversight.
DRIVER = {"DEF": "opp"}

# How much better a streamed defence must project before churning a roster spot
# for it. A defence swap costs a transaction and a week of another position's
# bench depth, and the fit's own bucket-to-bucket step is about 1 point.
MIN_STREAM_GAIN = 2.0

from robo import settings as _settings  # noqa: E402
_settings.apply(__name__, globals())


def fit(write: bool = True) -> dict:
    """Expected points by implied-total bucket, per position.

    Fitted on binned means rather than a straight line because the relationship
    flattens at both tails -- there is a floor to what a defence gives up to a
    bad offence and a ceiling on how badly a good one beats them -- and a linear
    fit would extrapolate through both.
    """
    import polars as pl
    try:
        df = pl.read_parquet(POINTS)
    except Exception as e:
        return {"error": f"cannot read {POINTS.name}: {type(e).__name__}", "curve": {}}

    rows = []
    for (yr, wk), g in df.group_by(["season", "week"]):
        imp = vegas.implied_totals(yr, wk)
        for r in g.iter_rows(named=True):
            d = imp.get(vegas.team_code(r["team"]))
            if d:
                rows.append((r["pos"], d["own"], d["opp"], float(r["pts"])))

    curve, stats = {}, {}
    for pos, drv in DRIVER.items():
        sub = sorted(((o if drv == "own" else p), v) for (ps, o, p, v) in rows if ps == pos)
        if len(sub) < BUCKETS * 20:
            continue
        n, pts = len(sub), []
        for i in range(BUCKETS):
            chunk = sub[i * n // BUCKETS:(i + 1) * n // BUCKETS]
            if not chunk:
                continue
            xs = [c[0] for c in chunk]
            pts.append({"lo": xs[0], "hi": xs[-1],
                        "mid": round(sum(xs) / len(xs), 3),
                        "n": len(chunk),
                        "pts": round(sum(c[1] for c in chunk) / len(chunk), 3)})
        curve[pos] = pts
        stats[pos] = {"n": n, "driver": drv,
                      "spread": round(pts[0]["pts"] - pts[-1]["pts"], 2)}

    out = {"schema": SCHEMA, "fitted": time.time(), "curve": curve, "stats": stats,
           "seasons": sorted({int(s) for s in df["season"].unique().to_list()})}
    if write:
        FIT_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def load_fit() -> dict:
    if FIT_FILE.exists():
        try:
            d = json.loads(FIT_FILE.read_text(encoding="utf-8"))
            if d.get("schema") == SCHEMA and d.get("curve"):
                return d
        except Exception:
            pass
    return fit()


def expected(pos: str, implied: float) -> float | None:
    """Expected points for this position against that implied total.

    Linear between bucket midpoints, flat outside them: the tails are where the
    sample thins, and extrapolating a slope off two hundred games is how a
    defence facing a 34-point favourite gets projected below zero.
    """
    pts = (load_fit().get("curve") or {}).get(pos)
    if not pts:
        return None
    if implied <= pts[0]["mid"]:
        return pts[0]["pts"]
    if implied >= pts[-1]["mid"]:
        return pts[-1]["pts"]
    for a, b in zip(pts, pts[1:]):
        if a["mid"] <= implied <= b["mid"]:
            f = (implied - a["mid"]) / (b["mid"] - a["mid"] or 1)
            return round(a["pts"] + f * (b["pts"] - a["pts"]), 2)
    return pts[-1]["pts"]


def rank_week(week: int, season_yr=None, pos: str = "DEF") -> list[dict]:
    """Every team's defence for one week, best matchup first.

    Empty when the books have not posted that week -- an unpriced week has no
    ranking, only the absence of one.
    """
    from robo import season as _season
    yr = int(season_yr or _season.SEASON)
    drv = DRIVER.get(pos)
    if not drv:
        return []
    out = []
    for team, d in vegas.implied_totals(yr, week).items():
        e = expected(pos, d[drv])
        if e is None:
            continue
        out.append({"team": team, "week": week, "opponent": d["opponent"],
                    "home": d["home"], "implied": d[drv], "pts": e})
    out.sort(key=lambda r: (-r["pts"], r["team"]))
    return out


def plan(from_week: int | None = None, weeks: int = 4, season_yr=None,
         pos: str = "DEF") -> dict:
    """A week-by-week streaming board out to the end of the priced horizon."""
    from robo import season as _season
    yr = int(season_yr or _season.SEASON)
    start = from_week or _season.current_week()
    end = min(start + weeks - 1, vegas.horizon(yr, start))
    board = {w: rank_week(w, yr, pos) for w in range(start, end + 1)}
    return {"season": yr, "pos": pos, "from": start, "to": end,
            "requested": weeks, "priced_through": vegas.horizon(yr, start),
            "board": board}


def team_outlook(team: str, from_week: int | None = None, weeks: int = 6,
                 season_yr=None, pos: str = "DEF") -> list[dict]:
    """One team's next few matchups -- the "is my defence worth holding" view."""
    p = plan(from_week, weeks, season_yr, pos)
    tm = vegas.team_code(team)
    out = []
    for w, rows in sorted(p["board"].items()):
        hit = next((r for r in rows if r["team"] == tm), None)
        out.append({"week": w, **(hit or {"team": tm, "pts": None})})
    return out


def report(from_week: int | None = None, weeks: int = 4, top: int = 8) -> str:
    from robo import season as _season
    f = load_fit()
    if f.get("error"):
        return f"cannot fit: {f['error']}"
    p = plan(from_week, weeks)
    st = (f.get("stats") or {}).get("DEF") or {}
    L = [f"DEFENCE STREAMING - weeks {p['from']}-{p['to']} "
         f"(asked for {p['requested']}, books priced through {p['priced_through']})",
         f"  curve fitted on {st.get('n', 0)} defence weeks, "
         f"{f.get('seasons')}, spread {st.get('spread', 0)} pts", ""]
    if p["to"] < p["from"]:
        L.append("  no week ahead is priced yet -- nothing to plan")
        return "\n".join(L)
    for w, rows in sorted(p["board"].items()):
        if not rows:
            L.append(f"  week {w}: unpriced")
            continue
        L.append(f"  week {w}")
        for r in rows[:top]:
            L.append(f"    {r['pts']:>6.2f}  {r['team']:<4} "
                     f"{'vs' if r['home'] else '@ '} {r['opponent']:<4} "
                     f"(opp implied {r['implied']:.1f})")
        L.append("")
    L.append("  KICKERS ARE NOT RANKED. The same fit on 1,478 kicker weeks runs")
    L.append("  6.95 to 8.16 with correlation +0.051 and no monotone ordering --")
    L.append("  a kicker's week is field-goal luck, not game environment.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--weeks", type=int, default=4)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--team", default=None)
    args = ap.parse_args()

    if args.fit:
        f = fit()
        if f.get("error"):
            print(f["error"])
            return
        for pos, s in (f.get("stats") or {}).items():
            print(f"  {pos}: {s['n']} weeks, driver {s['driver']}_implied, "
                  f"spread {s['spread']} pts")
        print(f"  -> {FIT_FILE.name}")
        return
    if args.team:
        for r in team_outlook(args.team, args.week, args.weeks):
            if r.get("pts") is None:
                print(f"  week {r['week']}: unpriced")
            else:
                print(f"  week {r['week']}: {r['pts']:>6.2f}  "
                      f"{'vs' if r['home'] else '@ '} {r['opponent']} "
                      f"(opp implied {r['implied']:.1f})")
        return
    print(report(args.week, args.weeks, args.top))


if __name__ == "__main__":
    main()
