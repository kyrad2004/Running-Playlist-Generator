"""Detect and merge duplicate activities.

Recording one run on two apps at once (Nike Run Club syncing to Strava while the
Strava app also records) produces two activities for one run. Left alone this
doubles weekly volume, corrupts any 80/20 easy-hard split, and lets "best recent
effort" pick whichever copy happens to be listed first.

The two copies rarely agree exactly — GPS drift, different auto-pause behaviour,
and warmup captured by one app but not the other all shift the distance — so
matching is by tolerance rather than equality, and anything uncertain is
reported for review rather than merged silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .activity import RunSummary

# Same-day pairs within these bounds are treated as one run. Distance is
# generous because one app often records a warmup the other misses.
DISTANCE_TOLERANCE = 0.25
TIME_TOLERANCE = 0.30

# Below this, the two copies agree closely enough to merge without a second look.
HIGH_CONFIDENCE_DISTANCE = 0.05

# Strava auto-names activities by time of day. A name outside this set was typed
# by a human and usually carries real information — "Malta Half Marathon" says
# the effort was maximal, which is exactly what a VDOT estimate needs to know.
GENERIC_NAME_SUFFIX = " Run"
GENERIC_NAME_PREFIXES = ("Morning", "Afternoon", "Evening", "Lunch", "Night")


def is_generic_name(name: str | None) -> bool:
    if not name:
        return True
    name = name.strip()
    return name.endswith(GENERIC_NAME_SUFFIX) and name[: -len(GENERIC_NAME_SUFFIX)] in (
        GENERIC_NAME_PREFIXES
    )


def _relative_gap(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return abs(a - b) / max(a, b)


def _richness(run: RunSummary) -> int:
    """How many of the fields later phases need this record actually has."""
    return sum(
        (
            bool(run.average_heartrate),
            bool(run.average_cadence_raw),
            bool(run.average_speed_mps),
            bool(run.raw.get("splits_standard")),
        )
    )


@dataclass
class DuplicateGroup:
    """Two or more activities believed to be the same run."""

    runs: list[RunSummary] = field(default_factory=list)

    @property
    def date(self) -> str:
        return (self.runs[0].start_date_local or "")[:10]

    @property
    def distance_gap(self) -> float:
        distances = [r.distance_m for r in self.runs if r.distance_m]
        if len(distances) < 2:
            return 0.0
        return (max(distances) - min(distances)) / max(distances)

    @property
    def high_confidence(self) -> bool:
        return self.distance_gap <= HIGH_CONFIDENCE_DISTANCE

    @property
    def heartrate_conflict(self) -> float | None:
        """Spread in bpm when more than one copy recorded heart rate.

        Two chest straps or a strap and a wrist sensor disagreeing by a few bpm
        is normal. A wide gap means one of them is wrong, and merging silently
        picks a winner — worth surfacing before that value feeds a zone
        calculation.
        """
        readings = [r.average_heartrate for r in self.runs if r.average_heartrate]
        if len(readings) < 2:
            return None
        return max(readings) - min(readings)

    def primary(self) -> RunSummary:
        """The copy to keep: richest first, longest as the tiebreak."""
        return max(self.runs, key=lambda r: (_richness(r), r.distance_m or 0))

    def merged(self) -> RunSummary:
        """The primary record, with gaps filled from the other copies.

        Only null fields are filled — a value present on the primary is never
        overwritten, so the merged record stays internally consistent rather
        than mixing pace from one device with heart rate from another.
        """
        primary = self.primary()
        others = [r for r in self.runs if r is not primary]

        merged = RunSummary(**{**primary.__dict__})
        merged.raw = dict(primary.raw)

        for other in others:
            if merged.average_heartrate is None and other.average_heartrate is not None:
                merged.average_heartrate = other.average_heartrate
                merged.max_heartrate = other.max_heartrate
                merged.has_heartrate = True
            if merged.average_cadence_raw is None and other.average_cadence_raw is not None:
                merged.average_cadence_raw = other.average_cadence_raw
            # A human-typed name beats an auto-generated one regardless of which
            # copy is richer, so a race keeps its label after merging.
            if is_generic_name(merged.name) and not is_generic_name(other.name):
                merged.name = other.name

        merged.raw["_merged_from"] = [r.id for r in self.runs]
        return merged


def find_duplicate_groups(
    runs: Iterable[RunSummary],
    distance_tolerance: float = DISTANCE_TOLERANCE,
    time_tolerance: float = TIME_TOLERANCE,
) -> list[DuplicateGroup]:
    """Group same-day activities that look like the same run.

    Grouping is transitive within a day: if A matches B and B matches C, all
    three become one group, which handles a run recorded on three devices.
    """
    by_date: dict[str, list[RunSummary]] = {}
    for run in runs:
        by_date.setdefault((run.start_date_local or "")[:10], []).append(run)

    groups: list[DuplicateGroup] = []
    for date, same_day in sorted(by_date.items()):
        if not date or len(same_day) < 2:
            continue

        unassigned = list(same_day)
        while unassigned:
            seed = unassigned.pop(0)
            cluster = [seed]
            changed = True
            while changed:
                changed = False
                for candidate in list(unassigned):
                    if any(_is_match(m, candidate, distance_tolerance, time_tolerance)
                           for m in cluster):
                        cluster.append(candidate)
                        unassigned.remove(candidate)
                        changed = True
            if len(cluster) > 1:
                groups.append(DuplicateGroup(cluster))

    return groups


def _is_match(
    a: RunSummary, b: RunSummary, distance_tolerance: float, time_tolerance: float
) -> bool:
    distance_gap = _relative_gap(a.distance_m, b.distance_m)
    if distance_gap is None or distance_gap > distance_tolerance:
        return False

    time_gap = _relative_gap(a.moving_time_s, b.moving_time_s)
    # Missing duration on either side shouldn't veto a clear distance match.
    if time_gap is not None and time_gap > time_tolerance:
        return False
    return True


def dedupe(
    runs: Sequence[RunSummary],
    distance_tolerance: float = DISTANCE_TOLERANCE,
    time_tolerance: float = TIME_TOLERANCE,
) -> tuple[list[RunSummary], list[DuplicateGroup]]:
    """Return (deduplicated runs, the groups that were collapsed)."""
    groups = find_duplicate_groups(runs, distance_tolerance, time_tolerance)
    duplicated_ids = {r.id for g in groups for r in g.runs}

    result = [r for r in runs if r.id not in duplicated_ids]
    result.extend(g.merged() for g in groups)
    result.sort(key=lambda r: r.start_date_local or "", reverse=True)
    return result, groups
