"""Duplicate detection, fixtured on real dual-recorded pairs."""

from rpg.activity import summarize
from rpg.dedupe import dedupe, find_duplicate_groups

METERS_PER_MILE = 1609.344


def run(id, date, miles, seconds=None, hr=None, cadence=None, name=""):
    return summarize(
        {
            "id": id,
            "name": name,
            "sport_type": "Run",
            "start_date_local": f"{date}T07:00:00Z",
            "distance": miles * METERS_PER_MILE,
            "moving_time": seconds if seconds is not None else int(miles * 540),
            "average_speed": (miles * METERS_PER_MILE) / (seconds or (miles * 540)),
            "has_heartrate": hr is not None,
            "average_heartrate": hr,
            "average_cadence": cadence,
        }
    )


def test_near_exact_pair_is_high_confidence():
    """13.28 vs 13.27 mi on one day — the same half marathon, twice."""
    runs = [
        run(1, "2026-02-22", 13.28, name="Malta Half Marathon"),
        run(2, "2026-02-22", 13.27, hr=169, name="Morning Run"),
    ]
    groups = find_duplicate_groups(runs)
    assert len(groups) == 1
    assert groups[0].high_confidence
    assert groups[0].distance_gap < 0.01


def test_looser_pair_still_groups_but_is_flagged():
    """8.75 vs 8.00 mi — one app caught a warmup the other didn't."""
    groups = find_duplicate_groups(
        [run(1, "2026-01-21", 8.75), run(2, "2026-01-21", 8.00, hr=167, cadence=79.5)]
    )
    assert len(groups) == 1
    assert not groups[0].high_confidence


def test_richer_copy_is_kept():
    runs = [
        run(1, "2026-02-11", 4.55, name="Afternoon Run"),
        run(2, "2026-02-11", 4.63, hr=162, cadence=80.5, name="Nike Run Club"),
    ]
    assert find_duplicate_groups(runs)[0].primary().id == 2


def test_merge_fills_gaps_without_overwriting():
    runs = [
        run(1, "2026-02-11", 4.63, hr=162, name="has HR only"),
        run(2, "2026-02-11", 4.55, cadence=80.5, name="has cadence only"),
    ]
    merged = find_duplicate_groups(runs)[0].merged()
    assert merged.average_heartrate == 162
    assert merged.average_cadence_raw == 80.5
    assert merged.steps_per_minute == 161.0


def test_merge_records_its_sources():
    runs = [run(1, "2026-02-22", 13.28), run(2, "2026-02-22", 13.27, hr=169)]
    assert set(find_duplicate_groups(runs)[0].merged().raw["_merged_from"]) == {1, 2}


def test_genuinely_different_runs_on_one_day_are_not_merged():
    """A 2-mile shakeout and a 10-mile long run share a date but aren't one run."""
    assert find_duplicate_groups([run(1, "2026-02-02", 2.0), run(2, "2026-02-02", 10.05)]) == []


def test_same_distance_on_different_days_is_not_a_duplicate():
    assert find_duplicate_groups([run(1, "2026-02-01", 5.0), run(2, "2026-02-02", 5.0)]) == []


def test_wildly_different_duration_blocks_a_match():
    """Same distance, but one took twice as long — a run and a walk, not a pair."""
    runs = [run(1, "2026-02-01", 5.0, seconds=2700), run(2, "2026-02-01", 5.0, seconds=5400)]
    assert find_duplicate_groups(runs) == []


def test_three_way_grouping_is_transitive():
    runs = [
        run(1, "2026-02-11", 4.55),
        run(2, "2026-02-11", 4.63, hr=162),
        run(3, "2026-02-11", 4.60, cadence=80.0),
    ]
    groups = find_duplicate_groups(runs)
    assert len(groups) == 1 and len(groups[0].runs) == 3


def test_dedupe_collapses_and_preserves_singletons():
    runs = [
        run(1, "2026-08-02", 1.75, name="solo recent run"),
        run(2, "2026-02-22", 13.28),
        run(3, "2026-02-22", 13.27, hr=169),
    ]
    deduped, groups = dedupe(runs)
    assert len(deduped) == 2
    assert len(groups) == 1
    assert deduped[0].id == 1  # newest first, untouched


def test_dedupe_raises_effective_coverage():
    """The point of the exercise: the merged record carries data neither
    copy had alone, so coverage measured after dedupe is the honest number."""
    from rpg.activity import coverage

    runs = [
        run(1, "2026-02-22", 13.28),
        run(2, "2026-02-22", 13.27, hr=169, cadence=80.0),
        run(3, "2026-02-11", 4.55),
        run(4, "2026-02-11", 4.63, hr=162, cadence=80.5),
    ]
    assert coverage(runs).heartrate_pct == 50.0
    deduped, _ = dedupe(runs)
    assert coverage(deduped).heartrate_pct == 100.0


def test_empty_and_single_inputs_are_safe():
    assert find_duplicate_groups([]) == []
    assert find_duplicate_groups([run(1, "2026-02-22", 5.0)]) == []
    deduped, groups = dedupe([])
    assert deduped == [] and groups == []


def test_missing_distance_does_not_crash():
    a = summarize({"id": 1, "sport_type": "Run", "start_date_local": "2026-02-22T07:00:00Z"})
    b = run(2, "2026-02-22", 5.0)
    assert find_duplicate_groups([a, b]) == []


def test_descriptive_name_survives_the_merge():
    """The richer copy was auto-named 'Morning Run'; the other says it was a
    race. Losing that label would hide a maximal effort from VDOT."""
    runs = [
        run(1, "2026-02-22", 13.28, name="Malta Half Marathon"),
        run(2, "2026-02-22", 13.27, hr=169, name="Morning Run"),
    ]
    merged = find_duplicate_groups(runs)[0].merged()
    assert merged.name == "Malta Half Marathon"
    assert merged.average_heartrate == 169  # still took the richer data


def test_generic_name_does_not_displace_a_descriptive_one():
    runs = [
        run(1, "2026-02-11", 4.55, name="Evening Run"),
        run(2, "2026-02-11", 4.63, hr=162, name="Nike Run Club: Five Mile Run"),
    ]
    assert find_duplicate_groups(runs)[0].merged().name == "Nike Run Club: Five Mile Run"


def test_is_generic_name_classification():
    from rpg.dedupe import is_generic_name

    for generic in ("Morning Run", "Afternoon Run", "Evening Run", "Lunch Run", "", None):
        assert is_generic_name(generic), generic
    for real in ("Malta Half Marathon", "Nike Run Club: 4K Tempo Run", "Afternoon Recovery"):
        assert not is_generic_name(real), real
