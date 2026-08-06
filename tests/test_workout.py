"""Classifying interval and hill sessions, whose averages describe nothing."""

from rpg.activity import METERS_PER_MILE, summarize
from rpg.workout import (
    HILL,
    INTERVAL,
    RACE,
    STEADY,
    TEMPO,
    annotate,
    annotate_all,
    classify,
    is_steady,
    steady_runs,
)


def make(name="Afternoon Run", miles=4.0, pace=540, elevation=20.0, id=1):
    seconds = miles * pace
    return summarize(
        {
            "id": id,
            "name": name,
            "sport_type": "Run",
            "start_date_local": "2025-07-19T07:00:00Z",
            "distance": miles * METERS_PER_MILE,
            "moving_time": int(seconds),
            "average_speed": (miles * METERS_PER_MILE) / seconds,
            "total_elevation_gain": elevation,
            "has_heartrate": False,
        }
    )


def detail_with_splits(speeds):
    return {"splits_standard": [{"average_speed": s} for s in speeds]}


# -- name signals -----------------------------------------------------------


def test_detects_intervals_from_the_name():
    for name in (
        "Nike Run Club: Speed Run",
        "Track intervals",
        "6x800m repeats",
        "Fartlek session",
        "Sprint work",
    ):
        assert classify(make(name=name)).kind == INTERVAL, name


def test_detects_hills_from_the_name():
    assert classify(make(name="Hill repeats")).kind == HILL
    assert classify(make(name="Incline session")).kind == HILL


def test_detects_tempo_from_the_name():
    assert classify(make(name="Nike Run Club: 4K Tempo Run")).kind == TEMPO


def test_detects_races_from_the_name():
    assert classify(make(name="Malta Half Marathon")).kind == RACE
    assert classify(make(name="5K Time Trial")).kind == RACE


def test_race_practice_is_not_treated_as_a_race():
    """'4.5mi Race Practice Long Run' is training, not a race result."""
    assert classify(make(name="4.5mi Race Practice Long Run")).kind != RACE


def test_generic_name_falls_back_to_steady():
    result = classify(make(name="Afternoon Run"))
    assert result.kind == STEADY
    assert result.confidence < 0.5  # a guess, and says so


# -- elevation signal -------------------------------------------------------


def test_heavy_climbing_reads_as_a_hill_session():
    result = classify(make(name="Evening Run", miles=4.0, elevation=180.0))
    assert result.kind == HILL
    assert any("climb" in s for s in result.signals)


def test_rolling_terrain_is_not_a_hill_workout():
    assert classify(make(name="Evening Run", miles=4.0, elevation=60.0)).kind == STEADY


# -- split variance, the decisive signal ------------------------------------


def test_split_variance_identifies_intervals_regardless_of_name():
    """Alternating reps and recoveries, on a run called 'Afternoon Run'."""
    detail = detail_with_splits([4.4, 2.8, 4.5, 2.7, 4.3])
    result = classify(make(name="Afternoon Run"), detail)
    assert result.kind == INTERVAL
    assert result.confidence >= 0.9


def test_even_splits_confirm_a_steady_run():
    detail = detail_with_splits([3.0, 3.02, 2.98, 3.01])
    result = classify(make(name="Afternoon Run"), detail)
    assert result.kind == STEADY
    assert result.confidence >= 0.8


def test_measurement_overrides_a_misleading_name():
    """Named like intervals, ran flat — trust the splits."""
    detail = detail_with_splits([3.0, 3.01, 2.99, 3.0])
    result = classify(make(name="Speed Run"), detail)
    assert result.kind == STEADY
    assert any("splits are even" in s for s in result.signals)


def test_too_few_splits_to_judge():
    result = classify(make(name="Afternoon Run"), detail_with_splits([3.0, 3.1]))
    assert result.kind == STEADY  # falls back, doesn't crash


# -- steadiness is what downstream code consumes ----------------------------


def test_intervals_and_hills_are_not_steady():
    assert not classify(make(name="Track intervals")).steady
    assert not classify(make(name="Hill repeats")).steady


def test_races_and_tempos_are_steady():
    """A race is a sustained maximal effort — the best VDOT anchor there is."""
    assert classify(make(name="Malta Half Marathon")).steady
    assert classify(make(name="Tempo Run")).steady


def test_annotate_marks_the_run():
    run = make(name="Track intervals")
    annotate(run)
    assert run.raw["_workout_kind"] == INTERVAL
    assert run.raw["_workout_steady"] is False
    assert not is_steady(run)


def test_unannotated_runs_default_to_steady():
    """Nothing is silently dropped just because classification hasn't run."""
    assert is_steady(make())


def test_annotate_all_counts_by_kind():
    runs = [
        make(name="Track intervals", id=1),
        make(name="Hill repeats", id=2),
        make(name="Afternoon Run", id=3),
    ]
    counts = annotate_all(runs)
    assert counts[INTERVAL] == 1 and counts[HILL] == 1 and counts[STEADY] == 1
    assert len(steady_runs(runs)) == 1


# -- the reason this exists -------------------------------------------------


def test_interval_session_is_excluded_from_vdot():
    """Its average pace blends reps with recoveries — not a sustained effort."""
    from rpg.vdot import score_efforts

    steady = make(name="Afternoon Run", miles=6.0, pace=540, id=1)
    intervals = make(name="Nike Run Club: Speed Run", miles=4.0, pace=435, id=2)
    annotate_all([steady, intervals])

    assert [e.run.id for e in score_efforts([steady, intervals])] == [1]


def test_interval_session_is_excluded_from_the_cadence_fit():
    from rpg.cadence import fit_cadence_model

    runs = []
    for i, (pace, spm) in enumerate(
        [(542, 156), (550, 157), (573, 158), (583, 159), (591, 162),
         (604, 159), (614, 159), (637, 156), (669, 162)]
    ):
        run = make(name="Afternoon Run", miles=4.0, pace=pace, id=i)
        run.raw["average_cadence"] = spm / 2
        run.average_cadence_raw = spm / 2
        runs.append(run)

    fast = make(name="Speed Run", miles=4.0, pace=435, id=99)
    fast.average_cadence_raw = 89.0
    annotate_all(runs + [fast])

    with_intervals = fit_cadence_model(runs + [fast])
    without = fit_cadence_model(runs)
    # The interval session is dropped, so both fits see the same points.
    assert (with_intervals is None) == (without is None)
    if with_intervals and without:
        assert with_intervals.n == without.n
