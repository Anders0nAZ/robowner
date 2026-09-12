"""Injury-responsive weekly value for roster management.

One weekly model drives both current and future value:

    value(w) = A(w) x SUM over states of  P(state, w) x points(state, w)

A(w) is availability -- a distribution over return week from robo/returns.py,
never a step. The states are S1 (the role he has) and S2 (the role ahead of
him), so inheritance is part of the total rather than added to it, and the
question "is upside double-counted" stops being a judgment call.

The level is the NFL model's mean for every remaining week, falling back by row
to Sleeper. Season-total projections are archived for research and never enter
this calculation. A successor is the greater of the provider's repriced weekly
number and the pre-event baseline plus fitted inherited opportunity.

    python -m robo.expected --top 40
    python -m robo.expected --explain "Player Name"
    python -m robo.expected --compare            # against ros.py
"""

import argparse
import json
import time

from robo import DATA, LEAGUE_ID_2026, injuries, returns, roles, ros, scout
from robo import season, settings
from robo import sleeper_read as api

CACHE = DATA / "expected.json"
BASELINES = DATA / "role_baselines.json"
# 2: `by_week[w]["miss"]` changed meaning. It was roles.miss_rate(pos), a
# position-wide constant; it is now the REALISED chance the job ahead was open
# that week, which for a known absence comes off the lead's own availability. A
# file written under 1 looks valid and reads wrong, so the bump forces a rebuild.
SCHEMA = 3

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


def availability(pid: str, player: dict, mine: dict, now: int) -> tuple:
    """({week: A(w)}, meta) -- when he can play, and what that rests on.

    Split out of raw_series so a man's availability can be read by somebody
    OTHER than himself. What a backup inherits depends on whether the man ahead
    of him is on the field, and that number lives here.
    """
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
    scout_lo = sig.get("return_week_min")
    scout_hi = sig.get("return_week_max")
    elig = feed_elig
    if scout_lo is not None:
        # Sleeper's zero-to-positive step is a forecast fallback, not a legal
        # floor. Explicit reporting may legitimately put a 50% return chance in
        # a week Sleeper still has at zero. ESPN's eligible week is the only
        # bound the report may not precede.
        elig = max(int(scout_lo), int(espn_elig)) if espn_elig else int(scout_lo)

    # Weeks already served, as of NOW and not as of the week being priced.
    # back_by() takes `missed` and `ahead` and reads the curve at missed+ahead,
    # and `ahead` is already week-minus-now -- so dating this to the target week
    # counts the same elapsed time twice and asks about a man who has been out
    # twice as long as he has.
    served = injuries.since(pid, now)

    a_by, arecs = {}, {}
    for w in mine:
        arec: dict = {}
        if scout_lo is not None and scout_hi is not None:
            possible = list(range(int(scout_lo), int(scout_hi) + 1))
            a = sum(1 for rw in possible if rw <= w) / len(possible)
            arec = {"mode": "reported return range", "p": round(a, 3),
                    "return_week_min": scout_lo, "return_week_max": scout_hi,
                    "basis": sig.get("return_basis")}
        else:
            a = returns.availability(status, body, w, now, missed=served,
                                     eligible_week=elig, record=arec)
        if elig and w < elig:
            a = 0.0
            arec = {"mode": "before expected return", "p": 0.0,
                    "eligible_week": elig,
                    "from": "scout" if elig == scout_wk else (floor_src or "feed"),
                    "floor": frec or None}
        a_by[w], arecs[w] = a, arec
    return a_by, {"eligible_week": elig, "feed_eligible": feed_elig,
                  "floor_source": floor_src, "floor": frec or None,
                  "scout_return": scout_wk,
                  "scout_return_min": scout_lo,
                  "scout_return_max": scout_hi,
                  "scout_basis": sig.get("return_basis"),
                  "role_change": sig.get("role_change"),
                  "injury_status": status, "injury": body, "arecs": arecs}


def raw_series(pid: str, pos: str, team: str, rates: dict, player: dict,
               now: int, avail: dict | None = None,
               baselines: dict | None = None,
               record: dict | None = None) -> dict:
    """{week: expected points} from availability and fitted role inheritance.

    `avail` maps player_id -> {week: A(w)} for everyone, so the chance the job
    ahead of him comes open can be read off THE LEAD'S OWN NUMBER instead of a
    position-wide constant. See the p_open comment below.
    """
    mine = rates.get(pid) or {}
    r = roles.projected_role(pid, team, pos, week=now)
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
    a_by, meta = (avail or {}).get(pid) or availability(pid, player, mine, now)
    arecs = meta.get("arecs") or {}

    out, detail = {}, {}
    baselines = baselines or {}
    for w, p1 in mine.items():
        a = a_by.get(w, 1.0)
        arec = arecs.get(w) or {}
        # S2 is what he picks up if the job ahead opens. It is a term in the
        # sum, never an addition to the total -- that is the difference between
        # this and ros.upside.
        # THE ROOM IS RE-READ EVERY WEEK, because who is ahead of him changes
        # when a man is barred from playing. Isiah Pacheco holds rank 2 in the
        # Detroit backfield on the season projection and cannot play until week
        # 5, so through weeks 1-4 Saylors is the effective RB2 and Vaki the RB3 --
        # and the carries a vacancy would send them are the ones the static room
        # was quietly sending to a man on injured reserve.
        rw = roles.projected_role(pid, team, pos, week=w)
        ahead_w = rates.get(rw["lead_id"]) if rw.get("lead_id") else ahead
        absorb_w = min(1.0, rw.get("absorbs") or 0.0)
        lead_pts = (ahead_w or {}).get(w, 0.0)
        base1 = ((baselines.get(pid) or {}).get(str(w), p1))
        lead_healthy = ((baselines.get(rw.get("lead_id")) or {}).get(
            str(w), lead_pts)) if rw.get("lead_id") else lead_pts
        p2 = lead_healthy * absorb_w
        # HOW OFTEN THE JOB AHEAD IS ACTUALLY OPEN, read off the lead's own
        # availability rather than a position-wide constant. miss_rate is the
        # fitted chance that ANY established starter sits out ANY week -- the
        # right number for a healthy room and badly wrong for a known absence.
        # With San Francisco's quarterback concussed this build put him at 47.8%
        # available in week 3 and simultaneously priced his backup as though the
        # job were 92.7% occupied, which is the same file disagreeing with
        # itself. Same construction marginal.draws() already uses: a known A(w)
        # below 1 overrides the hazard, a healthy lead falls back to it.
        lead_a = ((avail or {}).get(rw.get("lead_id")) or ({},))[0]
        la = lead_a.get(w)
        p_open = (1.0 - la) if (la is not None and la < 1.0) else miss
        structural = base1 + p_open * p2
        # Sleeper may already have reassigned the opportunity. Take the larger
        # complete statement; never stack its repricing on top of ours.
        v = a * max(p1, structural)
        out[w] = round(v, 3)
        # The REALISED chance the door opened, not the nominal constant.
        detail[w] = {"a": round(a, 3), "s1": round(base1, 3),
                     "provider": round(p1, 3),
                     "s2": round(p2, 3), "miss": round(p_open, 4),
                     # Carried per week so the simulator can rebuild the
                     # inheritance from the lead's own number and its own drawn
                     # fraction, instead of dividing s2 back out by a season-long
                     # absorb that no longer applies to every week.
                     "lead": round(lead_healthy, 3), "absorbs": round(absorb_w, 4),
                     "rank": rw.get("rank"),
                     "avail": arec, "pts": round(v, 3)}
    if record is not None:
        record.update({"role": r, "ahead_id": r.get("ahead_id"),
                       "absorbs": absorb, "miss_rate": miss,
                       **{k: v for k, v in meta.items() if k != "arecs"},
                       "by_week": detail})
    return out


def _load_baselines() -> dict:
    try:
        d = json.loads(BASELINES.read_text(encoding="utf-8"))
        return d.get("players") or {} if d.get("schema") == 1 else {}
    except Exception:
        return {}


def _write_baselines(players: dict, week: int) -> None:
    doc = {"schema": 1, "updated": time.time(), "week": week,
           "players": players}
    tmp = BASELINES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(BASELINES)


# --------------------------------------------------------------------- build

def build(week: int | None = None, league_id: str = LEAGUE_ID_2026) -> dict:
    wk = week or season.current_week()
    weights = ros.week_weights(wk, league_id)
    wr = ros.weekly_rates(wk, season.SEASON, league_id)
    rates = wr["rates"]
    players = api.players()
    # EVERYONE'S AVAILABILITY FIRST, because a backup's inheritance is priced
    # against the man ahead of him and that number is not his own. One pass, so
    # nothing is computed twice.
    avail = {}
    for pid, byweek in rates.items():
        p = players.get(pid) or {}
        if (p.get("position") or "DEF") in roles.PROJ_OPPORTUNITY:
            avail[pid] = availability(pid, p, byweek, wk)

    baselines = _load_baselines()
    refreshed = {pid: dict(by) for pid, by in baselines.items()}
    for pid, byweek in rates.items():
        p = players.get(pid) or {}
        pos = p.get("position") or "DEF"
        if pos not in roles.PROJ_OPPORTUNITY:
            continue
        own_a = (avail.get(pid) or ({},))[0]
        for w, pts in byweek.items():
            rr = roles.projected_role(pid, p.get("team") or "", pos, week=w)
            lead_a = ((avail.get(rr.get("lead_id")) or ({},))[0]).get(w, 1.0)
            if own_a.get(w, 1.0) >= 0.999 and lead_a >= 0.999:
                refreshed.setdefault(pid, {})[str(w)] = pts

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
        raw = raw_series(pid, pos, p.get("team") or "", rates, p, wk,
                         avail=avail, baselines=baselines, record=rec)
        raw_total = sum(raw.values())
        games_left = len(byweek)
        # Weekly modeled value is the level. Sleeper's season file is archived
        # as evidence, never used to pull this series back toward a stale total.
        series = dict(raw)
        # The per-week figure is published at 3dp, so the total is built from
        # the published figures rather than from their full-precision originals.
        series = {w: round(v, 3) for w, v in series.items()}
        total = sum(weights.get(w, 0.0) * v for w, v in series.items())

        rows[pid] = {
            "player_id": pid, "name": api.player_name(players, pid),
            "pos": pos, "team": p.get("team"),
            "ros": round(total, 2),
            "raw": round(raw_total, 2),
            "value_source": "weekly-model",
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
            "scout_return_min": rec.get("scout_return_min"),
            "scout_return_max": rec.get("scout_return_max"),
            "scout_basis": rec.get("scout_basis"),
            "role_change": rec.get("role_change"),
            "min_avail": round(min((d["a"] for d in rec["by_week"].values()),
                                   default=1.0), 3),
            "by_week": {str(w): {**rec["by_week"][w], "final": series[w]}
                        for w in sorted(series)},
        }
    _write_baselines(refreshed, wk)
    return {"schema": SCHEMA, "computed": time.time(), "week": wk,
            "season": season.SEASON, "weights": weights,
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

def trace(name: str = "", player_id: str | None = None) -> str:
    """The walkthrough behind one player's number.

    BY ID WHEREVER THE CALLER HAS ONE. Names are not unique and the collisions
    are not exotic -- "Josh Allen" is a quarterback and a linebacker, and
    re-resolving a name that a table already resolved picks a different man than
    the row the reader is looking at. find() stays for the CLI, where a name is
    all anybody types.
    """
    d = load()
    if player_id:
        r = (d.get("players") or {}).get(str(player_id))
        if not r:
            return f"no expectation on file for player_id {player_id!r}"
    else:
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
    L.append("[4] THE LEVEL -- weekly modeled value")
    L.append(f"    weekly total       {r['raw']:>8.2f}   "
             f"(availability x role; no season-total calibration)")
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
         f"  {'player':<22}{'pos':<5}{'tm':<5}{'ros':>8}"
         f"{'A min':>7}  source"]
    for r in rows[:top]:
        L.append(f"  {r['name'][:21]:<22}{r['pos']:<5}{str(r['team']):<5}"
                 f"{r['ros']:>8.1f}{r['min_avail']:>7.2f}  "
                 f"{r.get('value_source', 'weekly-model')}")
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
         f"  {'player':<22}{'pos':<5}{'ros.py':>9}{'new':>9}{'delta':>9}  source"]
    for r, o, delta in rows[:top]:
        L.append(f"  {r['name'][:21]:<22}{r['pos']:<5}{o:>9.1f}{r['ros']:>9.1f}"
                 f"{delta:>+9.1f}  {r.get('value_source', 'weekly-model')}")
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
