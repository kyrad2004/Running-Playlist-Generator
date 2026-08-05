#!/usr/bin/env python3
"""Pull real Strava activities and report exactly which fields come back.

This is the Phase 0 Week 1 deliverable: find out what data actually exists
before Phase 2 branches on it. It answers three questions:

  1. How many runs are in the recent window? (cold-start check: 7+ runs, 14+ days)
  2. What share of them have heart rate? (decides how much the no-HR path matters)
  3. What share have cadence, and what's the real step rate? (Phase 3 BPM matching)

    python scripts/inspect_strava.py                # last 60 days
    python scripts/inspect_strava.py --days 30
    python scripts/inspect_strava.py --save         # dump raw JSON for inspection
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

from rpg.activity import RunSummary, coverage, format_pace, is_run, summarize
from rpg.config import REPO_ROOT, ConfigError
from rpg.oauth import OAuthError
from rpg.strava import StravaClient
from rpg.transport import ApiError

SAMPLE_DIR = REPO_ROOT / "data" / "samples"

# Phase 2's data-sufficiency thresholds, checked here so Phase 0 knows whether
# the cold-start path is going to be the common case.
MIN_RUNS = 7
MIN_DAY_SPAN = 14


def print_table(runs: list[RunSummary]) -> None:
    header = f"{'date':<11} {'distance':>9} {'pace/mi':>8} {'pace/km':>8} {'HR':>6} {'spm':>6}  name"
    print(header)
    print("─" * len(header))
    for r in runs:
        date = (r.start_date_local or "")[:10]
        dist = f"{r.distance_miles:.2f} mi" if r.distance_miles else "—"
        hr = f"{r.average_heartrate:.0f}" if r.average_heartrate else "—"
        spm = f"{r.steps_per_minute:.0f}" if r.steps_per_minute else "—"
        name = (r.name or "")[:34]
        print(
            f"{date:<11} {dist:>9} {format_pace(r.pace_per_mile_s):>8} "
            f"{format_pace(r.pace_per_km_s):>8} {hr:>6} {spm:>6}  {name}"
        )


def report_detail_fields(client: StravaClient, run: RunSummary, save: bool) -> None:
    """The list endpoint omits splits and laps. Fetch one detail payload to show
    what only appears there — this trips people up in Phase 1."""
    print(f"\nDetail payload for the most recent run (id {run.id})")
    print("─" * 78)
    try:
        detail = client.activity(run.id)
    except ApiError as exc:
        print(f"  could not fetch detail: {exc}")
        return

    summary_keys = set(run.raw.keys())
    detail_only = sorted(set(detail.keys()) - summary_keys)
    print(f"  fields present ONLY on the detail endpoint: {', '.join(detail_only) or 'none'}")

    splits = detail.get("splits_standard") or []
    if splits:
        print(f"\n  splits_standard ({len(splits)} miles):")
        print(f"    {'#':>2} {'pace/mi':>8} {'HR':>6} {'elev':>7}")
        for split in splits:
            speed = split.get("average_speed")
            pace = format_pace(1609.344 / speed) if speed else "—"
            hr = f"{split['average_heartrate']:.0f}" if split.get("average_heartrate") else "—"
            elev = f"{split.get('elevation_difference', 0):+.0f}m"
            print(f"    {split.get('split', '?'):>2} {pace:>8} {hr:>6} {elev:>7}")
    else:
        print("  no splits_standard on this activity")

    laps = detail.get("laps") or []
    print(f"  laps: {len(laps)}")

    if save:
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        path = SAMPLE_DIR / f"activity_{run.id}_detail.json"
        path.write_text(json.dumps(detail, indent=2))
        print(f"\n  raw detail written to {path.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect real Strava activity data.")
    parser.add_argument("--days", type=int, default=60, help="lookback window (default 60)")
    parser.add_argument("--save", action="store_true", help="write raw JSON to data/samples/")
    parser.add_argument("--limit", type=int, default=25, help="max rows to print")
    args = parser.parse_args()

    try:
        client = StravaClient()
    except (ConfigError, OAuthError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    after = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())

    try:
        activities = client.all_activities(after=after)
    except OAuthError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"Strava request failed: {exc}", file=sys.stderr)
        return 1

    runs = [summarize(a) for a in activities if is_run(a)]
    runs.sort(key=lambda r: r.start_date_local, reverse=True)

    print(f"\nLast {args.days} days: {len(activities)} activities, {len(runs)} runs")
    if client.last_rate_limit:
        print(f"Rate limit: {client.last_rate_limit.describe()}")

    if not runs:
        print("\nNo runs in this window. Try a longer --days, and confirm the OAuth grant "
              "included activity:read_all (private runs are invisible without it).")
        return 1

    print()
    print_table(runs[: args.limit])
    if len(runs) > args.limit:
        print(f"… {len(runs) - args.limit} more")

    # -- field coverage --------------------------------------------------
    cov = coverage(runs)
    print("\nField coverage")
    print("─" * 78)
    print(f"  {'runs':<20}{cov.total_runs:>3}")
    print(f"  {'with heart rate':<20}{cov.with_heartrate:>3}   ({cov.heartrate_pct:.0f}%)")
    print(f"  {'with cadence':<20}{cov.with_cadence:>3}   ({cov.cadence_pct:.0f}%)")
    print(f"  {'with average_speed':<20}{cov.with_speed:>3}   ({cov.speed_pct:.0f}%)")

    if cov.with_cadence:
        spms = [r.steps_per_minute for r in runs if r.steps_per_minute]
        avg_spm = sum(spms) / len(spms)
        print(
            f"\n  mean cadence {avg_spm:.0f} spm "
            f"(Strava reports {avg_spm / 2:.0f} — it counts one leg, so we double it)"
        )

    # -- Phase 2 data-sufficiency preview --------------------------------
    dates = [r.start_date_local[:10] for r in runs if r.start_date_local]
    span_days = 0
    if len(dates) >= 2:
        newest = datetime.fromisoformat(dates[0])
        oldest = datetime.fromisoformat(dates[-1])
        span_days = (newest - oldest).days

    print("\nPhase 2 data-sufficiency check (preview)")
    print("─" * 78)
    runs_ok = cov.total_runs >= MIN_RUNS
    span_ok = span_days >= MIN_DAY_SPAN
    print(f"  {'✓' if runs_ok else '✗'} {cov.total_runs} runs (need {MIN_RUNS}+)")
    print(f"  {'✓' if span_ok else '✗'} {span_days}-day span (need {MIN_DAY_SPAN}+)")
    if runs_ok and span_ok:
        print("  → enough history; the cold-start path won't be your normal case")
    else:
        print("  → cold start applies; the 5K-estimate onboarding fallback matters")

    if cov.heartrate_pct < 50:
        print(
            f"\n  Heads up: only {cov.heartrate_pct:.0f}% of runs have HR, so the "
            "pace-only branch in Phase 2 is the primary path, not the fallback."
        )

    report_detail_fields(client, runs[0], args.save)

    if args.save:
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        path = SAMPLE_DIR / f"activities_{int(time.time())}.json"
        path.write_text(json.dumps(activities, indent=2))
        print(f"  raw summary list written to {path.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
