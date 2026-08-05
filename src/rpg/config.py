"""Configuration loading for Spotify and Strava credentials.

Credentials come from environment variables, optionally seeded from a .env file
at the repo root. Nothing here reads or writes tokens — see rpg.tokens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

# Scopes we actually use. Keep these minimal — Spotify shows every scope on the
# consent screen, and Strava silently drops data if you ask for too little.
SPOTIFY_SCOPES: tuple[str, ...] = (
    "user-read-private",  # identify the logged-in account
    "user-library-read",  # saved tracks, a candidate pool for Phase 3
    "user-top-read",  # top tracks, a second candidate pool
    "playlist-read-private",
    "playlist-modify-private",  # create the generated playlist
    "playlist-modify-public",
)

# activity:read_all is required to see private/followers-only activities.
# Plain "activity:read" silently omits them, which looks like missing data.
STRAVA_SCOPES: tuple[str, ...] = ("read", "activity:read_all")


class ConfigError(RuntimeError):
    """Raised when required credentials are missing or malformed."""


@dataclass(frozen=True)
class SpotifyConfig:
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = SPOTIFY_SCOPES

    @property
    def scope_string(self) -> str:
        return " ".join(self.scopes)


@dataclass(frozen=True)
class StravaConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = STRAVA_SCOPES

    @property
    def scope_string(self) -> str:
        return ",".join(self.scopes)


def load_env(dotenv_path: Path | None = None) -> None:
    """Load .env into os.environ. Existing env vars win."""
    path = dotenv_path or (REPO_ROOT / ".env")
    if path.exists():
        load_dotenv(path, override=False)


def _require(name: str, hint: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. {hint}")
    return value


def spotify_config() -> SpotifyConfig:
    load_env()
    client_id = _require(
        "SPOTIFY_CLIENT_ID",
        "Create an app at https://developer.spotify.com/dashboard and copy the Client ID "
        "into .env (see .env.example).",
    )
    redirect_uri = os.environ.get(
        "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
    ).strip()

    # Spotify rejects "localhost" in redirect URIs; it requires a literal
    # loopback IP. This fails at registration time with a confusing error, so
    # catch it here instead.
    if "localhost" in redirect_uri:
        raise ConfigError(
            "SPOTIFY_REDIRECT_URI uses 'localhost', which Spotify rejects. "
            "Use an explicit loopback IP, e.g. http://127.0.0.1:8888/callback, "
            "and register that exact string on your Spotify app."
        )

    return SpotifyConfig(client_id=client_id, redirect_uri=redirect_uri)


def strava_config() -> StravaConfig:
    load_env()
    hint = "Create an app at https://www.strava.com/settings/api and copy the values into .env."
    client_id = _require("STRAVA_CLIENT_ID", hint)
    client_secret = _require("STRAVA_CLIENT_SECRET", hint)
    redirect_uri = os.environ.get(
        "STRAVA_REDIRECT_URI", "http://localhost:8899/exchange_token"
    ).strip()
    return StravaConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )


def token_dir() -> Path:
    load_env()
    raw = os.environ.get("RPG_TOKEN_DIR", ".tokens")
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path
