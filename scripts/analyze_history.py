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

# Two sensors on the same run normally land within a few bpm of each other.
HR_CONFLICT_BPM = 5


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

    conflicts = [(g, g.heartrate_conflict) for g in groups if g.heartrate_conflict]
    material = [(g, gap) for g, gap in conflicts if gap >= HR_CONFLICT_BPM]
    if material:
        print(f"\n  Heart-rate disagreements ({len(material)}):")
        for group, gap in material:
            readings = sorted(r.average_heartrate for r in group.runs if r.average_heartrate)
            kept = group.primary().average_heartrate
            print(f"    {group.date}  {' vs '.join(f'{v:.0f}' for v in readings)} bpm "
                  f"(gap {gap:.0f}) — merge keeps {kept:.0f}")
        print("    Two sensors on one run disagreeing this much means one is wrong.")
        print("    Worth deciding which device you trust before HR feeds a zone calc.")


def report_blocks(runs, today) -> None:
    """Consistent stretches, which is where the engine can actually be tested."""
    from rpg.blocks import best_block, find_training_blocks

    blocks = find_training_blocks(runs)
    print("\nConsistent training blocks")
    print("─" * 78)
    if not blocks:
        print("  None found (needs 2+ runs/week for 3+ consecutive weeks).")
        print("  Every window in this history is too sparse to evaluate against.")
        return

    top = best_block(blocks)
    for block in blocks:
        marker = " ←" if block is top else ""
        age = block.age_days(today)
        print(f"  {block.describe()}{marker}")
        print(f"      longest run {block.longest_run_miles:.2f} mi, ended {age} days ago")

    if top and top.midpoint():
        print(f"\n  Largest block marked ←. To ask what the engine would have said")
        print(f"  during it, when there was real training behind the answer:")
        print(f"\n    python scripts/vdot_report.py --as-of {top.midpoint()}")


def report_workouts(runs, client, detail_budget: int) -> None:
    """Classify sessions, so interval and hill averages stop poisoning the maths."""
    from rpg.workout import NOT_STEADY, annotate_all

    details: dict[int, dict] = {}
    if detail_budget:
        # Prefer runs carrying cadence: those are the ones feeding the model
        # that a misclassification would distort.
        ordered = sorted(runs, key=lambda r: (not r.has_cadence, r.start_date_local or ""),
                         reverse=False)
        targets = [r for r in ordered if r.has_cadence][:detail_budget]
        targets += [r for r in ordered if not r.has_cadence][: detail_budget - len(targets)]
        print(f"\nFetching detail for {len(targets)} run(s) to measure split variance…")
        for n, run in enumerate(targets, start=1):
            try:
                details[run.id] = client.activity(run.id)
            except ApiError as exc:
                print(f"  stopped after {n - 1}: {exc}")
                break
        if client.last_rate_limit:
            print(f"  {client.last_rate_limit.describe()}")

    counts = annotate_all(runs, details)

    print("\nWorkout types")
    print("─" * 78)
    for kind in sorted(counts, key=lambda k: -counts[k]):
        excluded = " (excluded from VDOT and cadence fit)" if kind in NOT_STEADY else ""
        source = "measured" if details else "from names only"
        print(f"  {kind:<10} {counts[kind]:>3}{excluded}")
    if not details:
        print("\n  Classified from activity names alone. Many runs are called")
        print("  'Afternoon Run', which says nothing — pass --detail N to measure")
        print("  split variance instead of guessing.")


def report_cadence_fit(runs) -> None:
    """The fitted model, and whether it survives losing its extreme point."""
    from rpg.cadence import fit_cadence_model

    model = fit_cadence_model(runs)
    print("\nCadence model (steady runs only)")
    print("─" * 78)
    if model is None:
        print("  No usable fit — too few steady runs with cadence, or the")
        print("  relationship is too weak to justify a slope. Using a constant.")
        return

    print(f"  {model.describe()}")
    print(f"  explains {model.explains * 100:.0f}% of cadence variation")
    note = model.fragility_note()
    if note:
        print(f"\n  ⚠ FRAGILE: {note}")
    else:
        print("  Stable: the fit survives dropping its fastest run.")


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

    # Apply the same exclusions vdot_report uses, or the two scripts name
    # different anchors for the same history.
    from rpg.workout import is_steady

    candidates = [
        r
        for r in runs
        if r.distance_miles
        and r.distance_miles >= 3.0
        and is_steady(r)
        and not r.raw.get("_distance_conflict")
    ]
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


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation coefficient, no numpy. None when it isn't defined."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    numerator = sum(a * b for a, b in zip(dx, dy))
    denominator = (sum(a * a for a in dx) * sum(b * b for b in dy)) ** 0.5
    return numerator / denominator if denominator else None


def _stdev(values: list[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / n) ** 0.5


def report_cadence_model(runs) -> None:
    """Cadence-vs-pace pairs are the calibration data for Phase 3.

    The question this answers is whether target cadence needs to be a function
    of pace at all. If cadence doesn't move with pace across the range actually
    trained, a single constant is the better model — fewer parameters fitted to
    the same data.
    """
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
    mean_spm = sum(spms) / len(spms)
    spread = max(paces) - min(paces)

    print(f"\n  cadence  {min(spms):.0f}–{max(spms):.0f} spm, "
          f"mean {mean_spm:.0f}, sd {_stdev(spms):.1f}")
    print(f"  pace     {format_pace(min(paces))}–{format_pace(max(paces))}/mi "
          f"({spread / 60:.1f} min/mi spread)")

    r = _pearson(paces, spms)
    if r is None:
        print("\n  Not enough points to test whether cadence tracks pace.")
        return

    print(f"  correlation (pace vs cadence)  r = {r:+.2f}")

    if spread < 60:
        print("\n  ⚠ Paces too clustered to trust the correlation either way.")
        print("    Treat cadence as constant and revisit if the range widens.")
    elif abs(r) < 0.4:
        print(f"\n  → Cadence does NOT track pace across the range you train at.")
        print(f"    Use a single target of {mean_spm:.0f} spm rather than fitting a")
        print("    slope: one parameter, and the data doesn't support two.")
        print(f"    Music target: {mean_spm:.0f} BPM, or {mean_spm / 2:.0f} BPM at half-time.")
    else:
        print(f"\n  → Cadence does track pace (r = {r:+.2f}). A linear")
        print("    pace→cadence model is justified; fit it in Phase 3.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze deduplicated Strava history.")
    parser.add_argument("--days", type=int, default=365,
                        help="lookback window; use 730+ to reach blocks over a year old")
    parser.add_argument("--show-duplicates", action="store_true", help="list every group")
    parser.add_argument(
        "--detail",
        type=int,
        default=0,
        metavar="N",
        help="fetch the detail payload for up to N runs to classify workouts by "
        "split variance rather than by name. Costs one read per run against a "
        "100-per-15-minute limit, so start small.",
    )
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
    report_workouts(runs, client, args.detail)
    report_timeline(runs)
    report_blocks(runs, datetime.now(timezone.utc).date())
    report_recency(runs)
    report_fitness_anchor(runs)
    report_cadence_model(runs)
    report_cadence_fit(runs)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
