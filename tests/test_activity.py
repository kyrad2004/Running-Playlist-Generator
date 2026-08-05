from rpg.activity import (
    coverage,
    format_pace,
    is_run,
    mps_to_seconds_per_km,
    mps_to_seconds_per_mile,
    steps_per_minute,
    summarize,
)

# A realistic Strava summary payload: ~5 miles at ~8:00/mi with HR and cadence.
SAMPLE = {
    "id": 1234567890,
    "name": "Morning Run",
    "sport_type": "Run",
    "type": "Run",
    "start_date_local": "2026-08-01T07:12:00Z",
    "distance": 8046.72,
    "moving_time": 2400,
    "elapsed_time": 2460,
    "average_speed": 3.3528,  # m/s == 8:00/mi
    "max_speed": 4.5,
    "has_heartrate": True,
    "average_heartrate": 152.0,
    "max_heartrate": 171.0,
    "average_cadence": 86.0,  # one leg -> 172 spm
    "total_elevation_gain": 42.0,
}


def test_is_run_accepts_run_variants():
    assert is_run({"sport_type": "Run"})
    assert is_run({"sport_type": "TrailRun"})
    assert is_run({"sport_type": "VirtualRun"})
    assert not is_run({"sport_type": "Ride"})


def test_is_run_falls_back_to_type_when_sport_type_absent():
    assert is_run({"type": "Run"})


def test_pace_conversions():
    assert round(mps_to_seconds_per_mile(3.3528)) == 480  # 8:00/mi
    assert round(mps_to_seconds_per_km(3.3528)) == 298  # 4:58/km


def test_pace_conversion_handles_missing_speed():
    assert mps_to_seconds_per_mile(None) is None
    assert mps_to_seconds_per_km(0) is None


def test_format_pace():
    assert format_pace(480) == "8:00"
    assert format_pace(485) == "8:05"
    assert format_pace(None) == "—"
    assert format_pace(0) == "—"


def test_cadence_is_doubled_to_true_step_rate():
    """Strava reports one leg. Missing this halves every BPM target."""
    assert steps_per_minute(86.0) == 172.0
    assert steps_per_minute(None) is None


def test_summarize_extracts_and_derives():
    run = summarize(SAMPLE)
    assert run.id == 1234567890
    assert run.has_heartrate
    assert run.has_cadence
    assert run.steps_per_minute == 172.0
    assert round(run.distance_miles, 2) == 5.00
    assert format_pace(run.pace_per_mile_s) == "8:00"


def test_summarize_tolerates_missing_optional_fields():
    run = summarize({"id": 1, "sport_type": "Run"})
    assert not run.has_heartrate
    assert not run.has_cadence
    assert run.pace_per_mile_s is None
    assert run.distance_km is None
    assert run.steps_per_minute is None


def test_coverage_counts_and_percentages():
    runs = [
        summarize(SAMPLE),
        summarize({**SAMPLE, "id": 2, "has_heartrate": False, "average_heartrate": None}),
        summarize({**SAMPLE, "id": 3, "average_cadence": None}),
        summarize({"id": 4, "sport_type": "Run"}),
    ]
    cov = coverage(runs)
    assert cov.total_runs == 4
    assert cov.with_heartrate == 2
    assert cov.with_cadence == 2
    assert cov.with_speed == 3
    assert cov.heartrate_pct == 50.0


def test_coverage_of_empty_list_does_not_divide_by_zero():
    cov = coverage([])
    assert cov.total_runs == 0
    assert cov.heartrate_pct == 0.0
