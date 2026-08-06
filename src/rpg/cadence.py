"""Matching music tempo to running cadence.

Cadence rises with speed, so the target BPM depends on the pace being
prescribed — which is what makes a playlist "pace-synced" rather than just a
fixed-tempo mix.

An earlier reading of this data concluded the opposite. Ten cadence-carrying
runs clustered inside a 1.8 min/mi band gave r = -0.12, and a constant looked
like the honest model. Widening the history to two years produced 37 runs across
3.9 min/mi — 7:15 to 11:09 — and the relationship appeared: r = +0.61 against
speed. The first conclusion wasn't wrong about its data, it was wrong about how
little of the range that data covered.

Fit quality is moderate, not decisive: speed explains about 37% of cadence
variation, and 3.9 spm of scatter remains. So the model shifts the target
sensibly across zones, but it should never be treated as precise.

The half/double problem that plagues BPM detection is nearly free here. A track
that "feels" like 160 is routinely listed at 80, and normally you'd have to
resolve which is right. But a runner at 160 spm can stride to either — one step
per beat at 160, or one step per half-beat at 80 — so both are accepted and the
ambiguity mostly stops mattering. Which is fortunate, because almost no popular
music sits at 160 BPM; the useful pool is at half time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .activity import METERS_PER_MILE, RunSummary
from .workout import is_steady

# Fallback when there's no cadence history to fit against: the measured mean.
DEFAULT_TARGET_SPM = 160.0

# Scatter around the fitted line is ~3.9 spm, so ±5 is about one standard error.
DEFAULT_TOLERANCE_SPM = 5.0

# Multipliers a runner can actually stride to. 1 is a step per beat, 2 doubles a
# slow track, 0.5 halves a fast one. Beyond that the beat stops being findable.
STRIDE_MULTIPLIERS = (0.5, 1.0, 2.0)


@dataclass(frozen=True)
class CadenceModel:
    """Cadence as a linear function of speed.

    Fitted against speed rather than pace: cadence scales with how fast you're
    moving, and pace is its reciprocal, so a straight line fits speed better
    (r² 0.37 vs 0.29 on the reference data).
    """

    intercept: float  # spm
    slope: float  # spm per m/min
    r: float
    n: int
    residual_sd: float
    min_speed: float  # bounds of the fitted data, in m/min
    max_speed: float
    # Refit with the fastest run removed. A cadence-vs-speed fit is dominated by
    # its extreme point, and the fastest run in a recreational history is often
    # an interval session — whose *average* pace blends hard reps with recovery
    # jogs and doesn't describe a steady effort at all.
    r_without_extreme: float | None = None
    slope_without_extreme: float | None = None

    @property
    def explains(self) -> float:
        """Share of cadence variation the model accounts for."""
        return self.r**2

    @property
    def is_fragile(self) -> bool:
        """True when one point is carrying the relationship.

        Either the correlation collapses below the useful threshold without it,
        or the slope moves by more than a third — both mean the model describes
        that run more than it describes the athlete.
        """
        if self.r_without_extreme is None or self.slope_without_extreme is None:
            return False
        if abs(self.r_without_extreme) < MIN_USEFUL_R:
            return True
        if self.slope == 0:
            return True
        return abs(self.slope_without_extreme - self.slope) / abs(self.slope) > 0.33

    def fragility_note(self) -> str | None:
        if not self.is_fragile:
            return None
        return (
            f"Dropping the single fastest run takes r from {self.r:+.2f} to "
            f"{self.r_without_extreme:+.2f} and the slope from {self.slope:.3f} to "
            f"{self.slope_without_extreme:.3f}. The relationship rests on one point. "
            "If that run was an interval session, its average pace mixes reps with "
            "recoveries and is not a steady effort — use its laps instead."
        )

    def cadence_at_speed(self, meters_per_minute: float) -> float:
        """Predicted cadence, clamped to the speeds actually observed.

        Extrapolating past the data invents a number: nothing here says what
        happens at 6:00/mi if no run was ever that fast.
        """
        clamped = max(self.min_speed, min(self.max_speed, meters_per_minute))
        return self.intercept + self.slope * clamped

    def cadence_at_pace(self, seconds_per_mile: float) -> float:
        if seconds_per_mile <= 0:
            raise ValueError("Pace must be positive")
        return self.cadence_at_speed(METERS_PER_MILE / (seconds_per_mile / 60.0))

    def describe(self) -> str:
        return (
            f"cadence = {self.intercept:.1f} + {self.slope:.4f} x speed(m/min), "
            f"r={self.r:+.2f} over n={self.n}, ±{self.residual_sd:.1f} spm"
        )


# Fitted from 37 cadence-carrying runs spanning 7:15-11:09/mi in a real account.
# Replaced by fit_cadence_model() as soon as there's history to fit against.
REFERENCE_MODEL = CadenceModel(
    intercept=131.88,
    slope=0.17438,
    r=0.605,
    n=37,
    residual_sd=3.86,
    min_speed=METERS_PER_MILE / (11 * 60 + 9) * 60,
    max_speed=METERS_PER_MILE / (7 * 60 + 15) * 60,
)

# Below this the relationship isn't established well enough to prefer over a
# constant — r² under ~0.15 means the line barely beats the mean.
MIN_USEFUL_R = 0.4
MIN_FIT_POINTS = 8


def fit_cadence_model(runs: Iterable[RunSummary]) -> CadenceModel | None:
    """Fit cadence against speed from a run history.

    Returns None when there isn't enough data, or when the relationship is too
    weak to justify two parameters instead of one — in which case the caller
    should fall back to a constant target.
    """
    # Non-steady sessions are excluded: pairing an interval workout's average
    # cadence with its average pace relates two numbers that describe different
    # parts of the run.
    points = [
        (r.average_speed_mps * 60.0, r.steps_per_minute)
        for r in runs
        if r.steps_per_minute and r.average_speed_mps and is_steady(r)
    ]
    if len(points) < MIN_FIT_POINTS:
        return None

    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    n = len(points)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]

    sxx = sum(v * v for v in dx)
    syy = sum(v * v for v in dy)
    if sxx == 0 or syy == 0:
        return None

    sxy = sum(a * b for a, b in zip(dx, dy))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    r = sxy / (sxx * syy) ** 0.5

    if abs(r) < MIN_USEFUL_R:
        return None

    residuals = [y - (intercept + slope * x) for x, y in points]
    residual_sd = (sum(v * v for v in residuals) / n) ** 0.5

    # Refit without the fastest point. A cadence-vs-speed line is dominated by
    # its extreme, so if the conclusion depends on one run, say so.
    r_without = slope_without = None
    if n > MIN_FIT_POINTS:
        trimmed = sorted(points, key=lambda pair: pair[0])[:-1]
        tx = [x for x, _ in trimmed]
        ty = [y for _, y in trimmed]
        m = len(trimmed)
        tmx, tmy = sum(tx) / m, sum(ty) / m
        tdx = [x - tmx for x in tx]
        tdy = [y - tmy for y in ty]
        tsxx = sum(v * v for v in tdx)
        tsyy = sum(v * v for v in tdy)
        if tsxx and tsyy:
            tsxy = sum(a * b for a, b in zip(tdx, tdy))
            slope_without = tsxy / tsxx
            r_without = tsxy / (tsxx * tsyy) ** 0.5

    return CadenceModel(
        intercept=intercept,
        slope=slope,
        r=r,
        n=n,
        residual_sd=residual_sd,
        min_speed=min(xs),
        max_speed=max(xs),
        r_without_extreme=r_without,
        slope_without_extreme=slope_without,
    )


@dataclass(frozen=True)
class FitDiagnostics:
    """Why a cadence fit was accepted or refused.

    Returning a bare None loses the interesting part: whether there was too
    little data, or plenty of data showing no relationship. Those call for
    opposite responses — collect more runs, versus stop looking.
    """

    n: int
    r: float | None
    pace_range: tuple[float, float] | None  # seconds per mile, fast to slow
    excluded_non_steady: int
    accepted: bool
    reason: str

    @property
    def pace_spread_seconds(self) -> float | None:
        if not self.pace_range:
            return None
        return self.pace_range[1] - self.pace_range[0]


def diagnose_fit(runs: Sequence[RunSummary]) -> FitDiagnostics:
    """Report what a cadence fit found, whether or not it succeeded."""
    with_cadence = [r for r in runs if r.steps_per_minute and r.average_speed_mps]
    steady = [r for r in with_cadence if is_steady(r)]
    excluded = len(with_cadence) - len(steady)

    if len(steady) < MIN_FIT_POINTS:
        return FitDiagnostics(
            n=len(steady),
            r=None,
            pace_range=None,
            excluded_non_steady=excluded,
            accepted=False,
            reason=(
                f"Only {len(steady)} steady run(s) carry cadence; {MIN_FIT_POINTS} "
                "are needed. Collect more before concluding anything."
            ),
        )

    xs = [r.average_speed_mps * 60.0 for r in steady]
    ys = [r.steps_per_minute for r in steady]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sxx = sum(v * v for v in dx)
    syy = sum(v * v for v in dy)
    r = sum(a * b for a, b in zip(dx, dy)) / (sxx * syy) ** 0.5 if sxx and syy else None

    paces = sorted(METERS_PER_MILE / r_.average_speed_mps for r_ in steady)
    pace_range = (paces[0], paces[-1])

    if r is None:
        reason = "No variation in speed or cadence to correlate."
    elif abs(r) < MIN_USEFUL_R:
        reason = (
            f"n={n} steady runs across {(pace_range[1] - pace_range[0]) / 60:.1f} min/mi "
            f"give r={r:+.2f}, below the {MIN_USEFUL_R} threshold. Within steady "
            "running, cadence does not track pace — use a constant."
        )
    else:
        reason = f"n={n}, r={r:+.2f} — a slope is justified."

    return FitDiagnostics(
        n=n,
        r=r,
        pace_range=pace_range,
        excluded_non_steady=excluded,
        accepted=r is not None and abs(r) >= MIN_USEFUL_R,
        reason=reason,
    )


def target_cadence(
    pace_seconds_per_mile: float | None = None,
    model: CadenceModel | None = None,
) -> float:
    """The cadence to match music against for a given prescribed pace.

    With no pace or no model, falls back to the measured mean — a fixed-tempo
    playlist, which is what the project would have shipped before the wider
    history revealed the relationship.
    """
    if pace_seconds_per_mile is None:
        return DEFAULT_TARGET_SPM
    return (model or REFERENCE_MODEL).cadence_at_pace(pace_seconds_per_mile)


@dataclass(frozen=True)
class TempoMatch:
    """How well one track's BPM suits a target cadence."""

    bpm: float
    effective_bpm: float
    multiplier: float
    distance: float
    target_spm: float
    tolerance: float

    @property
    def within_band(self) -> bool:
        return self.distance <= self.tolerance

    @property
    def is_half_time(self) -> bool:
        """True when the track is being used at double its listed tempo.

        Worth surfacing: a 79 BPM track matching a 158 spm target is correct but
        counter-intuitive, and a user seeing "79 BPM" in a running playlist will
        assume it's a bug unless it's explained.
        """
        return self.multiplier == 2.0

    def describe(self) -> str:
        if self.multiplier == 1.0:
            return f"{self.bpm:.0f} BPM"
        if self.multiplier == 2.0:
            return f"{self.bpm:.0f} BPM (half-time, {self.effective_bpm:.0f} effective)"
        return f"{self.bpm:.0f} BPM (double-time, {self.effective_bpm:.0f} effective)"


def match_tempo(
    bpm: float,
    target_spm: float = DEFAULT_TARGET_SPM,
    tolerance: float = DEFAULT_TOLERANCE_SPM,
) -> TempoMatch:
    """Score a BPM against a cadence target, trying every stride multiplier."""
    if bpm <= 0:
        raise ValueError("BPM must be positive")

    best = min(
        ((m, abs(bpm * m - target_spm)) for m in STRIDE_MULTIPLIERS),
        key=lambda pair: (pair[1], abs(pair[0] - 1.0)),  # prefer 1:1 on a tie
    )
    multiplier, distance = best
    return TempoMatch(
        bpm=bpm,
        effective_bpm=bpm * multiplier,
        multiplier=multiplier,
        distance=distance,
        target_spm=target_spm,
        tolerance=tolerance,
    )


def usable_bpm_ranges(
    target_spm: float = DEFAULT_TARGET_SPM, tolerance: float = DEFAULT_TOLERANCE_SPM
) -> list[tuple[float, float]]:
    """The raw BPM windows that map onto the target, one per multiplier.

    Useful for querying a music source directly: rather than filtering a whole
    library, ask for tracks already inside these bands.
    """
    ranges = []
    for multiplier in sorted(STRIDE_MULTIPLIERS):
        low = (target_spm - tolerance) / multiplier
        high = (target_spm + tolerance) / multiplier
        ranges.append((low, high))
    return sorted(ranges)


def rank(
    candidates: Iterable[tuple[str, float]],
    target_spm: float = DEFAULT_TARGET_SPM,
    tolerance: float = DEFAULT_TOLERANCE_SPM,
) -> list[tuple[str, TempoMatch]]:
    """Order `(track_id, bpm)` pairs by how well they fit the target."""
    scored = [
        (track_id, match_tempo(bpm, target_spm, tolerance))
        for track_id, bpm in candidates
        if bpm and bpm > 0
    ]
    scored.sort(key=lambda pair: pair[1].distance)
    return scored


def select_playlist(
    candidates: Sequence[tuple[str, float]],
    count: int = 20,
    target_spm: float = DEFAULT_TARGET_SPM,
    tolerance: float = DEFAULT_TOLERANCE_SPM,
) -> tuple[list[tuple[str, TempoMatch]], list[str]]:
    """Pick the best `count` tracks, and say what was rejected and why.

    Returns (selected, warnings). Falling back to out-of-band tracks is allowed
    rather than returning a two-song playlist, but it is always reported — a
    silently loosened tolerance is how a "pace-synced" playlist ends up not
    being pace-synced.
    """
    ranked = rank(candidates, target_spm, tolerance)
    in_band = [pair for pair in ranked if pair[1].within_band]
    warnings: list[str] = []

    if len(in_band) >= count:
        selected = in_band[:count]
    else:
        selected = ranked[:count]
        if len(ranked) < count:
            warnings.append(
                f"Only {len(ranked)} candidate(s) had a BPM at all; wanted {count}."
            )
        if len(in_band) < count:
            warnings.append(
                f"Only {len(in_band)} track(s) fell within ±{tolerance:.0f} spm of "
                f"{target_spm:.0f}. Filled the rest from outside the band — "
                "these will not match your cadence."
            )

    half_time = sum(1 for _, m in selected if m.is_half_time)
    if half_time:
        warnings.append(
            f"{half_time} of {len(selected)} track(s) match at half-time. That is "
            "expected — little popular music sits at 160 BPM."
        )
    return selected, warnings
