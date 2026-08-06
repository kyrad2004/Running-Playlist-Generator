#!/usr/bin/env python3
"""Deduplicate Strava history and report what the data can actually support.

inspect_strava.py answers "what fields come back?". This answers "what does my
training history actually say?" — after collapsing dual-recorded runs, which
otherwise double every volume number.

    python scripts/analyze_history.py --days 365
    python scripts/analyze_history.py --days 365 --show-duplicates
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401

from rpg.activity import coverage, format_pace, is_run, summarize
from rpg.config import ConfigError
from rpg.dedupe import dedupe
from rpg.oauth import OAuthError
from rpg.strava import StravaClient
from rpg.transport import ApiError

# Past this, a fitness estimate from an old race describes who you were, not
# who you are. Prescribing paces off a stale benchmark is an injury risk.
STALE_AFTER_DAYS = 90


def report_duplicates(groups, verbose: bool) -> None:
    if not groups:
        print("\nNo duplicate recordings detected.")
        return

    high = sum(1 for g in groups if g.high_confidence)
    print(f"\nDuplicate recordings: {len(groups)} group(s) — {high} near-exact, "
          f"{len(groups) - high} looser matches")
    print("─" * 78)
    print("  Same run captured by two apps at once. Left in, these double your")
    print("  weekly volume and distort any easy/hard split.\n")

    shown = groups if verbose else groups[:5]
    for group in shown:
        marker = "  " if group.high_confidence else " ?"
        print(f"{marker} {group.date}   gap {group.distance_gap * 100:.1f}%")
        primary = group.primary()
        for run in group.runs:
            keep = "keep " if run is primary else "drop "
            hr = f"{run.average_heartrate:.0f}" if run.average_heartrate else "—"
            spm = f"{run.steps_per_minute:.0f}" if run.steps_per_minute else "—"
            dist = f"{run.distance_miles:.2f}mi" if run.distance_miles else "—"
            print(f"       {keep} {dist:>8}  HR {hr:>4}  spm {spm:>4}  {(run.name or '')[:38]}")
    if not verbose and len(groups) > 5:
        print(f"\n  … {len(groups) - 5} more (--show-duplicates for all)")
    print("\n  '?' marks a looser match worth eyeballing — one app may have")
    print("  recorded a warmup the other missed, or they may be separate runs.")


def report_timeline(runs) -> None:
    by_month: dict[str, list] = defaultdict(list)
    for run in runs:
        by_month[(run.start_date_local or "")[:7]].append(run)

    print("\nTraining timeline")
    print("─" * 78)
    print(f"  {'month':<9} {'runs':>5} {'miles':>8}   {'':<24}")
    peak = max((sum(r.distance_miles or 0 for r in rs) for rs in by_month.values()), default=0)
    for month in sorted(by_month, reverse=True):
        month_runs = by_month[month]
        miles = sum(r.distance_miles or 0 for r in month_runs)
        bar = "█" * int(round(20 * miles / peak)) if peak else ""
        print(f"  {month:<9} {len(month_runs):>5} {miles:>8.1f}   {bar}")


def report_fitness_anchor(runs) -> None:
    """The single best effort available for a VDOT estimate, and its age."""
    print("\nFitness anchor for VDOT")
    print("─" * 78)

    candidates = [r for r in runs if r.distance_miles and r.distance_miles >= 3.0]
    if not candidates:
        print("  No run of 3+ miles — VDOT from history isn't viable yet.")
        print("  → Use the onboarding estimate (ask for a recent 5K time).")
        return

    # Longest run at the fastest pace is a better proxy for a maximal effort
    # than either alone; a long race beats a short easy run.
    best = min(candidates, key=lambda r: (r.pace_per_mile_s or 9e9) / max(r.distance_miles, 1) ** 0.1)
    date = (best.start_date_local or "")[:10]
    age_days = (datetime.now(timezone.utc).date() - datetime.fromisoformat(date).date()).days

    print(f"  {date}  {best.distance_miles:.2f} mi @ {format_pace(best.pace_per_mile_s)}/mi"
          f"   {(best.name or '')[:34]}")
    print(f"  age: {age_days} days")

    if age_days > STALE_AFTER_DAYS:
        print(f"\n  ⚠ This is more than {STALE_AFTER_DAYS} days old. Fitness decays;")
        print("    prescribing paces from it would target who you were, not who")
        print("    you are. The engine needs a staleness rule, not just a max.")


def report_recency(runs) -> None:
    today = datetime.now(timezone.utc).date()
    windows = {"last 30d": 30, "last 90d": 90, "last 365d": 365}

    print("\nRecent volume")
    print("─" * 78)
    for label, days in windows.items():
        cutoff = today - timedelta(days=days)
        recent = [
            r for r in runs
            if r.start_date_local
            and datetime.fromisoformat(r.start_date_local[:10]).date() >= cutoff
        ]
        miles = sum(r.distance_miles or 0 for r in recent)
        per_week = miles / (days / 7)
        print(f"  {label:<10} {len(recent):>3} runs  {miles:>7.1f} mi  ({per_week:.1f} mi/wk)")

    if runs:
        last = (runs[0].start_date_local or "")[:10]
        gap = (today - datetime.fromisoformat(last).date()).days
        print(f"\n  last run: {last} ({gap} days ago)")


def report_cadence_model(runs) -> None:
    """Cadence-vs-pace pairs are the calibration data for Phase 3."""
    with_cadence = [r for r in runs if r.steps_per_minute and r.pace_per_mile_s]
    print("\nCadence calibration data (Phase 3)")
    print("─" * 78)
    if not with_cadence:
        print("  No runs with cadence — the pace→cadence model has nothing to fit.")
        print("  → Count your steps for 30s on one run and supply it as a constant.")
        return

    with_cadence.sort(key=lambda r: r.pace_per_mile_s)
    print(f"  {len(with_cadence)} run(s) carry cadence:\n")
    print(f"    {'pace/mi':>8} {'spm':>6}   date")
    for run in with_cadence:
        print(f"    {format_pace(run.pace_per_mile_s):>8} {run.steps_per_minute:>6.0f}"
              f"   {(run.start_date_local or '')[:10]}")

    spms = [r.steps_per_minute for r in with_cadence]
    paces = [r.pace_per_mile_s for r in with_cadence]
    print(f"\n  cadence range {min(spms):.0f}–{max(spms):.0f} spm "
          f"across {format_pace(min(paces))}–{format_pace(max(paces))}/mi")
    if max(paces) - min(paces) < 60:
        print("  ⚠ These paces are too clustered to fit a slope. Treat cadence as")
        print("    roughly constant for now, and revisit if faster runs get recorded.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze deduplicated Strava history.")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--show-duplicates", action="store_true", help="list every group")
    args = parser.parse_args()

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

    raw_runs = [summarize(a) for a in activities if is_run(a)]
    raw_runs.sort(key=lambda r: r.start_date_local or "", reverse=True)
    if not raw_runs:
        print("No runs in this window.")
        return 1

    runs, groups = dedupe(raw_runs)

    before, after_cov = coverage(raw_runs), coverage(runs)
    print(f"\nLast {args.days} days")
    print("─" * 78)
    print(f"  {len(raw_runs)} run activities → {len(runs)} distinct runs "
          f"({len(raw_runs) - len(runs)} duplicates collapsed)")
    print(f"\n  {'':<18} {'as recorded':>12} {'deduplicated':>14}")
    print(f"  {'heart rate':<18} {before.heartrate_pct:>11.0f}% {after_cov.heartrate_pct:>13.0f}%")
    print(f"  {'cadence':<18} {before.cadence_pct:>11.0f}% {after_cov.cadence_pct:>13.0f}%")

    report_duplicates(groups, args.show_duplicates)
    report_timeline(runs)
    report_recency(runs)
    report_fitness_anchor(runs)
    report_cadence_model(runs)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
