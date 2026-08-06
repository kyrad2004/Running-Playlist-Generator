"""Finding consistent training blocks in a lumpy history."""

from datetime import date, timedelta

from rpg.activity import METERS_PER_MILE, summarize
from rpg.blocks import best_block, find_training_blocks, week_start


def run(day: str, miles: float = 3.0, id: int = 0):
    seconds = miles * 540
    return summarize(
        {
            "id": id,
            "sport_type": "Run",
            "start_date_local": f"{day}T07:00:00Z",
            "distance": miles * METERS_PER_MILE,
            "moving_time": int(seconds),
            "average_speed": (miles * METERS_PER_MILE) / seconds,
            "has_heartrate": False,
        }
    )


def runs_over(start: date, weeks: int, per_week: int, miles: float = 3.0):
    """Regular training: `per_week` runs a week for `weeks` weeks."""
    out = []
    for week in range(weeks):
        for n in range(per_week):
            day = start + timedelta(days=week * 7 + n * 2)
            out.append(run(day.isoformat(), miles, id=week * 10 + n))
    return out


def test_week_start_is_monday():
    assert week_start(date(2026, 8, 6)).weekday() == 0  # a Thursday -> Monday
    assert week_start(date(2026, 8, 3)) == date(2026, 8, 3)  # already Monday


def test_finds_a_consistent_block():
    runs = runs_over(date(2025, 6, 2), weeks=8, per_week=3)
    blocks = find_training_blocks(runs)
    assert len(blocks) == 1
    assert blocks[0].runs_per_week > 2.5
    assert len(blocks[0].runs) == 24


def test_sparse_history_yields_no_block():
    """One run a month is not a training block."""
    runs = [run("2025-06-01"), run("2025-07-01"), run("2025-08-01")]
    assert find_training_blocks(runs) == []


def test_two_blocks_separated_by_a_quiet_period():
    """The real shape: train, stop for months, train again."""
    summer = runs_over(date(2025, 6, 2), weeks=6, per_week=3)
    winter = runs_over(date(2026, 1, 5), weeks=6, per_week=3)
    quiet = [run("2025-09-15"), run("2025-11-03")]

    blocks = find_training_blocks(summer + quiet + winter)
    assert len(blocks) == 2
    # Most recent first.
    assert blocks[0].start > blocks[1].start


def test_a_single_down_week_does_not_split_a_block():
    """A rest or travel week is part of the block, not the end of it."""
    before = runs_over(date(2025, 6, 2), weeks=3, per_week=3)
    after = runs_over(date(2025, 6, 30), weeks=3, per_week=3)  # skips one week
    blocks = find_training_blocks(before + after)
    assert len(blocks) == 1


def test_two_consecutive_down_weeks_do_split():
    before = runs_over(date(2025, 6, 2), weeks=3, per_week=3)
    after = runs_over(date(2025, 7, 7), weeks=3, per_week=3)  # skips two weeks
    assert len(find_training_blocks(before + after)) == 2


def test_blocks_do_not_start_or_end_on_a_tolerated_dip():
    runs = runs_over(date(2025, 6, 2), weeks=4, per_week=3) + [run("2025-07-10")]
    block = find_training_blocks(runs)[0]
    assert block.end < date(2025, 7, 10)


def test_short_blocks_are_ignored():
    runs = runs_over(date(2025, 6, 2), weeks=2, per_week=3)
    assert find_training_blocks(runs, min_weeks=3) == []
    assert len(find_training_blocks(runs, min_weeks=2)) == 1


def test_block_statistics():
    runs = runs_over(date(2025, 6, 2), weeks=4, per_week=3, miles=4.0)
    block = find_training_blocks(runs)[0]
    assert len(block.runs) == 12
    assert block.total_miles == 48.0
    assert 2.5 < block.runs_per_week < 4.5
    assert block.longest_run_miles == 4.0


def test_block_age_and_midpoint():
    runs = runs_over(date(2025, 6, 2), weeks=4, per_week=3)
    block = find_training_blocks(runs)[0]
    assert block.midpoint() > block.start
    assert block.midpoint() < block.end
    assert block.age_days(date(2026, 8, 6)) > 365


def test_best_block_picks_the_biggest():
    small = runs_over(date(2025, 6, 2), weeks=3, per_week=2, miles=2.0)
    large = runs_over(date(2026, 1, 5), weeks=6, per_week=4, miles=5.0)
    blocks = find_training_blocks(small + large)
    assert best_block(blocks).total_miles > 100


def test_best_block_of_nothing_is_none():
    assert best_block([]) is None


def test_unparseable_dates_are_skipped():
    bad = summarize({"id": 1, "sport_type": "Run", "start_date_local": "nonsense"})
    runs = runs_over(date(2025, 6, 2), weeks=4, per_week=3) + [bad]
    assert len(find_training_blocks(runs)) == 1
