"""Matching music tempo to running cadence.

Finding 5 measured cadence against pace across a 1.8 min/mi spread and found no
relationship (r = -0.12, sd 2.5 spm). So the target is a constant, not a model —
one parameter, and the data doesn't support two.

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

# Measured mean across the 10 cadence-carrying runs in a real account.
DEFAULT_TARGET_SPM = 160.0

# Measured sd was 2.5 spm; ±5 covers roughly two standard deviations.
DEFAULT_TOLERANCE_SPM = 5.0

# Multipliers a runner can actually stride to. 1 is a step per beat, 2 doubles a
# slow track, 0.5 halves a fast one. Beyond that the beat stops being findable.
STRIDE_MULTIPLIERS = (0.5, 1.0, 2.0)


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
