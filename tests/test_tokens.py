import json
import stat
import time

from rpg.tokens import TokenSet, TokenStore


def test_expired_when_no_access_token():
    assert TokenSet(access_token="").expired()


def test_expired_uses_leeway():
    # 60s of life left, but the 120s leeway means we should refresh now rather
    # than start a request that dies in flight.
    tokens = TokenSet(access_token="a", expires_at=time.time() + 60)
    assert tokens.expired(leeway_seconds=120)
    assert not tokens.expired(leeway_seconds=10)


def test_not_expired_without_expiry():
    assert not TokenSet(access_token="a", expires_at=None).expired()


def test_spotify_response_converts_expires_in_to_absolute():
    before = time.time()
    tokens = TokenSet.from_spotify_response(
        {"access_token": "new", "expires_in": 3600, "token_type": "Bearer"}
    )
    assert tokens.access_token == "new"
    assert before + 3590 <= tokens.expires_at <= time.time() + 3600


def test_spotify_refresh_keeps_previous_refresh_token():
    """Spotify omits refresh_token on refresh; dropping it would lock us out."""
    previous = TokenSet(access_token="old", refresh_token="keep-me", scope="a b")
    refreshed = TokenSet.from_spotify_response(
        {"access_token": "new", "expires_in": 3600}, previous=previous
    )
    assert refreshed.refresh_token == "keep-me"
    assert refreshed.scope == "a b"


def test_strava_response_uses_absolute_expires_at():
    tokens = TokenSet.from_strava_response(
        {"access_token": "a", "refresh_token": "r", "expires_at": 1893456000}
    )
    assert tokens.expires_at == 1893456000


def test_strava_rotated_refresh_token_replaces_old_one():
    previous = TokenSet(access_token="old", refresh_token="old-refresh")
    refreshed = TokenSet.from_strava_response(
        {"access_token": "new", "refresh_token": "new-refresh", "expires_at": 1893456000},
        previous=previous,
    )
    assert refreshed.refresh_token == "new-refresh"


def test_from_dict_ignores_unknown_fields():
    tokens = TokenSet.from_dict({"access_token": "a", "surprise_field": 1})
    assert tokens.access_token == "a"


def test_store_roundtrip(tmp_path):
    store = TokenStore("spotify", directory=tmp_path / "toks")
    assert store.load() is None

    store.save(TokenSet(access_token="a", refresh_token="r", expires_at=123.0))
    loaded = store.load()
    assert loaded.access_token == "a"
    assert loaded.refresh_token == "r"
    assert loaded.expires_at == 123.0


def test_store_writes_owner_only_permissions(tmp_path):
    store = TokenStore("strava", directory=tmp_path / "toks")
    store.save(TokenSet(access_token="secret"))
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, expected 0o600"


def test_store_leaves_no_temp_file(tmp_path):
    store = TokenStore("spotify", directory=tmp_path / "toks")
    store.save(TokenSet(access_token="a"))
    assert list(store.directory.glob("*.tmp")) == []


def test_store_handles_corrupt_file(tmp_path):
    store = TokenStore("spotify", directory=tmp_path / "toks")
    store.directory.mkdir(parents=True)
    store.path.write_text("{not json")
    assert store.load() is None


def test_store_clear(tmp_path):
    store = TokenStore("spotify", directory=tmp_path / "toks")
    store.save(TokenSet(access_token="a"))
    store.clear()
    assert not store.exists()
    store.clear()  # idempotent


def test_saved_payload_is_plain_json(tmp_path):
    store = TokenStore("spotify", directory=tmp_path / "toks")
    store.save(TokenSet(access_token="a", scope="x"))
    data = json.loads(store.path.read_text())
    assert data["access_token"] == "a"
    assert data["scope"] == "x"
