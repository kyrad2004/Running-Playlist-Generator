"""Auth behaviour that only shows up against the live API — pinned with mocks."""

from unittest.mock import MagicMock, patch

import pytest

from rpg.config import ConfigError, SpotifyConfig, StravaConfig, spotify_config
from rpg.oauth import OAuthError
from rpg.spotify import SpotifyAuth, SpotifyClient
from rpg.strava import StravaAuth
from rpg.tokens import TokenSet, TokenStore

SPOTIFY_CONFIG = SpotifyConfig(client_id="cid", redirect_uri="http://127.0.0.1:8888/callback")
STRAVA_CONFIG = StravaConfig(
    client_id="1", client_secret="s", redirect_uri="http://localhost:8899/exchange_token"
)


def json_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.content = b"{}"
    return response


# -- config -----------------------------------------------------------------


def test_spotify_config_rejects_localhost_redirect(monkeypatch):
    """Spotify requires a literal loopback IP; 'localhost' fails at authorize time
    with an opaque error, so we catch it during config load instead."""
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")
    with pytest.raises(ConfigError, match="localhost"):
        spotify_config()


def test_spotify_config_accepts_loopback_ip(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    assert spotify_config().redirect_uri == "http://127.0.0.1:8888/callback"


def test_missing_client_id_raises_with_actionable_hint(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "")
    with pytest.raises(ConfigError, match="developer.spotify.com"):
        spotify_config()


def test_scope_string_separators():
    # Spotify wants spaces, Strava wants commas. Swapping them silently grants
    # nothing on Strava.
    assert " " in SPOTIFY_CONFIG.scope_string
    assert "," in STRAVA_CONFIG.scope_string


# -- spotify ----------------------------------------------------------------


def test_spotify_refresh_persists_new_token(tmp_path):
    store = TokenStore("spotify", directory=tmp_path)
    store.save(TokenSet(access_token="old", refresh_token="r1", expires_at=0))
    auth = SpotifyAuth(config=SPOTIFY_CONFIG, store=store)

    with patch("rpg.spotify.request", return_value=json_response(
        {"access_token": "new", "expires_in": 3600}
    )):
        token = auth.access_token()

    assert token == "new"
    assert store.load().access_token == "new"
    assert store.load().refresh_token == "r1"  # carried over, not dropped


def test_spotify_uses_cached_token_without_network(tmp_path):
    import time

    store = TokenStore("spotify", directory=tmp_path)
    store.save(TokenSet(access_token="live", refresh_token="r", expires_at=time.time() + 3600))
    auth = SpotifyAuth(config=SPOTIFY_CONFIG, store=store)

    with patch("rpg.spotify.request") as req:
        assert auth.access_token() == "live"
    req.assert_not_called()


def test_spotify_without_tokens_points_at_the_auth_script(tmp_path):
    auth = SpotifyAuth(config=SPOTIFY_CONFIG, store=TokenStore("spotify", directory=tmp_path))
    with pytest.raises(OAuthError, match="auth_spotify.py"):
        auth.access_token()


def test_spotify_refresh_without_refresh_token_is_explicit(tmp_path):
    store = TokenStore("spotify", directory=tmp_path)
    auth = SpotifyAuth(config=SPOTIFY_CONFIG, store=store)
    with pytest.raises(OAuthError, match="re-run"):
        auth.refresh(TokenSet(access_token="a", refresh_token=None))


def test_search_limit_is_capped_at_ten(tmp_path):
    """Feb 2026 dropped the search cap from 50 to 10; asking for more 400s."""
    client = SpotifyClient.__new__(SpotifyClient)
    client.get = MagicMock(return_value={"tracks": {"items": []}})
    SpotifyClient.search_tracks(client, "x", limit=50)
    assert client.get.call_args.kwargs["limit"] == 10


def test_create_playlist_uses_me_endpoint(tmp_path):
    client = SpotifyClient.__new__(SpotifyClient)
    client.post = MagicMock(return_value={"id": "p1"})
    SpotifyClient.create_playlist(client, "Tempo Run")
    # POST /users/{id}/playlists was removed in Feb 2026.
    assert client.post.call_args.args[0] == "/me/playlists"


def test_add_items_uses_items_endpoint():
    client = SpotifyClient.__new__(SpotifyClient)
    client.post = MagicMock(return_value={})
    SpotifyClient.add_items(client, "p1", ["spotify:track:1"])
    # .../tracks was removed in Feb 2026.
    assert client.post.call_args.args[0] == "/playlists/p1/items"


def test_add_items_rejects_more_than_one_hundred_uris():
    client = SpotifyClient.__new__(SpotifyClient)
    client.post = MagicMock()
    with pytest.raises(ValueError, match="100"):
        SpotifyClient.add_items(client, "p1", [f"spotify:track:{i}" for i in range(101)])


# -- strava -----------------------------------------------------------------


def test_strava_refresh_persists_rotated_refresh_token(tmp_path):
    """Strava invalidates the old refresh token on every refresh."""
    store = TokenStore("strava", directory=tmp_path)
    store.save(TokenSet(access_token="old", refresh_token="r1", expires_at=0))
    auth = StravaAuth(config=STRAVA_CONFIG, store=store)

    with patch("rpg.strava.request", return_value=json_response(
        {"access_token": "new", "refresh_token": "r2", "expires_at": 9999999999}
    )):
        assert auth.access_token() == "new"

    assert store.load().refresh_token == "r2"


def test_strava_authorize_rejects_partial_scope_grant(tmp_path):
    """Strava's consent screen lets you untick activity:read_all, which yields a
    working token that silently hides private runs. Fail loudly instead."""
    auth = StravaAuth(config=STRAVA_CONFIG, store=TokenStore("strava", directory=tmp_path))
    callback = MagicMock(code="c", state="st", params={"scope": "read", "state": "st"})

    with patch("rpg.strava.generate_state", return_value="st"), patch(
        "rpg.strava.wait_for_callback", return_value=callback
    ), pytest.raises(OAuthError, match="activity:read_all"):
        auth.authorize_interactive(open_browser=False)


def test_strava_authorize_rejects_state_mismatch(tmp_path):
    auth = StravaAuth(config=STRAVA_CONFIG, store=TokenStore("strava", directory=tmp_path))
    callback = MagicMock(code="c", state="attacker", params={})

    with patch("rpg.strava.generate_state", return_value="mine"), patch(
        "rpg.strava.wait_for_callback", return_value=callback
    ), pytest.raises(OAuthError, match="State mismatch"):
        auth.authorize_interactive(open_browser=False)


def test_strava_authorize_saves_tokens_on_success(tmp_path):
    store = TokenStore("strava", directory=tmp_path)
    auth = StravaAuth(config=STRAVA_CONFIG, store=store)
    callback = MagicMock(
        code="c", state="st", params={"scope": "read,activity:read_all", "state": "st"}
    )

    with patch("rpg.strava.generate_state", return_value="st"), patch(
        "rpg.strava.wait_for_callback", return_value=callback
    ), patch("rpg.strava.request", return_value=json_response(
        {"access_token": "at", "refresh_token": "rt", "expires_at": 9999999999}
    )):
        auth.authorize_interactive(open_browser=False)

    assert store.load().access_token == "at"
