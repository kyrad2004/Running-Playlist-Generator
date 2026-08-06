"""HTTP helper shared by both API clients.

Handles the two failure modes that actually show up against Spotify and Strava:
429 rate limiting (respect Retry-After, back off) and transient 5xx.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_TIMEOUT = 20


class ApiError(RuntimeError):
    """A non-retryable API error, carrying enough context to debug it."""

    def __init__(self, provider: str, response: requests.Response) -> None:
        self.provider = provider
        self.status_code = response.status_code
        self.url = response.url
        try:
            self.payload: Any = response.json()
        except ValueError:
            self.payload = response.text[:500]
        super().__init__(
            f"{provider} {response.status_code} for {response.request.method} {response.url}: "
            f"{self.payload}"
        )


class RateLimitError(ApiError):
    """Rate limited and out of retries."""


@dataclass
class RateLimitStatus:
    """Strava's rate-limit headers.

    Two limits apply at once, in separate headers. The overall limit
    (X-RateLimit-*) is the higher one, currently 200 per 15 min / 2000 per day.
    The read-only limit (X-ReadRateLimit-*) is stricter, currently 100 / 1000,
    and since this project only reads, that is the one that actually binds.
    """

    short_limit: int | None = None
    short_usage: int | None = None
    daily_limit: int | None = None
    daily_usage: int | None = None
    read_short_limit: int | None = None
    read_short_usage: int | None = None
    read_daily_limit: int | None = None
    read_daily_usage: int | None = None

    @classmethod
    def from_headers(cls, headers) -> "RateLimitStatus":
        def pair(value: str | None) -> tuple[int | None, int | None]:
            if not value or "," not in value:
                return None, None
            short, daily = value.split(",", 1)
            try:
                return int(short.strip()), int(daily.strip())
            except ValueError:
                return None, None

        short_limit, daily_limit = pair(headers.get("X-RateLimit-Limit"))
        short_usage, daily_usage = pair(headers.get("X-RateLimit-Usage"))
        read_short_limit, read_daily_limit = pair(headers.get("X-ReadRateLimit-Limit"))
        read_short_usage, read_daily_usage = pair(headers.get("X-ReadRateLimit-Usage"))
        return cls(
            short_limit,
            short_usage,
            daily_limit,
            daily_usage,
            read_short_limit,
            read_short_usage,
            read_daily_limit,
            read_daily_usage,
        )

    def describe(self) -> str:
        if self.short_limit is None:
            return "rate limit headers not reported"
        text = (
            f"overall {self.short_usage}/{self.short_limit} in this 15-min window, "
            f"{self.daily_usage}/{self.daily_limit} today"
        )
        if self.read_short_limit is not None:
            text += (
                f" | read {self.read_short_usage}/{self.read_short_limit} and "
                f"{self.read_daily_usage}/{self.read_daily_limit} — this is the binding one"
            )
        return text


def request(
    method: str,
    url: str,
    *,
    provider: str,
    session: requests.Session | None = None,
    max_retries: int = 3,
    timeout: int = DEFAULT_TIMEOUT,
    **kwargs,
) -> requests.Response:
    """Perform a request, retrying 429s and 5xx with backoff.

    Returns the response for any 2xx. Raises ApiError otherwise.
    """
    http = session or requests
    attempt = 0

    while True:
        response = http.request(method, url, timeout=timeout, **kwargs)

        if response.ok:
            return response

        retryable = response.status_code == 429 or 500 <= response.status_code < 600
        if not retryable or attempt >= max_retries:
            if response.status_code == 429:
                raise RateLimitError(provider, response)
            raise ApiError(provider, response)

        # Retry-After is authoritative when present; otherwise exponential backoff.
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            delay = int(retry_after)
        else:
            delay = 2**attempt

        attempt += 1
        print(
            f"  [{provider}] {response.status_code} — retrying in {delay}s "
            f"(attempt {attempt}/{max_retries})"
        )
        time.sleep(delay)
