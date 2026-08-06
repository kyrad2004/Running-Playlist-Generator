#!/usr/bin/env python3
"""Compute VDOT from real Strava history and show what it implies.

Week 2's benchmark is judgement, not correctness: the formulas are validated in
tests, but only you can say whether the predicted paces feel right. So this
prints the estimate, its provenance, and — most importantly — how the prescribed
easy pace compares to what you have actually been running.

    python scripts/vdot_report.py --days 365
    python scripts/vdot_report.py --days 365 --vdot 35    # skip Strava, try a number
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401

from rpg.activity import format_pace, is_run, summarize
from rpg.config import ConfigError
from rpg.dedupe import dedupe
from rpg.oauth import OAuthError
from rpg.strava import StravaClient
from rpg.transport import ApiError
from rpg.vdot import (
    STANDARD_DISTANCES,
    estimate_from_runs,
    predict_race_time,
    training_paces,
)

CONFIDENCE_MARK = {"fresh": "\033[32m✓\033[0m", "stale": "\033[33m⚠\033[0m",
                   "insufficient": "\033[31m✗\033[0m"}


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def print_paces(vdot: float, label: str) -> None:
    paces = training_paces(vdot)
    print(f"\nTraining paces at VDOT {vdot:.1f} ({label})")
    print("─" * 78)
    rows = [
        # Fast bound first — easy_slow is the larger number of seconds, so
        # printing it first reads as an inverted range.
        ("Easy", f"{format_pace(paces.easy_fast)}–{format_pace(paces.easy_slow)}",
         "conversational; most of your weekly volume"),
        ("Marathon", format_pace(paces.marathon), "steady, sustainable for hours"),
        ("Threshold", format_pace(paces.threshold), "comfortably hard; ~1 hour race effort"),
        ("Interval", format_pace(paces.interval), "hard; 3–5 min repeats"),
        ("Repetition", format_pace(paces.repetition), "fast; short reps with full recovery"),
    ]
    for name, pace, note in rows:
        print(f"  {name:<12} {pace:>13} /mi   {note}")


def print_predictions(vdot: float) -> None:
    print(f"\nRace predictions at VDOT {vdot:.1f}")
    print("─" * 78)
    for name, meters in STANDARD_DISTANCES.items():
        seconds = predict_race_time(vdot, meters)
        per_mile = seconds / (meters / 1609.344)
        print(f"  {name:<16} {format_duration(seconds):>9}   ({format_pace(per_mile)}/mi)")


def print_reality_check(estimate, runs) -> None:
    """The part that decides whether the engine is worth trusting.

    If prescribed easy pace is far slower than what's actually being run, the
    80/20 logic in Phase 2 has something real to say.
    """
    vdot = estimate.effective_vdot()
    if vdot is None:
        return
    recent = runs[:5]
    if not recent:
        return

    paces = training_paces(vdot)
    print("\nReality check — prescribed easy pace vs. what you actually ran")
    print("─" * 78)
    print(f"  easy pace at VDOT {vdot:.1f}: "
          f"{format_pace(paces.easy_fast)}–{format_pace(paces.easy_slow)}/mi\n")

    faster_than_easy = 0
    for run in recent:
        if not run.pace_per_mile_s:
            continue
        actual = run.pace_per_mile_s
        if actual < paces.easy_fast:
            verdict, delta = "faster than easy", paces.easy_fast - actual
            faster_than_easy += 1
            note = f"  ({format_pace(delta)}/mi too fast)"
        elif actual > paces.easy_slow:
            verdict, note = "slower than easy", ""
        else:
            verdict, note = "in the easy band", ""
        print(f"  {(run.start_date_local or '')[:10]}  {run.distance_miles:>5.2f} mi @ "
              f"{format_pace(actual)}/mi   {verdict}{note}")

    if faster_than_easy >= 3:
        print(f"\n  {faster_than_easy} of your last {len(recent)} runs were faster than easy pace.")
        print("  Short runs at close to race effort with no easy volume underneath is")
        print("  exactly the pattern the 80/20 rule in Phase 2 exists to catch.")
        print("  Whether you trust that verdict is the Week 2 benchmark.")


def main() -> int:
    parser = argparse.ArgumentParser(description="VDOT from Strava history.")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--vdot", type=float, help="skip Strava and use this VDOT")
    parser.add_argument("--top", type=int, default=8, help="how many scored efforts to list")
    args = parser.parse_args()

    if args.vdot:
        print_paces(args.vdot, "supplied")
        print_predictions(args.vdot)
        print()
        return 0

    try:
        client = StravaClient()
    except (ConfigError, OAuthError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    after = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())
    try:
        activities = client.all_activities(after=after)
    except (ApiError, OAuthError) as exc:
        print(f"Strava request failed: {exc}", file=sys.stderr)
        return 1

    raw = [summarize(a) for a in activities if is_run(a)]
    raw.sort(key=lambda r: r.start_date_local or "", reverse=True)
    runs, _ = dedupe(raw)

    estimate = estimate_from_runs(runs)

    print(f"\nVDOT estimate — last {args.days} days, {len(runs)} distinct runs")
    print("─" * 78)
    mark = CONFIDENCE_MARK[estimate.confidence]
    if estimate.vdot is None:
        print(f"  {mark} no usable estimate")
        print(f"     {estimate.reason}")
        return 0

    print(f"  {mark} VDOT {estimate.vdot:.1f}   confidence: {estimate.confidence}")
    print(f"     {estimate.reason}")

    if estimate.outlier_warning:
        print(f"\n  ⚠ {estimate.outlier_warning}")

    best = estimate.best
    print(f"\n  from: {(best.run.start_date_local or '')[:10]}  "
          f"{best.run.distance_miles:.2f} mi @ {format_pace(best.run.pace_per_mile_s)}/mi"
          f"   {(best.run.name or '')[:36]}")

    print(f"\n  Top {args.top} scored efforts:")
    print(f"    {'VDOT':>5}  {'date':<11} {'dist':>7} {'pace/mi':>8}   name")
    for effort in estimate.efforts[: args.top]:
        run = effort.run
        print(f"    {effort.vdot:>5.1f}  {(run.start_date_local or '')[:10]:<11} "
              f"{run.distance_miles:>6.2f}mi {format_pace(run.pace_per_mile_s):>8}   "
              f"{(run.name or '')[:32]}")

    if estimate.confidence == "stale":
        decayed = estimate.decayed_vdot()
        print(f"\n  ⚠ Stale. A crude detraining haircut would put you nearer "
              f"VDOT {decayed:.1f}")
        print("    rather than {:.1f}. That gap is the size of the problem — it is a".format(estimate.vdot))
        print("    rule of thumb, not a measurement. The honest move is to run a")
        print("    time trial and re-anchor rather than trust either number.")
        print_paces(decayed, "detraining-adjusted, approximate")
    else:
        print_paces(estimate.vdot, "current")

    print_predictions(estimate.effective_vdot())
    print_reality_check(estimate, runs)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
