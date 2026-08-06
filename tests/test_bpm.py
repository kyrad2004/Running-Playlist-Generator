"""Track matching without ISRC — the failure modes that produce wrong BPMs."""

import pytest

from rpg.bpm import (
    BpmResult,
    CachingProvider,
    StubProvider,
    build_query,
    clean_title,
    normalize,
    primary_artist,
    query_from_spotify_track,
    score_match,
    variant_markers,
)


# -- normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Blinding Lights", "blinding lights"),
        ("Blinding Lights - Remastered 2021", "blinding lights"),
        ("Blinding Lights - 2021 Remaster", "blinding lights"),
        ("Levitating (feat. DaBaby)", "levitating"),
        ("Levitating [feat. DaBaby]", "levitating"),
        ("Song Title - Radio Edit", "song title"),
        ("Song Title - Single Version", "song title"),
        ("Song Title - Album Version", "song title"),
        ("Song Title - Bonus Track", "song title"),
        ("Song Title (Deluxe Edition)", "song title"),
        ("Café del Mar", "cafe del mar"),
        ("Don't Stop Me Now", "don t stop me now"),
        ("HUMBLE.", "humble"),
        ("Song   with    spaces", "song with spaces"),
    ],
)
def test_clean_title_strips_decorations(raw, expected):
    assert clean_title(raw) == expected


def test_clean_title_handles_stacked_suffixes():
    """Spotify stacks these: 'Song - Live - Remastered 2011'."""
    assert clean_title("Song - Live - Remastered 2011") == "song"


def test_clean_title_leaves_a_bare_title_alone():
    assert clean_title("Levitating") == "levitating"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dua Lipa", "dua lipa"),
        ("Dua Lipa, DaBaby", "dua lipa"),
        ("Dua Lipa & DaBaby", "dua lipa"),
        ("Dua Lipa feat. DaBaby", "dua lipa"),
        ("Dua Lipa ft. DaBaby", "dua lipa"),
        ("Dua Lipa featuring DaBaby", "dua lipa"),
        ("Calvin Harris with Rihanna", "calvin harris"),
        ("Jack Ü x Justin Bieber", "jack u"),
        ("Beyoncé", "beyonce"),
    ],
)
def test_primary_artist_drops_collaborators(raw, expected):
    assert primary_artist(raw) == expected


def test_normalize_is_idempotent():
    once = normalize("Café — del Mar!")
    assert normalize(once) == once


# -- variant detection ------------------------------------------------------


def test_variant_markers_detect_different_recordings():
    assert "live" in variant_markers("Song - Live at Wembley")
    assert "remix" in variant_markers("Song (Kaytranada Remix)")
    assert "acoustic" in variant_markers("Song - Acoustic")
    assert "sped_up" in variant_markers("Song - Sped Up")
    assert variant_markers("Blinding Lights") == []


def test_query_records_stripped_variants():
    query = build_query("The Killers", "Mr. Brightside - Live")
    assert query.title == "mr brightside"
    assert query.is_variant
    assert "live" in query.variants


def test_query_cache_key_is_stable_across_decorations():
    """The whole point: these three should hit the same cache entry."""
    a = build_query("Dua Lipa", "Levitating")
    b = build_query("Dua Lipa, DaBaby", "Levitating (feat. DaBaby)")
    c = build_query("Dua Lipa", "Levitating - Radio Edit")
    assert a.cache_key == b.cache_key == c.cache_key


def test_query_from_spotify_track():
    track = {
        "name": "Levitating (feat. DaBaby)",
        "artists": [{"name": "Dua Lipa"}, {"name": "DaBaby"}],
    }
    query = query_from_spotify_track(track)
    assert query.artist == "dua lipa"
    assert query.title == "levitating"


def test_query_from_spotify_track_without_artists():
    assert query_from_spotify_track({"name": "Something"}).artist == ""


# -- match scoring: the wrong-BPM failure modes -----------------------------


def test_exact_match_is_confident():
    query = build_query("Dua Lipa", "Levitating")
    confidence, notes = score_match(query, "Dua Lipa", "Levitating")
    assert confidence == pytest.approx(1.0)
    assert notes == []


def test_cover_by_a_different_artist_is_rejected():
    """Same title, wrong artist — a cover, routinely at a different tempo.
    This is the case that silently poisons a playlist."""
    query = build_query("The Killers", "Mr. Brightside")
    confidence, notes = score_match(query, "Some Covers Band", "Mr. Brightside")
    assert confidence < 0.6
    assert any("artist mismatch" in n for n in notes)


def test_live_recording_is_downgraded_even_on_a_perfect_string_match():
    """A live cut matched to the studio record is a real tempo risk."""
    studio = build_query("The Killers", "Mr. Brightside")
    live = build_query("The Killers", "Mr. Brightside - Live")

    studio_confidence, _ = score_match(studio, "The Killers", "Mr. Brightside")
    live_confidence, notes = score_match(live, "The Killers", "Mr. Brightside")

    assert live_confidence < studio_confidence
    assert any("variant" in n for n in notes)


def test_near_miss_title_lowers_confidence_without_rejecting():
    query = build_query("Dua Lipa", "Levitating")
    confidence, _ = score_match(query, "Dua Lipa", "Levitate")
    assert 0.6 < confidence < 1.0


def test_result_usability_thresholds():
    assert BpmResult(120, "x", confidence=0.9).is_confident
    assert BpmResult(120, "x", confidence=0.7).is_usable
    assert not BpmResult(120, "x", confidence=0.7).is_confident
    assert not BpmResult(120, "x", confidence=0.4).is_usable


# -- providers --------------------------------------------------------------


def test_stub_provider_matches_through_decorations():
    provider = StubProvider({("Dua Lipa", "Levitating"): 103.0})
    result = provider.bpm_for(build_query("Dua Lipa, DaBaby", "Levitating (feat. DaBaby)"))
    assert result is not None
    assert result.bpm == 103.0
    assert result.is_confident


def test_stub_provider_returns_none_rather_than_guessing():
    provider = StubProvider({("Dua Lipa", "Levitating"): 103.0})
    assert provider.bpm_for(build_query("Radiohead", "Creep")) is None


def test_caching_provider_looks_up_once(tmp_path):
    provider = StubProvider({("Dua Lipa", "Levitating"): 103.0})
    cached = CachingProvider(provider, tmp_path / "bpm.json")
    query = build_query("Dua Lipa", "Levitating")

    assert cached.bpm_for(query).bpm == 103.0
    assert cached.bpm_for(query).bpm == 103.0
    assert cached.hits == 1 and cached.misses == 1


def test_caching_provider_remembers_misses(tmp_path):
    """A track no database knows stays unknown; re-asking wastes rate limit."""
    cached = CachingProvider(StubProvider({}), tmp_path / "bpm.json")
    query = build_query("Nobody", "Nothing")

    assert cached.bpm_for(query) is None
    assert cached.bpm_for(query) is None
    assert cached.hits == 1


def test_caching_provider_persists_across_instances(tmp_path):
    path = tmp_path / "bpm.json"
    query = build_query("Dua Lipa", "Levitating")

    first = CachingProvider(StubProvider({("Dua Lipa", "Levitating"): 103.0}), path)
    first.bpm_for(query)
    first.save()

    # Empty provider — anything returned must have come from disk.
    second = CachingProvider(StubProvider({}), path)
    assert second.bpm_for(query).bpm == 103.0
    assert second.misses == 0


def test_caching_provider_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "bpm.json"
    path.write_text("{not json")
    cached = CachingProvider(StubProvider({("A", "B"): 100.0}), path)
    assert cached.bpm_for(build_query("A", "B")).bpm == 100.0
