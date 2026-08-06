"""Tempo matching against a 160 spm cadence target."""

import pytest

from rpg.cadence import (
    DEFAULT_TARGET_SPM,
    match_tempo,
    rank,
    select_playlist,
    usable_bpm_ranges,
)


def test_exact_match_scores_zero_distance():
    match = match_tempo(160.0)
    assert match.multiplier == 1.0
    assert match.distance == 0.0
    assert match.within_band


def test_half_time_track_matches_the_target():
    """80 BPM at two steps per beat is 160 spm — the common case, since almost
    no popular music sits at 160."""
    match = match_tempo(80.0)
    assert match.multiplier == 2.0
    assert match.effective_bpm == 160.0
    assert match.distance == 0.0
    assert match.is_half_time


def test_double_time_track_matches_the_target():
    match = match_tempo(320.0)
    assert match.multiplier == 0.5
    assert match.effective_bpm == 160.0
    assert not match.is_half_time


def test_one_to_one_wins_a_tie():
    """A track at the target exactly should be used straight, not halved."""
    assert match_tempo(160.0).multiplier == 1.0


def test_track_between_multiples_is_out_of_band():
    """120 BPM: 120 is 40 off, 240 is 80 off, 60 is 100 off. Nothing works."""
    match = match_tempo(120.0)
    assert not match.within_band
    assert match.distance == 40.0


def test_near_miss_stays_in_band():
    assert match_tempo(78.0).within_band  # 156 effective, 4 off
    assert not match_tempo(74.0).within_band  # 148 effective, 12 off


def test_custom_target_and_tolerance():
    match = match_tempo(85.0, target_spm=170.0, tolerance=2.0)
    assert match.effective_bpm == 170.0
    assert match.within_band


def test_describe_explains_half_time():
    assert "half-time" in match_tempo(80.0).describe()
    assert "BPM" in match_tempo(160.0).describe()
    assert "double-time" in match_tempo(320.0).describe()


def test_rejects_nonsense_bpm():
    with pytest.raises(ValueError):
        match_tempo(0)


def test_usable_bpm_ranges_cover_every_multiplier():
    ranges = usable_bpm_ranges(160.0, 5.0)
    assert (77.5, 82.5) in ranges  # half time
    assert (155.0, 165.0) in ranges  # one to one
    assert (310.0, 330.0) in ranges  # double time


def test_ranking_orders_by_fit():
    ranked = rank([("a", 120.0), ("b", 80.0), ("c", 78.0)])
    assert [track for track, _ in ranked] == ["b", "c", "a"]


def test_ranking_skips_missing_bpm():
    assert [t for t, _ in rank([("a", 0), ("b", 80.0), ("c", None)])] == ["b"]


def test_playlist_prefers_in_band_tracks():
    candidates = [("a", 80.0), ("b", 159.0), ("c", 120.0), ("d", 200.0)]
    selected, _ = select_playlist(candidates, count=2)
    assert {track for track, _ in selected} == {"a", "b"}


def test_playlist_reports_when_it_had_to_reach_outside_the_band():
    """A silently loosened tolerance is how a pace-synced playlist stops being
    pace-synced."""
    selected, warnings = select_playlist([("a", 80.0), ("b", 120.0)], count=2)
    assert len(selected) == 2
    assert any("will not match your cadence" in w for w in warnings)


def test_playlist_reports_a_short_candidate_pool():
    _, warnings = select_playlist([("a", 80.0)], count=10)
    assert any("wanted 10" in w for w in warnings)


def test_playlist_explains_half_time_selections():
    _, warnings = select_playlist([("a", 80.0), ("b", 79.0)], count=2)
    assert any("half-time" in w for w in warnings)


def test_playlist_of_perfect_matches_warns_only_about_half_time():
    selected, warnings = select_playlist([("a", 160.0), ("b", 158.0)], count=2)
    assert len(selected) == 2
    assert warnings == []


def test_default_target_matches_the_measured_cadence():
    """Finding 5: mean 159.5 spm across 10 runs, sd 2.5."""
    assert DEFAULT_TARGET_SPM == 160.0


# -- cadence model: overturned by a wider history ---------------------------

from rpg.activity import METERS_PER_MILE, summarize  # noqa: E402
from rpg.cadence import (  # noqa: E402
    REFERENCE_MODEL,
    fit_cadence_model,
    target_cadence,
)

# The real 37 cadence-carrying runs, 7:15 to 11:09/mi.
REAL_POINTS = [
    (435, 178), (463, 171), (483, 166), (494, 170), (527, 163), (542, 156),
    (544, 160), (545, 155), (546, 158), (550, 157), (553, 156), (558, 160),
    (564, 160), (573, 158), (574, 163), (575, 160), (579, 165), (581, 161),
    (583, 159), (583, 161), (584, 159), (584, 167), (585, 164), (591, 162),
    (600, 160), (604, 159), (611, 163), (614, 159), (614, 163), (614, 158),
    (618, 159), (621, 163), (624, 157), (629, 164), (637, 156), (637, 159),
    (669, 162),
]


def _run(pace_seconds, spm, id=0):
    speed = METERS_PER_MILE / pace_seconds
    return summarize(
        {
            "id": id,
            "sport_type": "Run",
            "start_date_local": "2025-07-01T07:00:00Z",
            "distance": 5000.0,
            "moving_time": int(5000 / speed),
            "average_speed": speed,
            "average_cadence": spm / 2,  # Strava reports one leg
            "has_heartrate": False,
        }
    )


def test_fit_recovers_the_relationship_from_real_data():
    """10 runs in a narrow band said r = -0.12 and 'use a constant'. 37 runs
    across 3.9 min/mi say otherwise."""
    model = fit_cadence_model([_run(p, c, i) for i, (p, c) in enumerate(REAL_POINTS)])
    assert model is not None
    assert model.r == pytest.approx(0.605, abs=0.02)
    assert model.n == 37
    assert 0.3 < model.explains < 0.45


def test_fitted_model_predicts_faster_paces_at_higher_cadence():
    model = fit_cadence_model([_run(p, c, i) for i, (p, c) in enumerate(REAL_POINTS)])
    assert model.cadence_at_pace(8 * 60) > model.cadence_at_pace(10 * 60)


def test_model_is_clamped_to_observed_speeds():
    """Nothing in the data says what happens at 5:00/mi, so don't invent it."""
    model = fit_cadence_model([_run(p, c, i) for i, (p, c) in enumerate(REAL_POINTS)])
    assert model.cadence_at_pace(5 * 60) == pytest.approx(model.cadence_at_pace(7 * 60 + 15))
    assert model.cadence_at_pace(20 * 60) == pytest.approx(model.cadence_at_pace(11 * 60 + 9))


def test_fit_refuses_a_narrow_clustered_sample():
    """The original 10 points: too narrow to support a slope."""
    narrow = [(527, 163), (550, 157), (553, 156), (583, 159), (583, 161),
              (584, 159), (591, 162), (611, 163), (618, 159), (637, 156)]
    assert fit_cadence_model([_run(p, c, i) for i, (p, c) in enumerate(narrow)]) is None


def test_fit_needs_enough_points():
    assert fit_cadence_model([_run(600, 160, 1), _run(500, 170, 2)]) is None


def test_fit_ignores_runs_without_cadence():
    runs = [_run(p, c, i) for i, (p, c) in enumerate(REAL_POINTS)]
    runs.append(summarize({"id": 99, "sport_type": "Run", "distance": 5000,
                           "moving_time": 1800, "average_speed": 2.8}))
    assert fit_cadence_model(runs).n == 37


def test_reference_model_matches_the_measured_fit():
    assert REFERENCE_MODEL.cadence_at_pace(10 * 60) == pytest.approx(160, abs=1.5)
    assert REFERENCE_MODEL.cadence_at_pace(8 * 60) == pytest.approx(167, abs=1.5)


def test_target_cadence_shifts_with_prescribed_pace():
    """The point of the whole project: an easy run and a tempo run should not
    get the same playlist."""
    easy = target_cadence(10 * 60)
    tempo = target_cadence(8 * 60)
    assert tempo > easy + 4


def test_target_cadence_falls_back_to_the_constant():
    assert target_cadence(None) == 160.0
