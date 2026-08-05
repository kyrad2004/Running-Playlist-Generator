"""Spotify auth + a thin Web API client.

Auth is Authorization Code with PKCE, which needs no client secret — the right
choice for a locally-run app where a secret couldn't be kept secret anyway.

Endpoint paths here follow the February 2026 Web API revision. Notably:
  * playlists are created at POST /me/playlists (POST /users/{id}/playlists is gone)
  * items are added at POST /playlists/{id}/items (…/tracks is gone)
  * search returns at most 10 results per type
  * /v1/audio-features is unavailable to apps created after 2024-11-27
See docs/PHASE0_FINDINGS.md.
"""

from __future__ import annotations

from urllib.parse import urlencode

import requests

from .config import SpotifyConfig, spotify_config
from .oauth import (
    OAuthError,
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
    wait_for_callback,
)
from .tokens import TokenSet, TokenStore
from .transport import request

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Hard cap imposed by the February 2026 API revision (was 50).
MAX_SEARCH_LIMIT = 10


class SpotifyAuth:
    """Owns the token lifecycle: authorize once, refresh thereafter."""

    def __init__(self, config: SpotifyConfig | None = None, store: TokenStore | None = None):
        self.config = config or spotify_config()
        self.store = store or TokenStore("spotify")

    # -- interactive ------------------------------------------------------
    def authorize_interactive(self, open_browser: bool = True) -> TokenSet:
        verifier = generate_code_verifier()
        state = generate_state()
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "scope": self.config.scope_string,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": code_challenge_s256(verifier),
            # Force the consent screen so scope changes actually take effect
            # instead of silently reusing a narrower prior grant.
            "show_dialog": "true",
        }
        url = f"{AUTHORIZE_URL}?{urlencode(params)}"
        result = wait_for_callback(self.config.redirect_uri, url, open_browser=open_browser)

        if result.state != state:
            raise OAuthError("State mismatch on the OAuth callback — aborting.")

        response = request(
            "POST",
            TOKEN_URL,
            provider="spotify",
            data={
                "grant_type": "authorization_code",
                "code": result.code,
                "redirect_uri": self.config.redirect_uri,
                "client_id": self.config.client_id,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        tokens = TokenSet.from_spotify_response(response.json())
        self.store.save(tokens)
        return tokens

    # -- refresh ----------------------------------------------------------
    def refresh(self, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise OAuthError("No Spotify refresh token stored — re-run scripts/auth_spotify.py.")
        response = request(
            "POST",
            TOKEN_URL,
            provider="spotify",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": self.config.client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        refreshed = TokenSet.from_spotify_response(response.json(), previous=tokens)
        self.store.save(refreshed)
        return refreshed

    def access_token(self) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise OAuthError(
                "No Spotify tokens found. Run: python scripts/auth_spotify.py"
            )
        if tokens.expired():
            tokens = self.refresh(tokens)
        return tokens.access_token


class SpotifyClient:
    def __init__(self, auth: SpotifyAuth | None = None):
        self.auth = auth or SpotifyAuth()
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.access_token()}"}

    def get(self, path: str, **params) -> dict:
        response = request(
            "GET",
            f"{API_BASE}{path}",
            provider="spotify",
            session=self.session,
            headers=self._headers(),
            params=params or None,
        )
        return response.json() if response.content else {}

    def post(self, path: str, json: dict) -> dict:
        response = request(
            "POST",
            f"{API_BASE}{path}",
            provider="spotify",
            session=self.session,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=json,
        )
        return response.json() if response.content else {}

    # -- endpoints --------------------------------------------------------
    def me(self) -> dict:
        return self.get("/me")

    def search_tracks(self, query: str, limit: int = MAX_SEARCH_LIMIT, offset: int = 0) -> list[dict]:
        limit = min(limit, MAX_SEARCH_LIMIT)
        payload = self.get("/search", q=query, type="track", limit=limit, offset=offset)
        return payload.get("tracks", {}).get("items", [])

    def saved_tracks(self, limit: int = 50, offset: int = 0) -> dict:
        return self.get("/me/tracks", limit=limit, offset=offset)

    def top_tracks(self, limit: int = 20, time_range: str = "medium_term") -> dict:
        return self.get("/me/top/tracks", limit=limit, time_range=time_range)

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        """POST /me/playlists — the per-user path was removed in Feb 2026."""
        return self.post(
            "/me/playlists",
            {"name": name, "description": description, "public": public},
        )

    def add_items(self, playlist_id: str, uris: list[str]) -> dict:
        """POST /playlists/{id}/items — .../tracks was removed in Feb 2026.

        Spotify accepts at most 100 URIs per call.
        """
        if len(uris) > 100:
            raise ValueError("Spotify accepts at most 100 URIs per add_items call.")
        return self.post(f"/playlists/{playlist_id}/items", {"uris": uris})
