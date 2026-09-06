"""Expected points, conditioned on actually playing -- one number, built once.

WHAT WAS WRONG. ros.py estimates "will he be on the field in a role" FOUR times,
with mechanisms that do not know about each other, and then adds them up:

  * the weekly feed is a rate conditional on him STARTING, so it silently sets
    the probability to 1 for a starter and 0 for a backup -- twelve men with a
    real season projection price at 0.00, and since moves.py decides adds on
    that number the bot cannot acquire a rising backup at all;
  * the same feed also encodes a RETURN DATE, as a hard step -- Jordyn Tyson
    reads 0.00 through week 4 and 8.90 from week 5, with no uncertainty in it
    anywhere, when the three dated sources on him spanned weeks 5 to 7;
  * `upside` is a fifth-column estimate of the same thing, ADDED to the total,
    and since P(miss) and absorption are position constants it reduces to the
    starter's rate times 6.1% -- it ranks backups by the quality of the man
    ahead of them, which is why Flacco priced 46x Carson Beck;
  * `news_mult` is one scalar for the whole rest of the season, applied at half
    strength because nobody could tell whether the feed had already priced it.

THE SHAPE HERE. Two factors, kept apart because they fail differently:

    value(w) = A(w) x SUM over states of  P(state, w) x points(state, w)

A(w) is availability -- a distribution over return week from robo/returns.py,
never a step. The states are S1 (the role he has) and S2 (the role ahead of
him), so inheritance is part of the total rather than added to it, and the
question "is upside double-counted" stops being a judgment call.

THE LEVEL COMES FROM THE MARKET, AND THAT IS THE WHOLE TRICK. Sleeper publishes
two projections that are different objects: the weekly feed is conditional on
starting, and the SEASON file is unconditional -- the chance he plays is already
inside the projected volume. Their ratio is the market's own implied
P(available and in a role), and it is free. So the structural model above is
built, then SCALED so its total matches what the market says the man is worth:

    k = (season points x games left / 17)  /  SUM over w of raw(w)

k is therefore the RESIDUAL -- everything the structural model does not explain
-- and reading it is the point. A backup whose k is near 1 is priced exactly as
injury luck implies. A backup whose k is 3 or 4 is one the market expects to
play for reasons no hazard rate knows about, which is a handoff being priced in.
That single number separates Carson Beck from Joe Flacco without anyone
asserting that it should.

WHERE THE IDENTITY DOES NOT BIND. It is a constraint, not an axiom. Both feeds
descend from the same assumptions, so when they AGREE they have only confirmed a
shared premise -- Tyson's k is 0.84, comfortably healthy, because both were built
on the same week-5 return. Where something independent contradicts them, the
level is released rather than pinned, and `k_source` says so on the row.

    python -m robo.expected --top 40
    python -m robo.expected --explain "Player Name"
    python -m robo.expected --compare            # against ros.py
"""

import argparse
import json
import time

from robo import DATA, LEAGUE_ID_2026, injuries, rankings, returns, roles, ros, scout
from robo import season, settings
from robo import sleeper_read as api

CACHE = DATA / "expected.json"
SCHEMA = 1

# A full NFL season of games behind the season projection. The league plays
# through week 17 and the projection covers all 17 games, so a rest-of-season
# target has to be prorated by games actually LEFT -- crediting a man with an
# eighteenth week nobody can start him in inflates every target by about 6%.
SEASON_GAMES = 17

# Bounds on the residual. It is a real quantity and mostly it should be left
# alone, but one stale row should not be able to move a valuation by an order of
# magnitude on its own. Set wide on purpose: k of 3-4 is a legitimate reading of
# a backup the market expects to play, and clamping that away would delete the
# exact signal this module was built to find.
K_CLAMP = (0.25, 6.0)

# Below this the raw structural total is too small to divide by -- a man the
# model gives almost nothing carries no shape for k to scale, and the ratio
# explodes on rounding noise rather than on information.
MIN_RAW_TO_SCALE = 1.0

settings.apply(__name__, globals())


# ------------------------------------------------------------------ the parts

# Sleeper designations that mean he cannot play now, so a zero-then-positive
# feed is describing a RETURN. Outside this set the same shape is a role
# forecast -- Shedeur Sanders is projected nothing until week 12 because he is
# not the starter, and reading that as an injury zeroed ten weeks of a perfectly
# healthy quarterback.
SIDELINED = ("IR", "PUP", "NFI", "Out", "Doubtful", "DNR", "COV", "Sus")


def _feed_eligible_week(pid: str, rates: dict, status: str | None) -> int | None:
    """The first week Sleeper still projects him for anything -- the FALLBACK.

    Sleeper encodes a return as a step from zero, and this used to be where the
    eligibility floor came from. It is not reliable enough for that: James
    Conner went on injured reserve on 30 August and cannot play until week 5,
    while this feed projected him 2.7 points in weeks 2, 3 and 4. So ESPN's
    published return date leads (see robo/injuries.py) and this answers only for
    men ESPN has nothing on.

    Gated on the designation for the same reason. A step is only evidence of a
    return if something is keeping him out; otherwise it is Sleeper saying he
    does not have the job yet, which his projection already prices at zero
    without any help from an availability term.
    """
    if not status or status not in SIDELINED:
        return None
    weeks = sorted(rates)
    for w in weeks:
        if (rates[w] or 0) > 0:
            return w if w != weeks[0] else None
    return None


def raw_series(pid: str, pos: str, team: str, rates: dict, player: dict,
               now: int, record: dict | None = None) -> dict:
    """{week: expected points} from the structural model, before calibration."""
    mine = rates.get(pid) or {}
    r = roles.projected_role(pid, team, pos)
    # THE JOB HE INHERITS IS THE LEAD'S, NOT THE MAN'S ONE RUNG ABOVE HIM.
    # roles.fit() defines a vacancy as the rank-1 man's opportunity going to
    # zero and measures `absorbs` as a fraction of HIS vacated share, so pairing
    # it with the rank above was a mismatched numerator and denominator. It made
    # no difference at rank 2, where the two are the same man, and gutted every
    # deep bench player: SF's rank-3 back was priced as inheriting from Jordan
    # James rather than from McCaffrey, which is the whole of the bet.
    ahead = rates.get(r["lead_id"]) if r.get("lead_id") else None
    miss = roles.miss_rate(pos) if pos in roles.OPPORTUNITY else 0.0
    absorb = min(1.0, r.get("absorbs") or 0.0)
    status = player.get("injury_status")

    # THE FLOOR IS A RULE, SO IT IS READ RATHER THAN INFERRED. ESPN publishes
    # the date a man is eligible to return; Sleeper's step is a projection that
    # happens to correlate with it, and disagreed for most of the men on injured
    # reserve. Sleeper answers only where ESPN has nothing.
    frec: dict = {}
    espn_elig = injuries.floor_week(pid, record=frec)
    feed_elig = espn_elig or _feed_eligible_week(pid, mine, status)
    floor_src = "espn" if espn_elig else ("feed" if feed_elig else None)
    # Out for the season is a floor no week can clear. Saying so explicitly
    # beats an eligible week past the last one, which arithmetic would treat as
    # "always too early" but which nothing on the row would explain.
    if injuries.out_for_season(pid):
        feed_elig, floor_src = max(mine or [0]) + 1, "espn (out for the season)"

    # ESPN types a diagnosis where Sleeper writes free text, and it reports a
    # suspension or the exempt list as no body part at all rather than as an
    # injury the return curves have never seen.
    body = injuries.body_part(pid) or player.get("injury_body_part")

    # A DATED ESTIMATE FROM REPORTING OUTRANKS THE FLOOR, and only a dated one.
    # The floor is the earliest a man is ALLOWED back, not a forecast that he
    # will be: Tyson is eligible in week 5, while the reporting at the time of
    # injury targeted week 6 and the two-month estimate landed near week 7.
    # Neither feed can hold that spread because both carry a date and not a
    # distribution. Where the scout has no date this is None and the floor
    # stands -- an absent estimate must never be read as "week 1".
    sig = scout.role_signal(pid)
    scout_wk = sig.get("return_week")
    elig = feed_elig
    if scout_wk and (not feed_elig or scout_wk > feed_elig):
        elig = scout_wk

    # Weeks already served, as of NOW and not as of the week being priced.
    # back_by() takes `missed` and `ahead` and reads the curve at missed+ahead,
    # and `ahead` is already week-minus-now -- so dating this to the target week
    # counts the same elapsed time twice and asks about a man who has been out
    # twice as long as he has.
    served = injuries.since(pid, now)

    out, detail = {}, {}
    for w, p1 in mine.items():
        arec: dict = {}
        a = returns.availability(status, body, w, now, missed=served,
                                 eligible_week=elig, record=arec)
        if elig and w < elig:
            a = 0.0
            arec = {"mode": "before expected return", "p": 0.0,
                    "eligible_week": elig,
                    "from": "scout" if elig == scout_wk else (floor_src or "feed"),
                    "floor": frec or None}
        # S2 is what he picks up if the job ahead opens. It is a term in the
        # sum, never an addition to the total -- that is the difference between
        # this and ros.upside.
        p2 = (ahead or {}).get(w, 0.0) * absorb
        v = a * (p1 + miss * p2)
        out[w] = round(v, 3)
        detail[w] = {"a": round(a, 3), "s1": round(p1, 3),
                     "s2": round(p2, 3), "miss": round(miss, 4),
                     "avail": arec, "pts": round(v, 3)}
    if record is not None:
        record.update({"role": r, "ahead_id": r.get("ahead_id"),
                       "absorbs": absorb, "miss_rate": miss,
                       "eligible_week": elig, "feed_eligible": feed_elig,
                       "floor_source": floor_src, "floor": frec or None,
                       "scout_return": scout_wk, "scout_basis": sig.get("return_basis"),
                       "role_change": sig.get("role_change"),
                       "injury_status": status,
                       "injury": body, "by_week": detail})
    return out


def release_target(target: float, mine: dict, feed_elig: int | None,
                   scout_wk: int | None) -> tuple[float, str]:
    """Adjust the market's level for weeks the reporting says he will miss.

    THE IDENTITY IS A CONSTRAINT, NOT AN AXIOM. Both Sleeper feeds are built on
    the same assumed return, so when they agree they have confirmed a shared
    premise rather than each other -- Tyson's k is a comfortable 0.84 precisely
    because the weekly step and the season volume were both drawn from week 5.
    Calibrating to that target would take the reporting's later date back out
    again at the last step.

    So the target is scaled by the share of playable weeks that survive the
    scout's date, rather than being discarded. Throwing the market away entirely
    would lose the one thing it is best at -- the LEVEL -- to fix the one thing
    it is worst at, the timing.
    """
    if not scout_wk or not (feed_elig and scout_wk > feed_elig):
        return target, "ratio"
    playable = [w for w, p in mine.items() if (p or 0) > 0]
    if not playable:
        return target, "ratio"
    kept = [w for w in playable if w >= scout_wk]
    adj = target * len(kept) / len(playable)
    return adj, (f"ratio, target released from week {feed_elig} to {scout_wk} "
                 f"on reporting ({len(kept)}/{len(playable)} weeks)")


def season_target(pid: str, games_left: int, spts: dict) -> float:
    """What the market says he is worth over the games that are left."""
    return (spts.get(pid) or 0.0) * games_left / SEASON_GAMES


def calibrate(raw_total: float, target: float, clamp: bool = True,
              record: dict | None = None) -> tuple[float, str]:
    """(k, why). The residual between the structural model and the market."""
    if target <= 0:
        if record is not None:
            record.update({"k": 1.0, "source": "none"})
        return 1.0, "no season projection to calibrate against"
    if raw_total < MIN_RAW_TO_SCALE:
        if record is not None:
            record.update({"k": None, "source": "season-only"})
        return 0.0, ("structural model gives ~nothing; the season file is the "
                     "only statement about him")
    k = target / raw_total
    clamped = False
    if clamp and not (K_CLAMP[0] <= k <= K_CLAMP[1]):
        k, clamped = min(max(k, K_CLAMP[0]), K_CLAMP[1]), True
    if record is not None:
        record.update({"k": round(k, 3), "raw_total": round(raw_total, 2),
                       "target": round(target, 2), "clamped": clamped,
                       "source": "ratio"})
    return k, ("ratio" + (" CLAMPED" if clamped else ""))


# --------------------------------------------------------------------- build

def build(week: int | None = None, league_id: str = LEAGUE_ID_2026,
          clamp: bool = True) -> dict:
    wk = week or season.current_week()
    weights = ros.week_weights(wk, league_id)
    wr = ros.weekly_rates(wk, season.SEASON, league_id)
    rates = wr["rates"]
    players = api.players()
    sc = season.scoring(league_id)

    spts = {}
    for r in rankings.load_projections():
        p = r.get("player") or {}
        pid = str(r.get("player_id") or p.get("player_id") or "")
        if pid:
            spts[pid] = rankings.custom_points(r.get("stats") or {}, sc)

    rows = {}
    for pid, byweek in rates.items():
        p = players.get(pid) or {}
        pos = p.get("position") or "DEF"
        # Defences are priced off the betting market and kickers are not ranked
        # at all -- neither has a season-file analogue to calibrate against, so
        # both keep ros.py's treatment rather than being forced through this.
        if pos not in roles.PROJ_OPPORTUNITY:
            continue
        rec: dict = {}
        raw = raw_series(pid, pos, p.get("team") or "", rates, p, wk, record=rec)
        raw_total = sum(raw.values())
        games_left = len(byweek)
        target = season_target(pid, games_left, spts)
        target, rel = release_target(target, rates.get(pid) or {},
                                     rec.get("feed_eligible"),
                                     rec.get("scout_return"))
        krec: dict = {}
        k, why = calibrate(raw_total, target, clamp, record=krec)
        if rel != "ratio":
            why = rel

        if krec.get("source") == "season-only":
            # No shape to scale, so spend the market's number evenly over the
            # games left. Flat is wrong about WHEN and right about HOW MUCH,
            # which is the correct trade here: lineup.py sets lineups off the
            # weekly feed, so this number only ever reaches add/drop decisions.
            per = target / games_left if games_left else 0.0
            series = {w: per for w in byweek}
        else:
            series = {w: k * v for w, v in raw.items()}
        total = sum(weights.get(w, 0.0) * v for w, v in series.items())

        rows[pid] = {
            "player_id": pid, "name": api.player_name(players, pid),
            "pos": pos, "team": p.get("team"),
            "ros": round(total, 2),
            "raw": round(raw_total, 2), "target": round(target, 2),
            "k": krec.get("k"), "k_source": krec.get("source"),
            "k_why": why, "clamped": bool(krec.get("clamped")),
            "weeks": games_left,
            "rank": rec["role"].get("rank"), "share": rec["role"].get("share"),
            "ahead_of": rec["role"].get("ahead_of"),
            # Who the inheritance is actually priced against, which is the man
            # holding the job and not the man one rung up. They differ from
            # rank 3 down, and that is where a trace would otherwise name the
            # wrong player as the reason for a number.
            "lead_of": rec["role"].get("lead_of"),
            "absorbs": rec.get("absorbs"),
            "injury_status": rec.get("injury_status"),
            "eligible_week": rec.get("eligible_week"),
            "feed_eligible": rec.get("feed_eligible"),
            "floor_source": rec.get("floor_source"),
            "scout_return": rec.get("scout_return"),
            "scout_basis": rec.get("scout_basis"),
            "role_change": rec.get("role_change"),
            "min_avail": round(min((d["a"] for d in rec["by_week"].values()),
                                   default=1.0), 3),
            "by_week": {str(w): {**rec["by_week"][w], "final": round(series[w], 3)}
                        for w in sorted(series)},
        }
    return {"schema": SCHEMA, "computed": time.time(), "week": wk,
            "season": season.SEASON, "weights": weights, "clamped": clamp,
            "players": rows}


def load(refresh: bool = False) -> dict:
    if not refresh and CACHE.exists():
        try:
            d = json.loads(CACHE.read_text(encoding="utf-8"))
            if d.get("schema") == SCHEMA and d.get("week") == season.current_week():
                return d
        except Exception:
            pass
    d = build()
    try:
        CACHE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass
    return d


def find(name: str, table: dict | None = None) -> list[str]:
    rows = (table or load())["players"]
    n = name.strip().lower()
    hit = [pid for pid, r in rows.items() if r["name"].lower() == n]
    return hit or [pid for pid, r in rows.items() if n in r["name"].lower()]


# ------------------------------------------------------------------ the trace

def trace(name: str) -> str:
    d = load()
    hits = find(name, d)
    if not hits:
        return f"no expectation on file for {name!r}"
    r = d["players"][hits[0]]
    W = d["weights"]
    L = [f"{r['name']}  ({r['pos']} {r['team']})   rest-of-season {r['ros']}", ""]
    L.append(f"[1] HIS ROOM, as the market projects it")
    L.append(f"    rank {r['rank']} of the {r['team']} {r['pos']} room, "
             f"{(r['share'] or 0):.1%} of its projected opportunity")
    if r["ahead_of"]:
        L.append(f"    behind {r['ahead_of']}")
    # Named separately because it is the number's actual reference. The
    # absorption curve measures a share of the LEAD's vacated work, so from
    # rank 3 down the man he is behind and the job he would inherit are two
    # different people, and printing only the first explains the wrong number.
    if r.get("lead_of"):
        L.append(f"    inherits {(r['absorbs'] or 0):.0%} of {r['lead_of']}'s "
                 f"work if that job opens")
    L.append("")
    L.append(f"[2] AVAILABILITY  (status {r['injury_status'] or 'healthy'})")
    if r.get("feed_eligible"):
        L.append(f"    Sleeper projects nothing before week {r['feed_eligible']} "
                 f"-- his earliest ELIGIBLE date, which is a rule, not a forecast")
    if r.get("scout_return"):
        L.append(f"    reporting says week {r['scout_return']}: "
                 f"{r.get('scout_basis') or 'no basis given'}")
        if r["scout_return"] != r.get("feed_eligible"):
            L.append(f"    -> the reported date is used, and the market's level "
                     f"is released with it")
    elif r.get("feed_eligible"):
        L.append(f"    no dated estimate from reporting, so the feed's date stands")
    if r.get("role_change") and r["role_change"] != "none":
        L.append(f"    reporting has his role {r['role_change']}")
    L.append(f"    lowest A(w) over the weeks left: {r['min_avail']:.3f}")
    L.append("")
    L.append("[3] EACH WEEK")
    L.append(f"    {'wk':<5}{'A':>7}{'S1':>8}{'S2':>8}{'raw':>8}"
             f"{'final':>8}{'weight':>8}")
    for w, b in sorted(r["by_week"].items(), key=lambda kv: int(kv[0])):
        L.append(f"    {w:<5}{b['a']:>7.3f}{b['s1']:>8.2f}{b['s2']:>8.2f}"
                 f"{b['pts']:>8.2f}{b['final']:>8.2f}"
                 f"{W.get(w, W.get(str(w), 1.0)):>8.2f}")
    L.append("")
    L.append("[4] THE LEVEL -- what the market says, against what the model built")
    L.append(f"    structural total   {r['raw']:>8.2f}   "
             f"(availability x role, before calibration)")
    L.append(f"    market target      {r['target']:>8.2f}   "
             f"(season projection over {r['weeks']} games left)")
    if r["k"] is None:
        L.append(f"    k                    n/a     {r['k_why']}")
    else:
        L.append(f"    k                  {r['k']:>8.3f}   {r['k_why']}")
        L.append(f"    k near 1 means injury luck alone explains him; "
                 f"well above 1 means")
        L.append(f"    the market is pricing a role he does not have yet.")
    L.append("")
    L.append(f"[5] rest-of-season {r['ros']}  "
             f"(weeks weighted by our playoff odds)")
    return "\n".join(L)


def report(top: int = 40, pos: str | None = None) -> str:
    d = load()
    rows = [r for r in d["players"].values()
            if not pos or r["pos"] == pos.upper()]
    rows.sort(key=lambda r: -r["ros"])
    L = [f"EXPECTED REST-OF-SEASON - week {d['week']}, "
         f"{len(d['players'])} players", "",
         f"  {'player':<22}{'pos':<5}{'tm':<5}{'ros':>8}{'k':>7}"
         f"{'A min':>7}  note"]
    for r in rows[:top]:
        k = f"{r['k']:.2f}" if r["k"] is not None else "  --"
        note = r["k_source"] + (" CLAMPED" if r["clamped"] else "")
        L.append(f"  {r['name'][:21]:<22}{r['pos']:<5}{str(r['team']):<5}"
                 f"{r['ros']:>8.1f}{k:>7}{r['min_avail']:>7.2f}  {note}")
    return "\n".join(L)


def compare(top: int = 25, pos: str | None = None) -> str:
    """Side by side with ros.py, biggest disagreements first.

    Sorted by absolute change rather than by value, because the rows worth
    reading are the ones the two models disagree about -- a board where 271 of
    364 players barely move is the reassuring part, not the finding.
    """
    d = load()
    old = (json.loads((DATA / "ros.json").read_text(encoding="utf-8"))
           .get("players") or {})
    rows = []
    for pid, r in d["players"].items():
        if pos and r["pos"] != pos.upper():
            continue
        o = (old.get(pid) or {}).get("mean")
        if o is None:
            continue
        rows.append((r, o, r["ros"] - o))
    rows.sort(key=lambda t: -abs(t[2]))
    L = [f"EXPECTED vs ros.py - week {d['week']}, {len(rows)} players", "",
         f"  {'player':<22}{'pos':<5}{'ros.py':>9}{'new':>9}{'delta':>9}"
         f"{'k':>7}  source"]
    for r, o, delta in rows[:top]:
        k = f"{r['k']:.2f}" if r["k"] is not None else "  --"
        L.append(f"  {r['name'][:21]:<22}{r['pos']:<5}{o:>9.1f}{r['ros']:>9.1f}"
                 f"{delta:>+9.1f}{k:>7}  {r['k_source']}"
                 + (" CLAMPED" if r["clamped"] else ""))
    moved = sum(1 for _, _, x in rows if abs(x) > 5)
    L += ["", f"  {moved} of {len(rows)} move by more than 5 points."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--pos", type=str, default=None)
    ap.add_argument("--explain", type=str, default=None)
    ap.add_argument("--compare", action="store_true",
                    help="side by side with ros.py")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    if args.compare:
        print(compare(args.top, args.pos))
        return
    if args.rebuild:
        d = build()
        CACHE.write_text(json.dumps(d), encoding="utf-8")
        print(f"built {len(d['players'])} players -> {CACHE.name}")
        return
    if args.explain:
        print(trace(args.explain))
        return
    print(report(args.top, args.pos))


if __name__ == "__main__":
    main()
