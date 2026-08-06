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
