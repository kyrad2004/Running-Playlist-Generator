"""Strava auth + a thin API v3 client.

Strava uses plain Authorization Code (no PKCE support), so the client secret is
required. Two behaviours worth knowing:

  * Refresh tokens rotate. Every refresh returns a NEW refresh_token, and the
    old one stops working — it must be persisted or you get locked out.
  * The activity LIST endpoint returns summary objects. Splits, per-lap data and
    device cadence only appear on the DETAIL endpoint, GET /activities/{id}.
"""

from __future__ import annotations

from urllib.parse import urlencode

import requests

from .config import StravaConfig, strava_config
from .oauth import OAuthError, generate_state, wait_for_callback
from .tokens import TokenSet, TokenStore
from .transport import RateLimitStatus, request

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

MAX_PER_PAGE = 200


class StravaAuth:
    def __init__(self, config: StravaConfig | None = None, store: TokenStore | None = None):
        self.config = config or strava_config()
        self.store = store or TokenStore("strava")

    def authorize_interactive(self, open_browser: bool = True) -> TokenSet:
        state = generate_state()
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            # "force" re-prompts so a scope change is actually applied rather
            # than reusing an older, narrower grant.
            "approval_prompt": "force",
            "scope": self.config.scope_string,
            "state": state,
        }
        url = f"{AUTHORIZE_URL}?{urlencode(params)}"
        result = wait_for_callback(self.config.redirect_uri, url, open_browser=open_browser)

        if result.state != state:
            raise OAuthError("State mismatch on the OAuth callback — aborting.")

        granted = result.params.get("scope", "")
        missing = [s for s in self.config.scopes if s not in granted.split(",")]
        if missing:
            raise OAuthError(
                f"Strava did not grant required scope(s): {', '.join(missing)}. "
                "On the consent screen, every checkbox must be ticked — "
                "'activity:read_all' is the one that exposes private activities."
            )

        response = request(
            "POST",
            TOKEN_URL,
            provider="strava",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code": result.code,
                "grant_type": "authorization_code",
            },
        )
        tokens = TokenSet.from_strava_response(response.json())
        self.store.save(tokens)
        return tokens

    def refresh(self, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise OAuthError("No Strava refresh token stored — re-run scripts/auth_strava.py.")
        response = request(
            "POST",
            TOKEN_URL,
            provider="strava",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
            },
        )
        # Persist immediately: the previous refresh token is now dead.
        refreshed = TokenSet.from_strava_response(response.json(), previous=tokens)
        self.store.save(refreshed)
        return refreshed

    def access_token(self) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise OAuthError("No Strava tokens found. Run: python scripts/auth_strava.py")
        if tokens.expired():
            tokens = self.refresh(tokens)
        return tokens.access_token


class StravaClient:
    def __init__(self, auth: StravaAuth | None = None):
        self.auth = auth or StravaAuth()
        self.session = requests.Session()
        self.last_rate_limit: RateLimitStatus | None = None

    def get(self, path: str, **params):
        response = request(
            "GET",
            f"{API_BASE}{path}",
            provider="strava",
            session=self.session,
            headers={"Authorization": f"Bearer {self.auth.access_token()}"},
            params=params or None,
        )
        self.last_rate_limit = RateLimitStatus.from_headers(response.headers)
        return response.json()

    # -- endpoints --------------------------------------------------------
    def athlete(self) -> dict:
        return self.get("/athlete")

    def activities(
        self,
        after: int | None = None,
        before: int | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict]:
        """One page of summary activities, newest first.

        `after`/`before` are POSIX timestamps.
        """
        params: dict[str, int] = {"page": page, "per_page": min(per_page, MAX_PER_PAGE)}
        if after is not None:
            params["after"] = int(after)
        if before is not None:
            params["before"] = int(before)
        return self.get("/athlete/activities", **params)

    def all_activities(self, after: int | None = None, max_pages: int = 10) -> list[dict]:
        """Page through summary activities until exhausted or max_pages hit."""
        collected: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self.activities(after=after, page=page, per_page=MAX_PER_PAGE)
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < MAX_PER_PAGE:
                break
        return collected

    def activity(self, activity_id: int) -> dict:
        """Detailed activity — the only place splits and laps appear."""
        return self.get(f"/activities/{activity_id}", include_all_efforts=False)

    def activity_streams(self, activity_id: int, keys: list[str] | None = None) -> dict:
        """Time-series data. Useful later for cadence-over-time, not just averages."""
        keys = keys or ["time", "velocity_smooth", "heartrate", "cadence", "distance"]
        return self.get(
            f"/activities/{activity_id}/streams",
            keys=",".join(keys),
            key_by_type="true",
        )
