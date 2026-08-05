"""Normalizing Strava activity payloads into the fields this project cares about.

Phase 0's question is "what actually comes back?", so this module deliberately
keeps raw values alongside derived ones — nothing is silently coerced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

METERS_PER_MILE = 1609.344

# Strava's sport_type values that count as running for our purposes.
RUN_SPORT_TYPES = {"Run", "TrailRun", "VirtualRun"}


def is_run(activity: dict) -> bool:
    return activity.get("sport_type", activity.get("type")) in RUN_SPORT_TYPES


def mps_to_seconds_per_km(mps: float | None) -> float | None:
    if not mps:
        return None
    return 1000.0 / mps


def mps_to_seconds_per_mile(mps: float | None) -> float | None:
    if not mps:
        return None
    return METERS_PER_MILE / mps


def format_pace(seconds_per_unit: float | None) -> str:
    """Render a pace as m:ss. Returns '—' when the input is missing."""
    if not seconds_per_unit or seconds_per_unit <= 0:
        return "—"
    minutes, seconds = divmod(int(round(seconds_per_unit)), 60)
    return f"{minutes}:{seconds:02d}"


def steps_per_minute(average_cadence: float | None) -> float | None:
    """Convert Strava run cadence to true step rate.

    Strava reports running cadence in RPM for ONE leg, so a typical 170 spm
    runner shows up as 85. Doubling it is required before any BPM matching —
    getting this wrong halves every target tempo.
    """
    if not average_cadence:
        return None
    return average_cadence * 2


@dataclass
class RunSummary:
    """The subset of an activity the plan's later phases depend on."""

    id: int
    name: str
    start_date_local: str
    sport_type: str
    distance_m: float | None
    moving_time_s: int | None
    elapsed_time_s: int | None
    average_speed_mps: float | None
    max_speed_mps: float | None
    has_heartrate: bool
    average_heartrate: float | None
    max_heartrate: float | None
    average_cadence_raw: float | None
    total_elevation_gain_m: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- derived ---------------------------------------------------------
    @property
    def distance_km(self) -> float | None:
        return self.distance_m / 1000.0 if self.distance_m else None

    @property
    def distance_miles(self) -> float | None:
        return self.distance_m / METERS_PER_MILE if self.distance_m else None

    @property
    def pace_per_km_s(self) -> float | None:
        return mps_to_seconds_per_km(self.average_speed_mps)

    @property
    def pace_per_mile_s(self) -> float | None:
        return mps_to_seconds_per_mile(self.average_speed_mps)

    @property
    def steps_per_minute(self) -> float | None:
        return steps_per_minute(self.average_cadence_raw)

    @property
    def has_cadence(self) -> bool:
        return self.average_cadence_raw is not None


def summarize(activity: dict) -> RunSummary:
    return RunSummary(
        id=activity.get("id"),
        name=activity.get("name", ""),
        start_date_local=activity.get("start_date_local", ""),
        sport_type=activity.get("sport_type", activity.get("type", "")),
        distance_m=activity.get("distance"),
        moving_time_s=activity.get("moving_time"),
        elapsed_time_s=activity.get("elapsed_time"),
        average_speed_mps=activity.get("average_speed"),
        max_speed_mps=activity.get("max_speed"),
        has_heartrate=bool(activity.get("has_heartrate")),
        average_heartrate=activity.get("average_heartrate"),
        max_heartrate=activity.get("max_heartrate"),
        average_cadence_raw=activity.get("average_cadence"),
        total_elevation_gain_m=activity.get("total_elevation_gain"),
        raw=activity,
    )


@dataclass
class Coverage:
    """How much of the data the later phases need is actually present.

    Phase 2 branches on has_heartrate, and Phase 3 wants cadence. Knowing the
    real coverage rate up front decides how much the no-HR fallback matters.
    """

    total_runs: int
    with_heartrate: int
    with_cadence: int
    with_speed: int

    @property
    def heartrate_pct(self) -> float:
        return 100.0 * self.with_heartrate / self.total_runs if self.total_runs else 0.0

    @property
    def cadence_pct(self) -> float:
        return 100.0 * self.with_cadence / self.total_runs if self.total_runs else 0.0

    @property
    def speed_pct(self) -> float:
        return 100.0 * self.with_speed / self.total_runs if self.total_runs else 0.0


def coverage(runs: Iterable[RunSummary]) -> Coverage:
    runs = list(runs)
    return Coverage(
        total_runs=len(runs),
        with_heartrate=sum(1 for r in runs if r.has_heartrate),
        with_cadence=sum(1 for r in runs if r.has_cadence),
        with_speed=sum(1 for r in runs if r.average_speed_mps),
    )
