"""On-disk cache for OAuth tokens.

Tokens live in a gitignored directory, one JSON file per provider, written with
owner-only permissions. Small enough that a file is the right storage; Phase 1's
SQLite work is for activity data, not credentials.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import token_dir


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    # Absolute POSIX timestamp. Strava returns this directly as expires_at;
    # Spotify returns expires_in, which we convert on the way in.
    expires_at: float | None = None
    scope: str | None = None
    token_type: str = "Bearer"

    def expired(self, leeway_seconds: int = 120) -> bool:
        """True if the token is gone or about to expire.

        The leeway keeps a request from starting with 3 seconds left on the
        clock and failing mid-flight.
        """
        if not self.access_token:
            return True
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - leeway_seconds)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TokenSet":
        known = {f for f in cls.__dataclass_fields__}  # noqa: PLC0206
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_spotify_response(
        cls, payload: dict, previous: "TokenSet | None" = None
    ) -> "TokenSet":
        """Spotify returns expires_in (seconds) and sometimes omits refresh_token
        on refresh — in that case the previous refresh token stays valid."""
        refresh = payload.get("refresh_token") or (previous.refresh_token if previous else None)
        return cls(
            access_token=payload["access_token"],
            refresh_token=refresh,
            expires_at=time.time() + float(payload.get("expires_in", 3600)),
            scope=payload.get("scope") or (previous.scope if previous else None),
            token_type=payload.get("token_type", "Bearer"),
        )

    @classmethod
    def from_strava_response(
        cls, payload: dict, previous: "TokenSet | None" = None
    ) -> "TokenSet":
        """Strava returns an absolute expires_at and always rotates the refresh
        token, so the new one must be persisted or the next refresh fails."""
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token")
            or (previous.refresh_token if previous else None),
            expires_at=float(payload["expires_at"]) if "expires_at" in payload else None,
            scope=payload.get("scope") or (previous.scope if previous else None),
            token_type=payload.get("token_type", "Bearer"),
        )


class TokenStore:
    """Reads and writes one provider's tokens."""

    def __init__(self, provider: str, directory: Path | None = None) -> None:
        self.provider = provider
        self.directory = directory or token_dir()
        self.path = self.directory / f"{provider}.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if not data.get("access_token"):
            return None
        return TokenSet.from_dict(data)

    def save(self, tokens: TokenSet) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass  # best effort; Windows and some mounts don't support this

        # Write via a temp file so an interrupted write can't leave a
        # half-written token file behind.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tokens.to_dict(), indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
