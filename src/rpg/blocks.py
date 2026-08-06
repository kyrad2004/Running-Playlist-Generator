"""Finding periods of consistent training in a run history.

A year of activity is rarely uniform — it's blocks of real training separated by
months of almost nothing. Averages across the whole span describe neither.

This matters twice. Analysing a block gives a picture of what training looks
like when it's actually happening, and a block is the only honest place to
evaluate the plan engine: asking "what would this have told me mid-block?" is
answerable, where "what should I do today, having run twice in a month?" is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from .activity import RunSummary

DEFAULT_MIN_RUNS_PER_WEEK = 2
DEFAULT_MIN_WEEKS = 3

# One quiet week inside an otherwise solid block is travel or a rest week, not
# the end of training. Allowing it stops blocks fragmenting into noise.
DEFAULT_MAX_GAP_WEEKS = 1


def run_date(run: RunSummary) -> date | None:
    raw = (run.start_date_local or "")[:10]
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def week_start(day: date) -> date:
    """The Monday of that day's week."""
    return day - timedelta(days=day.weekday())


@dataclass
class TrainingBlock:
    """A stretch of weeks with running happening regularly."""

    runs: list[RunSummary] = field(default_factory=list)

    @property
    def dates(self) -> list[date]:
        return sorted(d for d in (run_date(r) for r in self.runs) if d)

    @property
    def start(self) -> date | None:
        dates = self.dates
        return dates[0] if dates else None

    @property
    def end(self) -> date | None:
        dates = self.dates
        return dates[-1] if dates else None

    @property
    def days(self) -> int:
        if not self.start or not self.end:
            return 0
        return (self.end - self.start).days + 1

    @property
    def weeks(self) -> float:
        return self.days / 7.0

    @property
    def total_miles(self) -> float:
        return sum(r.distance_miles or 0 for r in self.runs)

    @property
    def runs_per_week(self) -> float:
        return len(self.runs) / self.weeks if self.weeks else 0.0

    @property
    def miles_per_week(self) -> float:
        return self.total_miles / self.weeks if self.weeks else 0.0

    @property
    def longest_run_miles(self) -> float:
        return max((r.distance_miles or 0 for r in self.runs), default=0.0)

    def age_days(self, today: date) -> int | None:
        """How long ago the block ended."""
        return (today - self.end).days if self.end else None

    def midpoint(self) -> date | None:
        """A sensible date to evaluate the engine 'as of'.

        Mid-block there is history behind it and training still ongoing, which
        is the situation the engine is actually designed for.
        """
        if not self.start or not self.end:
            return None
        return self.start + timedelta(days=self.days // 2)

    def describe(self) -> str:
        return (
            f"{self.start} to {self.end}  "
            f"{len(self.runs)} runs over {self.weeks:.1f} weeks  "
            f"{self.total_miles:.1f} mi  "
            f"({self.runs_per_week:.1f} runs/wk, {self.miles_per_week:.1f} mi/wk)"
        )


def find_training_blocks(
    runs: Iterable[RunSummary],
    min_runs_per_week: int = DEFAULT_MIN_RUNS_PER_WEEK,
    min_weeks: int = DEFAULT_MIN_WEEKS,
    max_gap_weeks: int = DEFAULT_MAX_GAP_WEEKS,
) -> list[TrainingBlock]:
    """Identify stretches of consistent running, most recent first."""
    by_week: dict[date, list[RunSummary]] = {}
    for run in runs:
        day = run_date(run)
        if day:
            by_week.setdefault(week_start(day), []).append(run)

    if not by_week:
        return []

    first, last = min(by_week), max(by_week)
    all_weeks: list[date] = []
    cursor = first
    while cursor <= last:
        all_weeks.append(cursor)
        cursor += timedelta(days=7)

    qualifying = {w for w in all_weeks if len(by_week.get(w, [])) >= min_runs_per_week}

    blocks: list[TrainingBlock] = []
    current: list[date] = []
    gap = 0

    for week in all_weeks:
        if week in qualifying:
            current.append(week)
            gap = 0
        elif current:
            gap += 1
            if gap > max_gap_weeks:
                blocks.append(current)
                current, gap = [], 0
            else:
                current.append(week)  # tolerated dip, still inside the block
    if current:
        blocks.append(current)

    result: list[TrainingBlock] = []
    for weeks in blocks:
        # Trim tolerated dips from the edges — a block shouldn't start or end
        # with a week that didn't qualify.
        while weeks and weeks[0] not in qualifying:
            weeks.pop(0)
        while weeks and weeks[-1] not in qualifying:
            weeks.pop()
        if len(weeks) < min_weeks:
            continue
        collected = [r for w in weeks for r in by_week.get(w, [])]
        if collected:
            result.append(TrainingBlock(collected))

    result.sort(key=lambda b: b.start or date.min, reverse=True)
    return result


def best_block(blocks: Sequence[TrainingBlock]) -> TrainingBlock | None:
    """The block with the most training in it — the best place to evaluate."""
    if not blocks:
        return None
    return max(blocks, key=lambda b: (b.total_miles, len(b.runs)))
