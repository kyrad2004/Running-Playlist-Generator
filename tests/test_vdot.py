"""VDOT validated against published Daniels tables.

The formulas are easy to transcribe wrong in ways that still produce
plausible-looking numbers, so the anchors here are values from Daniels'
published VDOT tables rather than outputs of this implementation.
"""

from datetime import date

import pytest

from rpg.activity import METERS_PER_MILE, summarize
from rpg.vdot import (
    STANDARD_DISTANCES,
    Effort,
    VdotError,
    estimate_from_runs,
    fraction_of_vo2max,
    pace_at_intensity,
    predict_race_time,
    score_efforts,
    training_paces,
    vdot_from_effort,
    velocity_at_vo2,
    vo2_at_velocity,
)

HALF_MARATHON = 21097.5


def mmss(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


# -- the curves themselves --------------------------------------------------


def test_vo2_at_velocity_known_point():
    """268.1 m/min is roughly 6:00/mi; oxygen cost lands near 51.9 ml/kg/min."""
    assert vo2_at_velocity(268.1) == pytest.approx(51.9, abs=0.2)


def test_velocity_inverts_vo2_exactly():
    for v in (150.0, 200.0, 250.0, 320.0):
        assert velocity_at_vo2(vo2_at_velocity(v)) == pytest.approx(v, abs=1e-6)


def test_fraction_exceeds_one_for_short_efforts():
    """The curve crosses 1.0 at about 11 minutes and keeps rising below that.

    Not a bug: a 3-minute effort is run above VO2max velocity, paid for
    anaerobically. It does mean VDOT from very short efforts leans on the
    curve's extrapolated end, which is a reason to prefer longer anchors.
    """
    assert fraction_of_vo2max(3.0) > fraction_of_vo2max(6.0) > 1.0
    assert fraction_of_vo2max(11.03) == pytest.approx(1.0, abs=0.001)
    assert fraction_of_vo2max(12.0) < 1.0


def test_fraction_decays_toward_base_for_long_efforts():
    assert fraction_of_vo2max(180.0) < 0.83
    assert fraction_of_vo2max(180.0) > 0.80


def test_fraction_is_monotonically_decreasing():
    values = [fraction_of_vo2max(t) for t in range(5, 240, 5)]
    assert all(a > b for a, b in zip(values, values[1:]))


def test_fraction_rejects_nonpositive_duration():
    with pytest.raises(VdotError):
        fraction_of_vo2max(0)


# -- VDOT from a race, against published table rows -------------------------


@pytest.mark.parametrize(
    "distance,time_seconds,expected_vdot",
    [
        (5000.0, 19 * 60 + 57, 50),  # Daniels: VDOT 50 runs 5K in 19:57
        (10000.0, 41 * 60 + 21, 50),  # ... 10K in 41:21
        (HALF_MARATHON, 91 * 60 + 35, 50),  # ... half in 1:31:35
        (42195.0, 190 * 60 + 49, 50),  # ... marathon in 3:10:49
        (5000.0, 24 * 60 + 8, 40),  # VDOT 40 runs 5K in 24:08
        (5000.0, 17 * 60 + 3, 60),  # VDOT 60 runs 5K in 17:03
    ],
)
def test_vdot_matches_published_race_times(distance, time_seconds, expected_vdot):
    assert vdot_from_effort(distance, time_seconds) == pytest.approx(expected_vdot, abs=0.3)


def test_vdot_is_consistent_across_distances():
    """One athlete's VDOT should be the same whichever race you compute it from."""
    from_5k = vdot_from_effort(5000.0, 19 * 60 + 57)
    from_marathon = vdot_from_effort(42195.0, 190 * 60 + 49)
    assert abs(from_5k - from_marathon) < 0.3


def test_faster_time_gives_higher_vdot():
    assert vdot_from_effort(5000.0, 1100) > vdot_from_effort(5000.0, 1300)


def test_vdot_rejects_nonsense_input():
    for meters, seconds in ((0, 100), (5000, 0), (-1, 100)):
        with pytest.raises(VdotError):
            vdot_from_effort(meters, seconds)


# -- training paces, against published table rows ---------------------------


def test_threshold_pace_matches_published_table():
    """Daniels lists VDOT 50 threshold pace at 6:51/mi."""
    assert mmss(training_paces(50).threshold) == "6:51"


def test_marathon_pace_is_close_to_published():
    # Table says 7:17/mi for VDOT 50; the zone is a band, so allow a few seconds.
    assert abs(training_paces(50).marathon - (7 * 60 + 17)) < 12


def test_paces_are_ordered_by_intensity():
    p = training_paces(45)
    assert p.easy_slow > p.easy_fast > p.marathon > p.threshold > p.interval > p.repetition


def test_higher_vdot_means_faster_paces():
    assert training_paces(60).threshold < training_paces(40).threshold


def test_pace_at_intensity_matches_training_paces():
    assert pace_at_intensity(50, 0.88) == pytest.approx(training_paces(50).threshold)


def test_training_paces_rejects_nonpositive_vdot():
    with pytest.raises(VdotError):
        training_paces(0)


# -- race prediction is the inverse of vdot_from_effort ---------------------


@pytest.mark.parametrize("distance", list(STANDARD_DISTANCES.values()))
def test_prediction_round_trips_through_vdot(distance):
    predicted = predict_race_time(48.0, distance)
    assert vdot_from_effort(distance, predicted) == pytest.approx(48.0, abs=0.05)


def test_prediction_matches_published_table():
    """VDOT 50 half marathon: 1:31:35."""
    assert predict_race_time(50.0, HALF_MARATHON) == pytest.approx(91 * 60 + 35, abs=30)


def test_fitter_runner_is_predicted_faster():
    assert predict_race_time(60.0, 5000.0) < predict_race_time(40.0, 5000.0)


# -- estimating from real history ------------------------------------------


def make_run(id, date_str, miles, pace_per_mile_seconds, name=""):
    seconds = miles * pace_per_mile_seconds
    return summarize(
        {
            "id": id,
            "name": name,
            "sport_type": "Run",
            "start_date_local": f"{date_str}T07:00:00Z",
            "distance": miles * METERS_PER_MILE,
            "moving_time": int(seconds),
            "average_speed": (miles * METERS_PER_MILE) / seconds,
            "has_heartrate": False,
        }
    )


TODAY = date(2026, 8, 6)


def test_malta_half_marathon_scores_a_plausible_vdot():
    """The real anchor effort: 13.27 mi at 9:32/mi."""
    vdot = vdot_from_effort(13.27 * METERS_PER_MILE, 13.27 * (9 * 60 + 32))
    assert 34 < vdot < 36


def test_easy_runs_score_below_a_race():
    """Why the estimate takes a max: a steady easy run understates fitness."""
    race = vdot_from_effort(13.27 * METERS_PER_MILE, 13.27 * (9 * 60 + 32))
    easy = vdot_from_effort(2.4 * METERS_PER_MILE, 2.4 * (9 * 60 + 0))
    assert easy < race


def test_short_runs_are_excluded_from_scoring():
    runs = [make_run(1, "2026-08-01", 0.5, 480), make_run(2, "2026-08-02", 3.0, 540)]
    assert [e.run.id for e in score_efforts(runs)] == [2]


def test_estimate_flags_a_stale_anchor():
    """The real situation: a strong February race, then five quiet months."""
    runs = [
        make_run(1, "2026-02-22", 13.27, 9 * 60 + 32, name="Malta Half Marathon"),
        make_run(2, "2026-08-02", 1.75, 8 * 60 + 55),
        make_run(3, "2026-07-19", 2.40, 9 * 60 + 0),
        make_run(4, "2026-05-16", 2.30, 9 * 60 + 36),
    ]
    estimate = estimate_from_runs(runs, today=TODAY)

    assert estimate.confidence == "stale"
    assert not estimate.usable
    assert estimate.age_days == 165
    assert "165 days old" in estimate.reason
    assert estimate.best.run.name == "Malta Half Marathon"


def test_stale_estimate_is_decayed_and_capped():
    runs = [
        make_run(1, "2026-02-22", 13.27, 9 * 60 + 32),
        make_run(2, "2026-08-02", 1.75, 8 * 60 + 55),
        make_run(3, "2026-07-19", 2.40, 9 * 60 + 0),
    ]
    estimate = estimate_from_runs(runs, today=TODAY)
    decayed = estimate.decayed_vdot()
    assert decayed < estimate.vdot
    assert decayed >= estimate.vdot * 0.80  # capped at MAX_DECAY


def test_recent_effort_with_volume_behind_it_is_fresh():
    runs = [make_run(i, f"2026-08-0{i}", 4.0, 8 * 60 + 30) for i in range(1, 6)]
    estimate = estimate_from_runs(runs, today=TODAY)
    assert estimate.confidence == "fresh"
    assert estimate.usable


def test_recent_effort_without_volume_is_still_flagged():
    """One hard run last week isn't evidence of sustained fitness."""
    runs = [
        make_run(1, "2026-08-01", 5.0, 8 * 60),
        make_run(2, "2026-07-30", 3.0, 9 * 60),
        make_run(3, "2026-07-28", 3.0, 9 * 60),
    ]
    estimate = estimate_from_runs(runs, today=TODAY, min_recent_runs=4)
    assert estimate.confidence == "stale"
    assert "too little to confirm" in estimate.reason


def test_too_few_efforts_returns_insufficient():
    runs = [make_run(1, "2026-08-01", 3.0, 540), make_run(2, "2026-08-02", 3.0, 540)]
    estimate = estimate_from_runs(runs, today=TODAY)
    assert estimate.confidence == "insufficient"
    assert estimate.vdot is None
    assert "onboarding" in estimate.reason


def test_empty_history_is_insufficient_not_a_crash():
    estimate = estimate_from_runs([], today=TODAY)
    assert estimate.confidence == "insufficient"
    assert estimate.vdot is None
    assert estimate.decayed_vdot() is None


def test_effort_handles_unparseable_date():
    run = summarize({"id": 1, "sport_type": "Run", "start_date_local": "not-a-date"})
    assert Effort(run, 40.0).age_days(TODAY) is None


def test_short_fast_run_outscoring_longer_efforts_is_flagged():
    """A 2.09 mi run scoring 6 VDOT above a half marathon is more likely a bad
    distance reading than a breakthrough."""
    runs = [
        make_run(1, "2026-03-04", 2.09, 7 * 60 + 26, name="Lunch Run"),
        make_run(2, "2026-02-22", 13.27, 9 * 60 + 32, name="Malta Half Marathon"),
        make_run(3, "2026-02-02", 10.05, 9 * 60 + 27),
    ]
    estimate = estimate_from_runs(runs, today=TODAY)
    assert estimate.outlier_warning is not None
    assert "GPS" in estimate.outlier_warning


def test_longer_best_effort_is_not_flagged_as_an_outlier():
    runs = [
        make_run(1, "2026-02-22", 13.27, 9 * 60 + 32, name="Malta Half Marathon"),
        make_run(2, "2026-02-18", 2.13, 9 * 60 + 30),
        make_run(3, "2026-02-02", 10.05, 10 * 60),
    ]
    assert estimate_from_runs(runs, today=TODAY).outlier_warning is None


def test_effective_vdot_is_decayed_when_stale():
    runs = [
        make_run(1, "2026-02-22", 13.27, 9 * 60 + 32),
        make_run(2, "2026-08-02", 1.75, 8 * 60 + 55),
        make_run(3, "2026-07-19", 2.40, 9 * 60 + 0),
    ]
    estimate = estimate_from_runs(runs, today=TODAY)
    assert estimate.confidence == "stale"
    assert estimate.effective_vdot() == estimate.decayed_vdot()
    assert estimate.effective_vdot() < estimate.vdot


def test_effective_vdot_is_raw_when_fresh():
    runs = [make_run(i, f"2026-08-0{i}", 4.0, 8 * 60 + 30) for i in range(1, 6)]
    estimate = estimate_from_runs(runs, today=TODAY)
    assert estimate.confidence == "fresh"
    assert estimate.effective_vdot() == estimate.vdot


def test_easy_band_bounds_are_ordered_as_seconds():
    """easy_slow is the larger number of seconds; the report must print the
    fast bound first or the range reads inverted."""
    paces = training_paces(41.0)
    assert paces.easy_slow > paces.easy_fast


# -- plausibility: does the estimate survive contact with habitual pace? ----


def test_implied_vdot_inverts_training_paces():
    from rpg.vdot import INTENSITY, implied_vdot_if_pace_were

    paces = training_paces(42.0)
    assert implied_vdot_if_pace_were(paces.easy_fast, INTENSITY["easy_fast"]) == pytest.approx(
        42.0, abs=0.01
    )


def test_plausibility_flags_an_estimate_that_is_too_low():
    """The real complaint: VDOT 34 prescribes ~11:30/mi easy, but these runs are
    all near 9:00/mi. Something is wrong with the estimate, not the runner."""
    from rpg.vdot import check_plausibility

    runs = [
        make_run(1, "2026-08-02", 1.75, 8 * 60 + 55),
        make_run(2, "2026-07-19", 2.40, 9 * 60 + 0),
        make_run(3, "2026-05-16", 2.30, 9 * 60 + 36),
        make_run(4, "2026-04-07", 2.76, 9 * 60 + 0),
    ]
    check = check_plausibility(34.1, runs)
    assert check.looks_low
    assert check.implied_low > 40
    assert "too low" in check.message


def test_plausibility_accepts_a_consistent_estimate():
    """Easy runs actually run at easy pace — nothing to flag."""
    from rpg.vdot import check_plausibility

    paces = training_paces(42.0)
    easy = (paces.easy_fast + paces.easy_slow) / 2
    runs = [make_run(i, f"2026-08-0{i}", 4.0, easy) for i in range(1, 5)]
    check = check_plausibility(42.0, runs)
    assert not check.looks_low
    assert check.message is None


def test_plausibility_handles_missing_input():
    from rpg.vdot import check_plausibility

    assert not check_plausibility(None, []).looks_low
    assert not check_plausibility(40.0, []).looks_low


def test_decay_is_gentle_enough_to_be_defensible():
    """4%/month capped at 20% took a 34.8 anchor to 28.5 and produced paces a
    real runner rejected on sight. Keep the haircut modest."""
    runs = [
        make_run(1, "2026-02-22", 13.27, 9 * 60 + 32),
        make_run(2, "2026-08-02", 1.75, 8 * 60 + 55),
        make_run(3, "2026-07-19", 2.40, 9 * 60 + 0),
    ]
    estimate = estimate_from_runs(runs, today=TODAY)
    assert estimate.decayed_vdot() >= estimate.vdot * 0.92


def test_parse_race_accepts_common_formats():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from vdot_report import parse_race

    label, meters, seconds = parse_race("5K=22:30")
    assert label == "5K" and meters == 5000.0 and seconds == 1350

    _, _, long_seconds = parse_race("marathon=3:10:49")
    assert long_seconds == 3 * 3600 + 10 * 60 + 49

    assert parse_race("half marathon=1:45:00")[1] == 21097.5


def test_parse_race_rejects_bad_input():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from vdot_report import parse_race

    for bad in ("5K", "10 miles=40:00", "5K=1:2:3:4"):
        with pytest.raises(ValueError):
            parse_race(bad)
