"""The weekly scorecard: what the bot believed before each game, against what happened.

    python -m robo.scorecard --week 2            # score one completed week, append to the ledger
    python -m robo.scorecard --week 2 --detail   # also print the per-player rows (never stored)
    python -m robo.scorecard --backfill          # every completed week
    python -m robo.scorecard                     # print the newest ledger rows

WHY. Nothing in the bot checked a forecast against an outcome. The weeks 1-2
measurement found real things by hand -- the model's current-week number was
further than Sleeper's kickoff projection on flagged players, scout's "avoid"
calls underperformed by about four points while its "boost" calls carried no
signal, and most of a forecast's miss is one-week noise rather than a
persistent error. This makes that measurement weekly and recorded, so any
later change to how the bot weighs those sources has evidence behind it.

IT ONLY MEASURES. It writes nothing but its own ledger (data/scorecard.jsonl,
aggregates only -- no verdict reasons, no per-player rows) and changes no
setting. Turning these numbers into proposed settings is a separate, later step.

EVERY FORECAST IS THE ONE THAT EXISTED BEFORE THAT PLAYER'S GAME:
  * Sleeper   -- the last projection capture before HIS kickoff;
  * model     -- the newest kept model vintage before his kickoff
                 (robo.model_proj.vintage_before). Weeks exported before
                 vintages were kept fall back to the final weekly file and are
                 labelled `reconstructed`;
  * bot       -- the bot's own weekly `final` (availability ramp applied) from
                 the newest OBSERVED value_history vintage before his kickoff;
                 before 15 Sep only reconstructions exist, labelled so.
Status at kickoff comes from the pulse's flag timeline (sleeper_lag.levels()),
never from a capture row, which does not carry live injury status.

THE COHORT is every skill player Sleeper projected at MIN_COHORT or more in ANY
capture before his own kickoff -- so the late injury replacement who climbed
mid-week is in it, as is the man who fell out of it.

SCOUT VERDICTS carry their provenance. Rows backfilled from news-pulse records
are `first_observed`: the time is when the verdict was first SEEN, which proves
it existed by then, not that it was in force from then. Live-logged rows are
`logged`. The two are scored separately.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import time
from pathlib import Path

from robo import DATA, LEAGUE_ID_2026, model_proj, season, sleeper_lag, value_history
from robo import sleeper_read as api
from robo.rankings import custom_points

LEDGER = DATA / "scorecard.jsonl"
MODEL_WEEKLY = DATA / "nflmodel" / "out"
SKILL = ("QB", "RB", "WR", "TE")
MIN_COHORT = 5.0
HIGH_CONF = 0.7
# A week is complete once its last game is this long past kickoff.
GAME_HOURS = 5


# ---------------------------------------------------------------- statistics

def summarize(errors: list[float], pairs: list[tuple[float, float]] | None = None) -> dict:
    """n, bias (actual - forecast), its standard error, MAE, and correlation."""
    n = len(errors)
    if not n:
        return {"n": 0}
    out = {"n": n, "mean": round(st.mean(errors), 3),
           "se": round(st.pstdev(errors) / math.sqrt(n), 3) if n > 1 else None,
           "mae": round(st.mean(abs(e) for e in errors), 3)}
    if pairs and len(pairs) >= 3:
        f, a = zip(*pairs)
        if st.pstdev(f) > 0 and st.pstdev(a) > 0:
            out["corr"] = round(st.correlation(f, a), 3)
    return out


def persistent(r1: list[float], r2: list[float]) -> dict:
    """Correlated part of two weeks' misses for the same players.

    Independent per-week noise does not correlate across weeks; a player whose
    true rate differs from his projection misses in the same direction twice.
    sqrt(cov) is the persistent error per week -- the sigma an optimizer's-curse
    correction needs.
    """
    n = len(r1)
    if n < 10:
        return {"n": n}
    c = st.covariance(r1, r2)
    rho = st.correlation(r1, r2)
    return {"n": n, "corr": round(rho, 3), "corr_se": round((1 - rho * rho) / math.sqrt(n - 1), 3),
            "cov": round(c, 3), "sigma_e": round(math.sqrt(c), 3) if c > 0 else 0.0}


def newest(rows: list[dict]) -> list[dict]:
    """The newest row per (week, metric, source, group, label)."""
    best: dict = {}
    for r in rows:
        k = (r.get("week"), r.get("metric"), r.get("source"), r.get("group"), r.get("label"))
        if k not in best or r.get("computed_at", 0) >= best[k].get("computed_at", 0):
            best[k] = r
    return list(best.values())


def read_ledger(path: Path | None = None) -> list[dict]:
    p = path or LEDGER
    if not p.exists():
        return []
    return newest([json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])


# ---------------------------------------------------------------- inputs

class Context:
    """Everything a week is scored from, loaded once."""

    def __init__(self):
        self.caps, self.kick, self.teams, self.pos = sleeper_lag._captures()
        self.levels = sleeper_lag.levels()
        self.hist = sleeper_lag._verdict_history()
        self.played = sleeper_lag._played()
        self.values = value_history.snapshots()
        self._actuals: dict = {}
        self._vintage: dict = {}
        self._weekly: dict = {}

    def actuals(self, week: int) -> dict:
        if week not in self._actuals:
            sc = season.scoring()
            raw = api.get(f"stats/nfl/regular/{season.SEASON}/{week}") or {}
            self._actuals[week] = {str(pid): custom_points(s or {}, sc) for pid, s in raw.items()}
        return self._actuals[week]

    def kickoff(self, week: int, pid: str) -> float | None:
        return self.kick.get((week, self.teams.get((week, pid))))

    def model(self, week: int, pid: str, ko: float) -> tuple[dict | None, str]:
        key = (week, ko)
        if key not in self._vintage:
            self._vintage[key] = model_proj.vintage_before(season.SEASON, week, ko)
        doc = self._vintage[key]
        if doc is not None:
            return (doc.get("players") or {}).get(pid), "observed"
        if week not in self._weekly:
            p = MODEL_WEEKLY / f"weekly_{season.SEASON}_wk{week:02d}.json"
            self._weekly[week] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        return (self._weekly[week].get("players") or {}).get(pid), "reconstructed"

    def bot(self, week: int, pid: str, ko: float) -> tuple[float | None, str]:
        before = [d for d in self.values if float(d.get("computed") or 0) < ko]
        observed = [d for d in before if not d.get("reconstructed")]
        pick, label = (observed[-1], "observed") if observed else (
            (before[-1], "reconstructed") if before else (None, ""))
        if pick is None:
            return None, ""
        v = ((pick.get("players") or {}).get(pid) or {}).get("by_week", {}).get(str(week))
        return (float(v) if v is not None else None), label


def cohort(ctx: Context, week: int) -> list[dict]:
    """One row per cohort player: every forecast before his kickoff, and the outcome."""
    caps = ctx.caps.get(week) or []
    if not caps:
        return []
    actual = ctx.actuals(week)
    first_t = caps[0][0]
    rows = []
    for pid in {p for _t, pts in caps for p in pts}:
        if ctx.pos.get((week, pid)) not in SKILL:
            continue
        ko = ctx.kickoff(week, pid)
        if not ko:
            continue
        series = [(t, pts[pid]) for t, pts in caps if t < ko and pid in pts]
        if not series or max(v for _t, v in series) < MIN_COHORT:
            continue
        m, m_label = ctx.model(week, pid, ko)
        b, b_label = ctx.bot(week, pid, ko)
        tl = ctx.levels.get(pid) or []
        lv = sleeper_lag.level_at(tl, ko)
        flagged = lv > 0 or any(first_t < t < ko and l > 0 for t, l in tl)
        rows.append({"player_id": pid, "pos": ctx.pos.get((week, pid)), "ko": ko,
                     "sleeper": series[-1][1], "model": (m or {}).get("mean"),
                     "p10": (m or {}).get("p10"), "p90": (m or {}).get("p90"),
                     "model_label": m_label, "bot": b, "bot_label": b_label,
                     "status": ("healthy", "questionable", "out")[lv], "flagged": flagged,
                     "actual": actual.get(pid, 0.0)})
    return rows


# ---------------------------------------------------------------- metrics

def _label(rows: list[dict], key: str) -> str:
    labels = {r[key] for r in rows if r.get(key)}
    return labels.pop() if len(labels) == 1 else ("mixed" if labels else "")


def accuracy(rows: list[dict]) -> list[dict]:
    out = []
    groups = {"all": rows, "flagged": [r for r in rows if r["flagged"]],
              "never_flagged": [r for r in rows if not r["flagged"]]}
    groups.update({f"status:{s}": [r for r in rows if r["status"] == s]
                   for s in ("healthy", "questionable", "out")})
    groups.update({f"pos:{p}": [r for r in rows if r["pos"] == p] for p in SKILL})
    for source, label_key in (("sleeper", None), ("model", "model_label"), ("bot", "bot_label")):
        for g, grp in groups.items():
            have = [r for r in grp if r.get(source) is not None]
            if not have:
                continue
            e = [r["actual"] - r[source] for r in have]
            out.append({"metric": "kickoff_accuracy", "source": source, "group": g,
                        "label": _label(have, label_key) if label_key else "observed",
                        **summarize(e, [(r[source], r["actual"]) for r in have])})
    cov = [r for r in rows if r.get("p10") is not None and r.get("p90") is not None]
    if cov:
        inside = sum(r["p10"] <= r["actual"] <= r["p90"] for r in cov)
        out.append({"metric": "model_coverage_p10_p90", "source": "model", "group": "all",
                    "label": _label(cov, "model_label"), "n": len(cov),
                    "mean": round(inside / len(cov), 3), "target": 0.8})
    return out


def persistence(prev: list[dict], cur: list[dict]) -> list[dict]:
    out = []
    a = {r["player_id"]: r for r in prev}
    for source in ("sleeper", "model"):
        both = [(a[r["player_id"]], r) for r in cur if r["player_id"] in a
                and r.get(source) is not None and a[r["player_id"]].get(source) is not None]
        r1 = [p["actual"] - p[source] for p, _c in both]
        r2 = [c["actual"] - c[source] for _p, c in both]
        out.append({"metric": "persistent_error", "source": source, "group": "pair",
                    "label": "observed", **persistent(r1, r2)})
    return out


def scout(ctx: Context, rows: list[dict]) -> list[dict]:
    out = []
    tagged = []
    for r in rows:
        v = sleeper_lag.verdict_as_of(r["player_id"], r["ko"], ctx.hist)
        if v.get("verdict"):
            src = "first_observed" if str(v.get("writer", "")).startswith("backfill") else "logged"
            tagged.append((r, v, src))
    for src in ("first_observed", "logged"):
        for verdict in ("avoid", "neutral", "boost"):
            for conf_group, floor in (("all", 0.0), ("conf>=0.7", HIGH_CONF)):
                grp = [r for r, v, s in tagged if s == src and v["verdict"] == verdict
                       and float(v.get("confidence") or 0) >= floor]
                if not grp:
                    continue
                e = [r["actual"] - r["sleeper"] for r in grp]
                out.append({"metric": "scout_residual_vs_sleeper", "source": "scout",
                            "group": f"{verdict}:{conf_group}", "label": src, **summarize(e)})
    return out


def return_weeks(ctx: Context, week: int) -> list[dict]:
    """Did a player named to return this week actually play this week?"""
    counts = {"on_time": 0, "later_than_said": 0, "earlier_than_said": 0}
    src_seen = set()
    for (w, pid), team in ctx.teams.items():
        if w != week:
            continue
        ko = ctx.kick.get((week, team))
        if not ko:
            continue
        v = sleeper_lag.verdict_as_of(pid, ko, ctx.hist)
        rw = v.get("return_week")
        if not rw:
            continue
        src_seen.add("first_observed" if str(v.get("writer", "")).startswith("backfill") else "logged")
        played = (pid, week) in ctx.played
        if int(rw) == week:
            counts["on_time" if played else "later_than_said"] += 1
        elif int(rw) > week and played:
            counts["earlier_than_said"] += 1
    n = sum(counts.values())
    label = src_seen.pop() if len(src_seen) == 1 else ("mixed" if src_seen else "")
    return [{"metric": "scout_return_week", "source": "scout", "group": k, "label": label,
             "n": c, "of": n} for k, c in counts.items()] if n else []


def lag(ctx: Context, week: int) -> list[dict]:
    """Status -> reprice lag, for onto-absence events that fell in this week."""
    kicks = [k for (w, _t), k in ctx.kick.items() if w == week]
    prev = [k for (w, _t), k in ctx.kick.items() if w == week - 1]
    if not kicks:
        return []
    lo, hi = (max(prev) if prev else min(kicks) - 7 * 86400), max(kicks)
    evs, _q = sleeper_lag.events()
    moves = sleeper_lag.projection_moves()
    # Only men worth pricing, as in the lag report: projected at MIN_COHORT+
    # this week or next -- a Sunday injury is repriced in NEXT week's number.
    worth = {pid for w in (week, week + 1) for _t, pts in ctx.caps.get(w, [])
             for pid, v in pts.items() if v >= MIN_COHORT}
    lags, firsts = [], []
    for e in evs:
        if (e["direction"] != "onto" or not lo < e["at"] <= hi
                or e["player_id"] not in worth):
            continue
        nxt = min((x["at"] for x in evs if x["player_id"] == e["player_id"] and x["at"] > e["at"]),
                  default=float("inf"))
        pm = [t for t in moves.get(e["player_id"], []) if e["at"] <= t < nxt]
        if pm:
            lags.append((pm[0] - e["at"]) / 3600)
            firsts.append((pm[0] - e["first_at"]) / 3600)
    out = []
    for group, xs in (("from_designation", lags), ("from_first_flag", firsts)):
        if xs:
            q = sorted(xs)
            out.append({"metric": "sleeper_reprice_lag_h", "source": "sleeper", "group": group,
                        "label": "observed", "n": len(q), "mean": round(st.median(q), 2),
                        "p90": round(q[int(0.9 * (len(q) - 1))], 2)})
    return out


def executed_moves(ctx: Context, week: int, detail: list | None = None) -> list[dict]:
    """Added vs dropped player's actual points, for real moves made before this week.

    A SANITY CHECK, NOT THE SIMULATOR'S GAIN: it ignores whether either man
    would have started for us, which is what the lineup-marginal figure prices.

    SLEEPER'S COMPLETED TRANSACTIONS, not the bot's journal: the journal records
    a claim when it is SUBMITTED, and only Sleeper knows which ones won.
    """
    from robo.transactions import ROSTER_ID
    actual = ctx.actuals(week)
    diffs = []
    for leg in range(1, week + 1):
        for t in api.transactions(LEAGUE_ID_2026, leg) or []:
            if t.get("status") != "complete" or t.get("type") == "commissioner":
                continue
            adds = [p for p, rid in (t.get("adds") or {}).items() if rid == ROSTER_ID]
            drops = [p for p, rid in (t.get("drops") or {}).items() if rid == ROSTER_ID]
            if not adds or not drops:
                continue                          # an open-slot add has nothing to compare
            at = float(t.get("status_updated") or t.get("created") or 0) / 1000
            kos = [k for k in (ctx.kickoff(week, p) for p in adds + drops) if k]
            if not kos or at >= min(kos):
                continue                          # made after this week began
            d = sum(actual.get(p, 0.0) for p in adds) - sum(actual.get(p, 0.0) for p in drops)
            diffs.append(d)
            if detail is not None:
                detail.append(({"at": at, "adds": adds, "drops": drops}, d))
    return [{"metric": "executed_move_points", "source": "bot", "group": "add_minus_drop",
             "label": "sanity_check", **summarize(diffs)}] if diffs else []


# ---------------------------------------------------------------- orchestration

def completed_weeks(ctx: Context) -> list[int]:
    now = time.time()
    weeks = {w for (w, _t) in ctx.kick}
    return sorted(w for w in weeks
                  if max(k for (ww, _t), k in ctx.kick.items() if ww == w) + GAME_HOURS * 3600 < now)


def score_week(ctx: Context, week: int, detail: bool = False) -> list[dict]:
    rows = cohort(ctx, week)
    if not rows:
        return []
    out = accuracy(rows) + scout(ctx, rows) + return_weeks(ctx, week) + lag(ctx, week)
    if week > 1:
        out += persistence(cohort(ctx, week - 1), rows)
    moves_detail: list = []
    out += executed_moves(ctx, week, moves_detail if detail else None)
    now = time.time()
    for r in out:
        r.update({"week": week, "computed_at": now})
    if detail:
        _print_detail(week, rows, moves_detail)
    return out


def append(rows: list[dict], path: Path | None = None) -> None:
    p = path or LEDGER
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def render(rows: list[dict]) -> str:
    L = []
    for w in sorted({r["week"] for r in rows}):
        L.append(f"\nWEEK {w}")
        for r in sorted((r for r in rows if r["week"] == w),
                        key=lambda r: (r["metric"], r["source"], r["group"], r["label"])):
            extra = "  ".join(f"{k} {r[k]}" for k in ("mean", "se", "mae", "corr", "sigma_e",
                                                        "p90", "of", "target") if r.get(k) is not None)
            L.append(f"  {r['metric']:<26} {r['source']:<8} {r['group']:<22} "
                     f"{r['label']:<14} n={r.get('n', 0):<4} {extra}")
    return "\n".join(L)


def _print_detail(week, rows, moves_detail) -> None:
    from robo import sleeper_read
    dump = sleeper_read.players() or {}
    print(f"\nWEEK {week} DETAIL (not stored)")
    for r in sorted(rows, key=lambda r: r["actual"] - r["sleeper"]):
        name = (dump.get(r["player_id"]) or {}).get("full_name") or r["player_id"]
        print(f"  {name[:22]:<22} {r['pos']:<2} {r['status']:<12} sleeper {r['sleeper']:5.1f}  "
              f"model {r['model'] if r['model'] is not None else float('nan'):5.1f}  "
              f"bot {r['bot'] if r['bot'] is not None else float('nan'):5.1f}  actual {r['actual']:5.1f}")
    for j, d in moves_detail:
        print(f"  move {time.strftime('%m-%d %H:%M', time.gmtime(j['at']))}Z "
              f"+{j.get('adds')} -{j.get('drops')}: add minus drop {d:+.1f}")


def main():
    ap = argparse.ArgumentParser(description="weekly forecast scorecard (measures only)")
    ap.add_argument("--week", type=int)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--latest", action="store_true",
                    help="score the most recent completed week (the Tuesday task)")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    if a.week is None and not (a.backfill or a.latest):
        print(render(read_ledger()) or "ledger is empty")
        return
    ctx = Context()
    done = set(completed_weeks(ctx))
    if not done:
        raise SystemExit("no completed week to score")
    weeks = sorted(done) if a.backfill else [max(done)] if a.latest else [a.week]
    fresh = []
    for w in weeks:
        if w not in done:
            raise SystemExit(f"week {w} is not complete yet")
        fresh += score_week(ctx, w, detail=a.detail)
    append(fresh)
    print(render(newest(fresh)))


if __name__ == "__main__":
    main()
