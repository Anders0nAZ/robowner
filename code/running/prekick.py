"""Put the decision cascade in front of every kickoff, not on a wall clock.

THE PROBLEM THIS SOLVES. Roboner decided at three fixed times a day -- 07:00,
07:00's roster pass and 09:00/16:00 from RobonerLineup -- while the local NFL
model has, since before the season, been taking a capture fifteen minutes
before every kickoff slot. So the freshest data in the system was collected on
schedule and then not read by the thing making the decision. Week 1's Sunday
afternoon block kicks at 13:25 local with the last Roboner run at 09:00: a
lineup set on four-hour-old lines and a four-hour-old injury feed.

WHY NOT POLL. Kickoff times are published weeks ahead, so there is nothing to
discover by checking every few minutes -- that is thousands of runs a week to
fire about six times, each paying a full import. This runs once a day and
registers a one-shot per remaining slot, which is the same design the model's
archive_projections.plan_day() already uses and for the same reason.

WHY NOT JUST CALL THIS from RobonerModelCaptureNow.bat. Three lines would have
done it, and the dependency direction already exists. But the cascade runs its
own capture and export as steps, so it would capture twice within seconds, and
CaptureNow.bat is the one job in either project where a missed window cannot be
recovered. Roboner's decision cadence belongs to Roboner.

WHAT A CLOSER RUN ACTUALLY BUYS. Start/sit, and the data underneath it.
moves.ROS_MOVE_BLACKOUT_H holds ros, fill and stream for six hours before a
kickoff no matter who invokes them, so a T-10 run makes no ordinary roster move
-- by design. `patch` is exempt, so an unfillable starting slot still gets
fixed, which is the emergency the hour justifies.

  python -m robo.prekick --plan-day             # what it would register
  python -m robo.prekick --plan-day --install   # register it
  python -m robo.prekick --list                 # what is registered now
"""

import argparse
import datetime as dt
import subprocess

from robo import ROOT, season, settings, vegas

# Games kicking within this of each other are ONE slot, so a Sunday afternoon
# at 16:05 and 16:25 is a single run. The slot is timed off its EARLIEST game,
# so every game in it is still pre-kickoff when the cascade fires.
SLOT_WINDOW_MIN = 30

# Minutes before the slot to run. Ten rather than the model's fifteen, and
# deliberately behind it: both jobs capture and export, and firing them together
# would have them contend for the same work. The cascade takes about twenty
# seconds, so this still clears Sleeper's lock by a wide margin.
LEAD_MIN = 10

# Its own runner rather than RobonerLineupSilent.vbs, which it briefly shared.
# The two are no longer the same run: this one passes --pregame, which skips
# scout because nothing it produces can reach a decision ten minutes before a
# kickoff. Sharing a runner was right only while the runs were identical.
RUNNER = ROOT / "RobonerPreKickSilent.vbs"

# schtasks defaults /SC ONCE to PT72H, which would leave a wedged pregame run
# sitting for three days and overlapping every slot after it. Generous against a
# run that takes about fifteen seconds, and still clears well before the next
# slot could fire.
ONESHOT_LIMIT = "PT15M"

PREFIX = "RobonerPreKick_"

settings.apply(__name__, globals())


def slots(season_yr, week: int, window_min: int = SLOT_WINDOW_MIN) -> list:
    """Distinct kickoff slots for one week, as epoch seconds, oldest first."""
    out: list = []
    for k in vegas.kickoffs(season_yr, week):
        if not out or (k - out[-1][-1]) > window_min * 60:
            out.append([k])
        else:
            out[-1].append(k)
    return [c[0] for c in out]


def registered() -> list:
    """Every one-shot of ours currently in Task Scheduler, by name."""
    r = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    names = []
    for row in (r.stdout or "").splitlines():
        name = row.split(",")[0].strip().strip('"').lstrip("\\")
        if name.startswith(PREFIX):
            names.append(name)
    return sorted(set(names))


def sweep(install: bool) -> int:
    """Remove one-shots left over from previous days.

    THEY CANNOT DELETE THEMSELVES. schtasks /Z needs an EndBoundary that
    /SC ONCE does not provide, so a /Z one-shot does not merely fail to clean up
    -- it never registers at all, silently. Sweeping at the top of each daily
    run is what makes this self-healing: a day the machine was off leaves stale
    entries and the next run clears them.

    The daily planner is RobonerPreKickDaily and carries no trailing underscore,
    so it can never match this prefix and delete itself.
    """
    gone = 0
    for name in registered():
        if install:
            subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                           capture_output=True, text=True)
        gone += 1
    return gone


def _cap_runtime(name: str) -> bool:
    """Give a one-shot a runtime limit. schtasks cannot, PowerShell can.

    /SC ONCE has no flag for ExecutionTimeLimit and defaults it to PT72H, so a
    wedged run would hold for three days and overlap every slot behind it. Done
    as a second step rather than by building task XML, because the XML route
    means owning the whole definition to change one field.
    """
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$t = Get-ScheduledTask -TaskName '{name}'; "
         f"$t.Settings.ExecutionTimeLimit = '{ONESHOT_LIMIT}'; "
         "Set-ScheduledTask -InputObject $t | Out-Null"],
        capture_output=True, text=True)
    return r.returncode == 0


def plan_day(season_yr=None, week: int | None = None, lead_min: int = LEAD_MIN,
             install: bool = False, now: float | None = None) -> list:
    """Queue a cascade run before each of today's remaining kickoff slots."""
    season_yr = season_yr or season.SEASON
    week = week if week is not None else season.current_week()

    swept = sweep(install)
    if swept:
        print(f"swept {swept} stale one-shot task(s)")

    now_dt = (dt.datetime.fromtimestamp(now) if now is not None
              else dt.datetime.now()).astimezone()
    today = now_dt.date()

    found = slots(season_yr, week)
    if not found:
        print(f"no readable kickoff times for {season_yr} week {week} -- "
              "nothing queued")
        return []

    queued = []
    for slot in found:
        local = dt.datetime.fromtimestamp(slot).astimezone()
        fire = local - dt.timedelta(minutes=lead_min)
        if fire <= now_dt or fire.date() != today:
            continue              # already past, or not today's problem
        queued.append((f"{PREFIX}{fire:%Y%m%d_%H%M}", fire, local))

    print(f"\n{len(queued)} kickoff slot(s) left today "
          f"(week {week}, lead {lead_min} min, local time):")
    for name, fire, local in queued:
        print(f"  {fire:%H:%M} local  ->  slot {local:%H:%M}   {name}")
        if not install:
            continue
        r = subprocess.run(
            ["schtasks", "/Create", "/SC", "ONCE", "/TN", name,
             "/TR", f'wscript.exe "{RUNNER}"', "/ST", f"{fire:%H:%M}", "/F"],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("    FAILED: " + (r.stderr or r.stdout).strip()[:160])
            continue
        print("    registered" + ("" if _cap_runtime(name) else
                                  " (WARNING: runtime limit not set)"))
    if queued and not install:
        print("  (dry run -- pass --install to register these)")
    return queued


def report(season_yr=None, week: int | None = None) -> str:
    """Every slot this week and whether a run is queued in front of it."""
    season_yr = season_yr or season.SEASON
    week = week if week is not None else season.current_week()
    have = set(registered())
    L = [f"PRE-KICKOFF RUNS - {season_yr} week {week}, lead {LEAD_MIN} min",
         f"  runner: {RUNNER.name}", ""]
    found = slots(season_yr, week)
    if not found:
        return "\n".join(L + ["  schedule unreadable -- no slots"])
    now = dt.datetime.now().astimezone()
    for slot in found:
        local = dt.datetime.fromtimestamp(slot).astimezone()
        fire = local - dt.timedelta(minutes=LEAD_MIN)
        name = f"{PREFIX}{fire:%Y%m%d_%H%M}"
        # Order matters: a slot later TODAY that is simply not registered yet
        # reads as "not queued", and only a different date is "later day".
        # Testing the date last called tonight's game tomorrow's problem.
        if name in have:
            mark = "queued"
        elif fire < now:
            mark = "past"
        elif fire.date() != now.date():
            mark = "later day"
        else:
            mark = "NOT QUEUED"
        L.append(f"  slot {local:%a %d %b %H:%M}   fire {fire:%H:%M}   {mark}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="queue a cascade run before each kickoff")
    ap.add_argument("--plan-day", action="store_true",
                    help="queue one-shots for today's remaining slots")
    ap.add_argument("--install", action="store_true",
                    help="actually register them (default is a dry run)")
    ap.add_argument("--list", action="store_true", help="this week's slots and their state")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--lead", type=int, default=LEAD_MIN)
    a = ap.parse_args()
    if a.list:
        print(report(week=a.week))
    elif a.plan_day:
        plan_day(week=a.week, lead_min=a.lead, install=a.install)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
