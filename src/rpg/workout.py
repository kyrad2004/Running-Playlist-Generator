"""Classifying what kind of run an activity was.

Averages only describe a run that was actually steady. An interval session's
average pace blends hard reps with recovery jogs; a hill workout's average pace
is dragged down by climbing rather than by effort. Feeding either into a VDOT
estimate or a cadence-vs-pace fit treats a number that describes no part of the
run as though it described all of it.

Detection uses three signals, cheapest first:

1. **The activity name.** Free, and often decisive — "4K Tempo Run" says so.
   Unreliable alone, since half of everything is called "Afternoon Run".
2. **Elevation per mile.** Free, from the summary payload. Identifies hills.
3. **Split or lap variance.** Definitive, but only on the detail endpoint, at
   one request per activity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .activity import METERS_PER_MILE, RunSummary

STEADY = "steady"
INTERVAL = "interval"
HILL = "hill"
TEMPO = "tempo"
RACE = "race"
LONG = "long"
UNKNOWN = "unknown"

# Kinds whose average pace does not describe a sustained effort, so neither VDOT
# nor a cadence fit should use the whole-activity numbers.
NOT_STEADY = frozenset({INTERVAL, HILL})

# Order matters: "hill repeats" is a hill session, not an interval session, so
# HILL is tested first even though both patterns match it.
_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    (HILL, r"\bhill|\bincline|\bstair"),
    (INTERVAL, r"\binterval|\brepeat|\bfartlek|\bspeed\s*(run|work|session)|\btrack\b"
               r"|\b\d{3,4}\s*m\s*(repeat|rep)|\bx\s*\d{3,4}\s*m|\bsprint"),
    (RACE, r"\brace\b|\bmarathon\b|\bhalf\b|\b5k\b|\b10k\b|\bparkrun|\btime\s*trial|\btt\b"),
    (TEMPO, r"\btempo|\bthreshold|\bprogression|\bnegative\s+split"),
    (LONG, r"\blong\s+run"),
)

# Phrases that cancel a specific kind. "4.5mi Race Practice Long Run" contains
# "race" but is training — the negation rules out RACE only, leaving the name
# free to match LONG instead.
_NEGATIONS: dict[str, str] = {
    RACE: r"race\s+practice|practice\s+race|race\s+pace",
}

# Metres of climbing per mile. A flat road run sits under ~15; a deliberate hill
# session is far above. Between the two is rolling terrain, not a workout.
HILLY_METERS_PER_MILE = 30.0

# Coefficient of variation across per-mile splits. A steady run holds well under
# 5%; alternating reps and recoveries pushes it past 10%.
INTERVAL_SPLIT_CV = 0.10


@dataclass
class Classification:
    kind: str
    confidence: float
    signals: list[str] = field(default_factory=list)

    @property
    def steady(self) -> bool:
        """Whether the whole-activity average is a meaningful summary."""
        return self.kind not in NOT_STEADY

    def describe(self) -> str:
        detail = f" ({'; '.join(self.signals)})" if self.signals else ""
        return f"{self.kind}{detail}"


def _from_name(name: str | None) -> tuple[str, str] | None:
    if not name:
        return None
    lowered = name.lower()
    for kind, pattern in _NAME_PATTERNS:
        if not re.search(pattern, lowered):
            continue
        negation = _NEGATIONS.get(kind)
        if negation and re.search(negation, lowered):
            continue  # this kind is ruled out; a later pattern may still fit
        return kind, f"name matches {kind}"
    return None


def _elevation_per_mile(run: RunSummary) -> float | None:
    if not run.total_elevation_gain_m or not run.distance_miles:
        return None
    return run.total_elevation_gain_m / run.distance_miles


def _split_cv(detail: dict) -> float | None:
    """Coefficient of variation across per-mile split speeds."""
    splits = detail.get("splits_standard") or []
    speeds = [s.get("average_speed") for s in splits if s.get("average_speed")]
    if len(speeds) < 3:
        return None
    mean = sum(speeds) / len(speeds)
    if mean <= 0:
        return None
    variance = sum((v - mean) ** 2 for v in speeds) / len(speeds)
    return variance**0.5 / mean


def classify(run: RunSummary, detail: dict | None = None) -> Classification:
    """Best guess at what kind of session this was.

    `detail` is the payload from GET /activities/{id}; supplying it upgrades a
    guess to a measurement, since split variance separates intervals from steady
    running directly rather than by naming convention.
    """
    signals: list[str] = []
    kind = UNKNOWN
    confidence = 0.0

    named = _from_name(run.name)
    if named:
        kind, signal = named
        signals.append(signal)
        confidence = 0.7

    climb = _elevation_per_mile(run)
    if climb is not None and climb >= HILLY_METERS_PER_MILE:
        signals.append(f"{climb:.0f} m climb/mile")
        if kind in (UNKNOWN, STEADY):
            kind, confidence = HILL, 0.7
        else:
            confidence = min(confidence + 0.15, 0.95)

    if detail is not None:
        cv = _split_cv(detail)
        if cv is not None:
            signals.append(f"split variation {cv * 100:.0f}%")
            if cv >= INTERVAL_SPLIT_CV:
                # Measured surging beats any naming convention.
                if kind not in (HILL,):
                    kind = INTERVAL
                confidence = max(confidence, 0.9)
            elif kind == UNKNOWN:
                kind, confidence = STEADY, 0.8
            elif kind == INTERVAL:
                # Named like intervals but ran flat — trust the measurement.
                kind, confidence = STEADY, 0.6
                signals.append("named as intervals but splits are even")

    if kind == UNKNOWN:
        kind, confidence = STEADY, 0.3
        signals.append("no signal; assumed steady")

    return Classification(kind=kind, confidence=confidence, signals=signals)


def annotate(run: RunSummary, detail: dict | None = None) -> Classification:
    """Classify and record the verdict on the run for downstream consumers."""
    result = classify(run, detail)
    run.raw["_workout_kind"] = result.kind
    run.raw["_workout_steady"] = result.steady
    return result


def annotate_all(
    runs: Iterable[RunSummary], details: dict[int, dict] | None = None
) -> dict[str, int]:
    """Annotate every run, returning a count by kind."""
    details = details or {}
    counts: dict[str, int] = {}
    for run in runs:
        result = annotate(run, details.get(run.id))
        counts[result.kind] = counts.get(result.kind, 0) + 1
    return counts


def is_steady(run: RunSummary) -> bool:
    """Whether this run's averages can be trusted as a sustained effort.

    Defaults to True for unannotated runs, so nothing is silently dropped just
    because classification hasn't run.
    """
    return run.raw.get("_workout_steady", True)


def steady_runs(runs: Iterable[RunSummary]) -> list[RunSummary]:
    return [r for r in runs if is_steady(r)]
