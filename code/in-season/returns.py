"""When a hurt man comes back -- measured, and as a distribution rather than a date.

WHY THIS EXISTS. Sleeper's weekly feed encodes a return as a HARD STEP. Jordyn
Tyson, on IR with a hamstring, reads 0.00 for weeks 1-4 and then 8.90-9.10 every
week from 5 through 17. There is no uncertainty in that number anywhere: one
week he is worth nothing and the next he is a full-time starter. The three dated
sources on him actually spanned three weeks -- IR designated-to-return made week
5 his earliest eligible date, ESPN's timeline at the time of injury targeted
week 6, and the two-month estimate landed near week 7 -- and Sleeper took the
earliest of them. A step cannot carry that spread, so nothing downstream can
either.

The other half of the same defect is that a scout verdict arrives as ONE SCALAR
for the whole rest of the season. Tyson's 0.82 multiplier is applied uniformly to
weeks 5 through 17, which marks down his week-14 value for a hamstring that will
long since have healed. A verdict about WHEN a man returns has to land on the
weeks it is about.

WHAT IS MEASURED. Ten seasons of nflverse injury reports, cross-referenced
against who actually appears in player_stats -- the same absence-is-not-a-zero
grid roles.py had to build, for the same reason: a player who did not play has no
row, so "did not play" reads as ABSENT and never as 0.

    injury         n   median   mean    p90
    Hamstring    367      1.0   1.82    4.0
    Knee         364      1.0   1.95    4.0
    Ankle        324      1.0   1.84    4.0
    Concussion   248      0.0   1.03    3.0

CENSORING IS NOT OPTIONAL HERE. A man who is hurt in week 15 and never plays
again is not a one-week absence, and dropping him -- or counting his spell as
three weeks because the season ended -- biases every curve short, which is the
direction that matters: it would tell the bot that everyone comes back soon.
Spells that reach the end of the season are right-censored and the survival curve
is Kaplan-Meier, so they contribute what they actually observed and no more.

THE CURVE IS CONDITIONAL ON TIME ALREADY MISSED. "How long will he be out" is the
wrong question once a man has already sat four weeks; the right one is how long
he has left, given he is still out. P(back within k more weeks | already out j)
is read straight off the survival curve, so the same fit answers week 1 and week 6.

WHAT IT CANNOT DO. This is fitted on a GAME-WEEK `Out` designation, not on IR.
An IR spell is categorically longer, and nflverse roster-status history is not on
disk, so the IR floor comes from Sleeper's own earliest-eligible week and the
spread above that floor is only knowable from reporting. `tier` says which of the
two any given answer came from, because a number fitted on 367 hamstrings and a
number carried by a pooled fallback are not the same claim.

    python -m robo.returns --fit
    python -m robo.returns --report
    python -m robo.returns --curve Hamstring
"""

import argparse
import json
import time
from functools import lru_cache

from robo import DATA, MODEL_ROOT, roles, settings

PARQUET = MODEL_ROOT / "data" / "parquet"
FIT_FILE = DATA / "returns_fit.json"
SCHEMA = 1

FIT_FIRST, FIT_LAST = 2016, 2025

# How far out the curve is carried. Past this the survivors are a different
# population -- season-ending injuries -- and a rest-of-season sum has nothing
# left to spend on them anyway.
MAX_WEEKS = 12

# Below this many spells a body part's own curve is noise and falls back to the
# pooled one. Same discipline and same number as roles.MIN_EVENTS, which was set
# after a four-event cell priced a fifth-string back like a starter.
MIN_EVENTS = 25

# Designations that start a spell. `Doubtful` is included because it resolves to
# out far more often than not; `Questionable` is not, because it is the league's
# most-abused label and a man carrying it usually plays.
OUT_STATUSES = ("Out", "Doubtful")

# What a player on IR is assumed to be worth before his earliest eligible week.
# Not a fitted number -- it is the league rule, and the rule is that he cannot
# play at all.
IR_FLOOR = 0.0

settings.apply(__name__, globals())


# ------------------------------------------------------------------ the fit

def _played(seasons) -> tuple[set, set]:
    """(who played in which week, which weeks each team actually played).

    The second set is what keeps a bye from being counted as a week missed.
    """
    d = roles._stats(seasons)
    if d is None:
        return set(), set()
    played = set(zip(d["season"].to_list(), d["week"].to_list(),
                     d["player_id"].to_list()))
    team_weeks = set(zip(d["season"].to_list(), d["week"].to_list(),
                         d["team"].to_list()))
    return played, team_weeks


def _injuries(seasons):
    """Injury reports for the skill positions, one row per player-week."""
    import polars as pl
    frames = []
    cols = ["season", "week", "gsis_id", "position", "team",
            "report_status", "report_primary_injury"]
    for yr in seasons:
        p = PARQUET / f"injuries_{yr}.parquet"
        if not p.exists():
            continue
        try:
            df = pl.read_parquet(p)
        except Exception:
            continue
        if any(c not in df.columns for c in cols):
            continue
        df = df.select(cols).with_columns(
            pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))
        frames.append(df.filter(pl.col("position").is_in(list(roles.OPPORTUNITY))))
    return pl.concat(frames, how="diagonal") if frames else None


def spells(seasons=None) -> list[dict]:
    """Every absence spell that began with an out-designation.

    ONE SPELL PER ABSENCE, NOT ONE PER REPORT. A man listed Out in four
    consecutive weeks is one four-week injury, and counting him four times would
    both inflate n and drag every curve toward zero, since the later rows start
    partway through an absence already in progress.
    """
    seasons = list(seasons or range(FIT_FIRST, FIT_LAST + 1))
    inj = _injuries(seasons)
    if inj is None:
        return []
    played, team_weeks = _played(seasons)

    out_rows = {}
    for s, w, g, _pos, tm, st, bp in inj.iter_rows():
        if st in OUT_STATUSES and g:
            out_rows[(s, g, w)] = (bp or "Unknown", tm)

    last_week = {}
    for s, w, _t in team_weeks:
        last_week[s] = max(last_week.get(s, 0), w)

    spells = []
    for (s, g, w), (bp, tm) in sorted(out_rows.items()):
        # A new spell only if he was not already out the week before. Playing in
        # w-1 counts as not-out even when he carried a designation.
        if (s, g, w - 1) in out_rows and (s, w - 1, g) not in played:
            continue
        if (s, w, g) in played:
            continue                      # designated, then played anyway
        missed, back, k = 0, False, w + 1
        while k <= last_week.get(s, 18):
            if (s, k, tm) not in team_weeks:
                k += 1                    # his team was on bye; not a week missed
                continue
            if (s, k, g) in played:
                back = True
                break
            missed += 1
            k += 1
        spells.append({"season": s, "gsis": g, "week": w, "injury": bp,
                       "missed": missed, "returned": back})
    return spells


def _km(durations: list[tuple[int, bool]]) -> list[float]:
    """Kaplan-Meier survival, S[k] = P(still out after k weeks).

    `durations` is (weeks missed, did he return). A censored spell leaves the
    risk set without ever counting as a return, which is the whole point: the
    man who never came back must not look like a fast recovery.
    """
    S, surv, at_risk = [], 1.0, len(durations)
    for k in range(MAX_WEEKS + 1):
        died = sum(1 for d, ret in durations if ret and d == k)
        cens = sum(1 for d, ret in durations if not ret and d == k)
        if at_risk > 0:
            surv *= (1 - died / at_risk)
        S.append(round(surv, 4))
        at_risk -= (died + cens)
    return S


def fit(seasons=None) -> dict:
    sp = spells(seasons)
    by_injury: dict[str, list] = {}
    for s in sp:
        by_injury.setdefault(s["injury"], []).append((s["missed"], s["returned"]))
    pooled = [(s["missed"], s["returned"]) for s in sp]
    curve = {bp: {"n": len(v), "S": _km(v)}
             for bp, v in by_injury.items() if len(v) >= MIN_EVENTS}
    out = {"schema": SCHEMA, "fitted": time.time(),
           "seasons": [min(seasons or [FIT_FIRST]), max(seasons or [FIT_LAST])],
           "spells": len(sp),
           "censored": sum(1 for s in sp if not s["returned"]),
           "min_events": MIN_EVENTS, "max_weeks": MAX_WEEKS,
           "pooled": {"n": len(pooled), "S": _km(pooled)},
           "curve": curve}
    FIT_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


@lru_cache(maxsize=1)
def load_fit() -> dict:
    try:
        d = json.loads(FIT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d if d.get("schema") == SCHEMA else {}


# ------------------------------------------------------------- the questions

def survival(injury: str | None, record: dict | None = None) -> tuple[list, str]:
    """(S[k], tier) for this body part, pooled when its own cell is thin."""
    f = load_fit()
    if not f:
        return [], "no fit on disk"
    cell = (f.get("curve") or {}).get(injury or "")
    if cell:
        if record is not None:
            record.update({"injury": injury, "n": cell["n"], "tier": "fitted"})
        return cell["S"], f"fitted, n={cell['n']}"
    p = f.get("pooled") or {}
    if record is not None:
        record.update({"injury": injury, "n": p.get("n"), "tier": "pooled"})
    return p.get("S") or [], f"pooled, n={p.get('n')} (no cell for {injury!r})"


def back_by(injury: str | None, ahead: int, missed: int = 0,
            assume_return: bool = False, record: dict | None = None) -> float:
    """P(he is back within `ahead` more weeks | already out `missed`).

    Conditioning on time served is what makes one fit answer every week of a
    spell: the survivors at week 4 are a slower population than the men who
    entered it, and reading the unconditional curve would keep promising a
    return that has already failed to arrive four times.

    `assume_return` ALSO conditions on his coming back at all this season, which
    matters more than it sounds. Every curve here plateaus below 1.0 because
    some share of spells are season-ending -- hamstrings level off near 85% --
    so a man asked about in week 14 comes back at 0.79 no matter how long he has
    had to heal, and the missing 21% is not "still hurt", it is "was a different
    injury entirely". Where something independent says he IS expected back --
    IR designated-to-return, or a reported timeline -- that mass does not apply
    to him and dividing it out is the correct read, not an optimistic one.
    """
    S, why = survival(injury, record=record)
    if not S:
        return 0.0
    j = min(max(missed, 0), len(S) - 1)
    k = min(j + max(ahead, 0), len(S) - 1)
    base = S[j]
    if base <= 0:
        return 1.0
    p = 1.0 - (S[k] / base)
    if assume_return:
        eventual = 1.0 - (S[-1] / base)
        # A curve that never resolves carries no timing information to rescale;
        # saying so beats dividing by something indistinguishable from zero.
        if eventual > 0.05:
            p = p / eventual
            why += ", rescaled to a certain return"
    if record is not None:
        record.update({"missed": j, "ahead": ahead, "S_j": S[j], "S_k": S[k],
                       "assume_return": assume_return,
                       "p_back": round(min(p, 1.0), 4), "why": why})
    return max(0.0, min(1.0, p))


def availability(injury_status: str | None, injury: str | None, week: int,
                 now: int, missed: int = 0, eligible_week: int | None = None,
                 record: dict | None = None) -> float:
    """P(on the field in `week`), for a man carrying `injury_status` today.

    IR IS A RULE, NOT A FORECAST. Before his earliest eligible week he cannot
    play at all, so the curve is not consulted -- consulting it there would hand
    back a cheerful recovery probability for a week the league forbids him from
    playing in. From that week on the fit takes over, which is the only part
    that is a forecast.
    """
    if not injury_status or injury_status in ("Questionable", "NA", None):
        if record is not None:
            record.update({"mode": "healthy", "p": 1.0, "status": injury_status})
        return 1.0
    if injury_status == "IR" and eligible_week and week < eligible_week:
        if record is not None:
            record.update({"mode": "ir-ineligible", "p": IR_FLOOR,
                           "eligible_week": eligible_week})
        return IR_FLOOR
    # An earliest-eligible week is only ever set for IR designated-to-return,
    # which is a team declaring it expects him back -- so the season-ending mass
    # in the raw curve is not his.
    ahead = max(0, week - now)
    rec: dict = {}
    p = back_by(injury, ahead, missed,
                assume_return=bool(eligible_week), record=rec)
    if record is not None:
        record.update({"mode": "curve", "p": round(p, 4),
                       "status": injury_status, "detail": rec})
    return p


# ---------------------------------------------------------------- reporting

def report() -> str:
    import statistics
    f = load_fit()
    if not f:
        return "no fit on disk -- run `python -m robo.returns --fit`"
    L = [f"RETURN CURVES - {f['spells']} spells, "
         f"{f['censored']} never returned that season "
         f"({f['seasons'][0]}-{f['seasons'][1]})", ""]
    L.append(f"  {'injury':<16}{'n':>6}   " +
             "  ".join(f"wk+{k}" for k in range(1, 7)))
    rows = sorted((f.get("curve") or {}).items(), key=lambda kv: -kv[1]["n"])
    for bp, cell in rows:
        back = [1 - cell["S"][k] for k in range(1, 7)]
        L.append(f"  {bp[:15]:<16}{cell['n']:>6}   " +
                 "  ".join(f"{b:>4.0%}" for b in back))
    p = f["pooled"]
    L.append(f"  {'(pooled)':<16}{p['n']:>6}   " +
             "  ".join(f"{1 - p['S'][k]:>4.0%}" for k in range(1, 7)))
    L += ["", "  Read as: P(back within this many weeks), given he has just been",
          "  ruled out. Conditioned on weeks already missed by back_by()."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fit", action="store_true")
    g.add_argument("--report", action="store_true")
    g.add_argument("--curve", type=str, help="one body part, week by week")
    args = ap.parse_args()

    if args.fit:
        d = fit()
        print(f"fitted {d['spells']} spells ({d['censored']} censored), "
              f"{len(d['curve'])} body parts with their own curve "
              f"-> {FIT_FILE.name}")
        return
    if args.curve:
        S, why = survival(args.curve)
        if not S:
            print(f"  {why}")
            return
        print(f"{args.curve}  ({why})")
        print(f"  {'weeks out':<12}{'P(back by then)':>18}")
        for k in range(1, min(len(S), 9)):
            print(f"  {k:<12}{1 - S[k]:>17.1%}")
        return
    print(report())


if __name__ == "__main__":
    main()
