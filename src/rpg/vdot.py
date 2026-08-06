"""VDOT: Daniels & Gilbert's running fitness model.

Two empirical curves do the work.

1. Oxygen cost of running at a velocity (ml/kg/min for v in m/min):

       VO2 = -4.60 + 0.182258·v + 0.000104·v²

2. The fraction of VO2max sustainable for a duration (t in minutes):

       %VO2max = 0.8 + 0.1894393·e^(-0.012778·t) + 0.2989558·e^(-0.1932605·t)

VDOT is the first divided by the second: the VO2max implied by having held a
given pace for a given time. It is a *pseudo* VO2max — derived from performance
rather than measured in a lab — which is the point, since it can be computed
from any hard effort.

Inverting curve 1 turns a VDOT back into training paces, and iterating both
turns it into race predictions.

Validated against published Daniels tables in tests/test_vdot.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from .activity import METERS_PER_MILE, RunSummary
from .workout import is_steady

# Curve 1 coefficients — oxygen cost of running.
_VO2_C = -4.60
_VO2_B = 0.182258
_VO2_A = 0.000104

# Curve 2 coefficients — sustainable fraction of VO2max by duration.
_FRAC_BASE = 0.8
_FRAC_SLOW_COEF, _FRAC_SLOW_RATE = 0.1894393, -0.012778
_FRAC_FAST_COEF, _FRAC_FAST_RATE = 0.2989558, -0.1932605

STANDARD_DISTANCES: dict[str, float] = {
    "1 mile": METERS_PER_MILE,
    "5K": 5000.0,
    "10K": 10000.0,
    "half marathon": 21097.5,
    "marathon": 42195.0,
}

# Below this a GPS wobble dominates the measurement, and the effort is too short
# for curve 2 to be well behaved.
MIN_EFFORT_METERS = 1500.0

# Daniels expresses each training zone as a band of %VO2max. Easy is genuinely a
# range; the exact bounds differ between editions of the book, so treat these as
# the centre of the published spread rather than canon.
INTENSITY = {
    "easy_slow": 0.63,
    "easy_fast": 0.72,
    "marathon": 0.84,
    "threshold": 0.88,
    "interval": 0.98,
    "repetition": 1.05,
}


class VdotError(ValueError):
    """Raised when an effort can't produce a meaningful VDOT."""


# ---------------------------------------------------------------------------
# The two curves, and their inverses
# ---------------------------------------------------------------------------


def vo2_at_velocity(meters_per_minute: float) -> float:
    """Oxygen cost of running at this velocity."""
    v = meters_per_minute
    return _VO2_C + _VO2_B * v + _VO2_A * v * v


def velocity_at_vo2(vo2: float) -> float:
    """Inverse of `vo2_at_velocity` — the positive root of the quadratic."""
    discriminant = _VO2_B**2 - 4 * _VO2_A * (_VO2_C - vo2)
    if discriminant < 0:
        raise VdotError(f"No real velocity for VO2 {vo2}")
    return (-_VO2_B + math.sqrt(discriminant)) / (2 * _VO2_A)


def fraction_of_vo2max(minutes: float) -> float:
    """The share of VO2max sustainable for this duration.

    Near 1.0 for efforts around 6 minutes, decaying toward 0.8 over hours.
    """
    if minutes <= 0:
        raise VdotError("Duration must be positive")
    return (
        _FRAC_BASE
        + _FRAC_SLOW_COEF * math.exp(_FRAC_SLOW_RATE * minutes)
        + _FRAC_FAST_COEF * math.exp(_FRAC_FAST_RATE * minutes)
    )


def vdot_from_effort(meters: float, seconds: float) -> float:
    """VDOT implied by covering `meters` in `seconds`.

    Assumes a maximal effort. A steady easy run run through this returns a
    number well below true fitness — which is exactly why `estimate_from_runs`
    takes a maximum rather than an average.
    """
    if meters <= 0 or seconds <= 0:
        raise VdotError("Distance and time must both be positive")
    minutes = seconds / 60.0
    velocity = meters / minutes
    return vo2_at_velocity(velocity) / fraction_of_vo2max(minutes)


def predict_race_time(vdot: float, meters: float) -> float:
    """Seconds to cover `meters` at this VDOT.

    Duration appears on both sides — velocity depends on the sustainable
    fraction, which depends on how long the race takes — so this bisects rather
    than solving directly.
    """
    if vdot <= 0 or meters <= 0:
        raise VdotError("VDOT and distance must both be positive")

    low, high = 1.0, 60.0 * 24.0  # minutes
    for _ in range(200):
        mid = (low + high) / 2
        implied = vo2_at_velocity(meters / mid) / fraction_of_vo2max(mid)
        # Going faster (less time) implies a higher VDOT.
        if implied > vdot:
            low = mid
        else:
            high = mid
    return (low + high) / 2 * 60.0


# ---------------------------------------------------------------------------
# Training paces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingPaces:
    """Seconds per mile for each Daniels training zone."""

    easy_slow: float
    easy_fast: float
    marathon: float
    threshold: float
    interval: float
    repetition: float

    def as_dict(self) -> dict[str, float]:
        return {
            "easy_slow": self.easy_slow,
            "easy_fast": self.easy_fast,
            "marathon": self.marathon,
            "threshold": self.threshold,
            "interval": self.interval,
            "repetition": self.repetition,
        }


def pace_at_intensity(vdot: float, fraction: float) -> float:
    """Seconds per mile at a given fraction of VDOT."""
    velocity = velocity_at_vo2(vdot * fraction)
    return METERS_PER_MILE / velocity * 60.0


def training_paces(vdot: float) -> TrainingPaces:
    if vdot <= 0:
        raise VdotError("VDOT must be positive")
    return TrainingPaces(**{k: pace_at_intensity(vdot, f) for k, f in INTENSITY.items()})


# ---------------------------------------------------------------------------
# Estimating from history, with staleness
# ---------------------------------------------------------------------------

STALE_AFTER_DAYS = 90
MIN_QUALIFYING_EFFORTS = 3

# A short run that scores far above every longer effort is more often a GPS
# artifact or a mis-recorded distance than a genuine breakthrough, so it gets
# flagged rather than silently accepted as the anchor.
OUTLIER_VDOT_GAP = 2.0

# Detraining rule of thumb, not a fitted model. Deliberately gentle: the large
# VO2max losses reported in the literature come from near-total inactivity, and
# an athlete still running at all retains far more than that. An earlier version
# of this used 4% per month capped at 20%, which took a VDOT 34.8 anchor down to
# 28.5 and produced training paces a real runner immediately recognised as
# absurd. Treat it as a floor on plausible fitness, never as a measurement.
DECAY_PER_30_DAYS = 0.015
MAX_DECAY = 0.08


@dataclass
class Effort:
    """One run, scored."""

    run: RunSummary
    vdot: float

    @property
    def run_date(self) -> date | None:
        raw = (self.run.start_date_local or "")[:10]
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None

    def age_days(self, today: date) -> int | None:
        run_date = self.run_date
        return (today - run_date).days if run_date else None


@dataclass
class VdotEstimate:
    """A VDOT estimate plus everything needed to decide whether to trust it."""

    vdot: float | None
    best: Effort | None
    efforts: list[Effort]
    age_days: int | None
    recent_run_count: int
    recent_miles: float
    confidence: str  # "fresh" | "stale" | "insufficient"
    reason: str
    outlier_warning: str | None = None

    @property
    def usable(self) -> bool:
        return self.confidence == "fresh"

    def effective_vdot(self) -> float | None:
        """The number the report should actually use.

        Stale estimates are shown decayed, so paces, predictions and the
        reality check all have to agree on which value they're describing.
        """
        return self.decayed_vdot() if self.confidence == "stale" else self.vdot

    def decayed_vdot(self) -> float | None:
        """The estimate adjusted for how long ago the effort was.

        A deliberately crude haircut, capped, and only meaningful as an
        illustration of the size of the staleness problem.
        """
        if self.vdot is None or self.age_days is None:
            return None
        if self.age_days <= 30:
            return self.vdot
        decay = min(DECAY_PER_30_DAYS * (self.age_days - 30) / 30.0, MAX_DECAY)
        return self.vdot * (1 - decay)


def implied_vdot_if_pace_were(pace_seconds_per_mile: float, fraction: float) -> float:
    """The VDOT for which this pace sits at a given fraction of VO2max.

    The inverse question to `training_paces`: instead of "what should I run at
    VDOT 40", this asks "if this pace really is my easy pace, how fit am I?"
    """
    if pace_seconds_per_mile <= 0:
        raise VdotError("Pace must be positive")
    velocity = METERS_PER_MILE / (pace_seconds_per_mile / 60.0)
    return vo2_at_velocity(velocity) / fraction


@dataclass
class Plausibility:
    """Whether an estimate survives contact with how the athlete actually runs.

    A VDOT derived from one old effort can be badly wrong. Habitual training
    pace is a second, independent line of evidence: if someone comfortably runs
    every day at a pace the model says is far too hard for them, the model —
    not the runner — is more likely to be mistaken.
    """

    looks_low: bool
    median_recent_pace: float | None
    implied_low: float | None  # if those runs are at the fast end of easy
    implied_high: float | None  # if they're at the slow end of easy
    runs_faster_than_easy: int
    runs_considered: int
    message: str | None


def check_plausibility(
    vdot: float | None,
    runs: Sequence[RunSummary],
    sample: int = 5,
    threshold: float = 0.6,
) -> Plausibility:
    """Compare prescribed easy pace against recent habitual pace."""
    recent = [r for r in runs[:sample] if r.pace_per_mile_s]
    if vdot is None or not recent:
        return Plausibility(False, None, None, None, 0, len(recent), None)

    paces = sorted(r.pace_per_mile_s for r in recent)
    median = paces[len(paces) // 2]
    easy_fast = pace_at_intensity(vdot, INTENSITY["easy_fast"])
    faster = sum(1 for p in paces if p < easy_fast)

    implied_low = implied_vdot_if_pace_were(median, INTENSITY["easy_fast"])
    implied_high = implied_vdot_if_pace_were(median, INTENSITY["easy_slow"])

    looks_low = (faster / len(paces)) >= threshold and implied_low > vdot + 2
    message = None
    if looks_low:
        message = (
            f"{faster} of the last {len(paces)} runs were faster than this VDOT's "
            f"easy pace. Two readings: either those runs are harder than they feel, "
            f"or the estimate is too low. If a median of "
            f"{_mmss(median)}/mi is genuinely conversational, it implies VDOT "
            f"{implied_low:.0f}–{implied_high:.0f}, not {vdot:.1f} — a gap far larger "
            "than the model can resolve from easy runs alone. Re-anchor with a hard "
            "effort you actually raced."
        )
    return Plausibility(
        looks_low, median, implied_low, implied_high, faster, len(paces), message
    )


def _mmss(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def score_efforts(runs: Iterable[RunSummary]) -> list[Effort]:
    """VDOT for every run long enough to be meaningful, best first.

    Two kinds of run are skipped entirely:

    * a distance conflict — VDOT is a function of pace, and a pace computed from
      a distance known to be wrong is not a weak estimate but a fabricated one;
    * a non-steady session — an interval workout's average pace blends hard reps
      with recovery jogs, and a hill session's is dragged down by climbing, so
      neither average describes a sustained effort at any point of the run.
    """
    efforts: list[Effort] = []
    for run in runs:
        if not run.distance_m or not run.moving_time_s:
            continue
        if run.distance_m < MIN_EFFORT_METERS:
            continue
        if run.raw.get("_distance_conflict"):
            continue
        if not is_steady(run):
            continue
        try:
            efforts.append(Effort(run, vdot_from_effort(run.distance_m, run.moving_time_s)))
        except VdotError:
            continue
    efforts.sort(key=lambda e: e.vdot, reverse=True)
    return efforts


def estimate_from_runs(
    runs: Sequence[RunSummary],
    today: date | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
    recent_window_days: int = 30,
    min_recent_runs: int = 4,
) -> VdotEstimate:
    """Best-effort VDOT with an explicit verdict on whether it can be trusted.

    Taking the maximum is deliberate: easy runs score far below true fitness, so
    the hardest recent effort is the closest available proxy for a maximal one.
    The cost is that the maximum tends to be an old race, which is precisely why
    staleness has to be reported alongside it.
    """
    today = today or datetime.now(timezone.utc).date()
    efforts = score_efforts(runs)

    recent = [
        e for e in efforts
        if (age := e.age_days(today)) is not None and age <= recent_window_days
    ]
    recent_miles = sum(e.run.distance_miles or 0 for e in recent)

    if len(efforts) < MIN_QUALIFYING_EFFORTS:
        return VdotEstimate(
            vdot=None,
            best=None,
            efforts=efforts,
            age_days=None,
            recent_run_count=len(recent),
            recent_miles=recent_miles,
            confidence="insufficient",
            reason=(
                f"Only {len(efforts)} run(s) of at least "
                f"{MIN_EFFORT_METERS / 1000:.1f} km — need {MIN_QUALIFYING_EFFORTS}. "
                "Use the onboarding estimate instead."
            ),
        )

    best = efforts[0]
    age = best.age_days(today)

    # Is the top effort a short run beating every longer one? That pattern is
    # usually a bad distance reading rather than real fitness.
    outlier_warning = None
    if len(efforts) > 1:
        runner_up = efforts[1]
        gap = best.vdot - runner_up.vdot
        best_distance = best.run.distance_m or 0
        runner_up_distance = runner_up.run.distance_m or 0
        if gap > OUTLIER_VDOT_GAP and best_distance < runner_up_distance:
            outlier_warning = (
                f"Top effort ({best.run.distance_miles:.2f} mi, VDOT {best.vdot:.1f}) "
                f"outscores the next best ({runner_up.run.distance_miles:.2f} mi, "
                f"VDOT {runner_up.vdot:.1f}) by {gap:.1f} despite being shorter. "
                "Short efforts are the ones GPS gets wrong — check that distance "
                "before treating this as the anchor."
            )

    if age is not None and age > stale_after_days:
        reason = (
            f"Best effort is {age} days old (limit {stale_after_days}). "
            f"Only {len(recent)} run(s) and {recent_miles:.1f} mi in the last "
            f"{recent_window_days} days, so there's no evidence that fitness held."
        )
        confidence = "stale"
    elif len(recent) < min_recent_runs:
        reason = (
            f"Effort is recent ({age} days) but only {len(recent)} run(s) in the "
            f"last {recent_window_days} days — too little to confirm it."
        )
        confidence = "stale"
    else:
        reason = (
            f"Best effort {age} days old, with {len(recent)} run(s) and "
            f"{recent_miles:.1f} mi in the last {recent_window_days} days."
        )
        confidence = "fresh"

    return VdotEstimate(
        vdot=best.vdot,
        best=best,
        efforts=efforts,
        age_days=age,
        recent_run_count=len(recent),
        recent_miles=recent_miles,
        confidence=confidence,
        reason=reason,
        outlier_warning=outlier_warning,
    )
