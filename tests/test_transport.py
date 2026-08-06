from unittest.mock import MagicMock, patch

import pytest

from rpg.transport import ApiError, RateLimitError, RateLimitStatus, request


def fake_response(status_code: int, headers: dict | None = None, json_body=None):
    response = MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.headers = headers or {}
    response.url = "https://api.example.com/x"
    response.request.method = "GET"
    response.json.return_value = json_body if json_body is not None else {"error": "boom"}
    response.text = "boom"
    return response


def test_returns_successful_response():
    session = MagicMock()
    session.request.return_value = fake_response(200, json_body={"ok": True})
    result = request("GET", "https://api.example.com/x", provider="test", session=session)
    assert result.ok
    assert session.request.call_count == 1


def test_retries_429_then_succeeds():
    session = MagicMock()
    session.request.side_effect = [
        fake_response(429, headers={"Retry-After": "1"}),
        fake_response(200, json_body={"ok": True}),
    ]
    with patch("rpg.transport.time.sleep") as sleep:
        result = request("GET", "https://api.example.com/x", provider="test", session=session)
    assert result.ok
    assert session.request.call_count == 2
    sleep.assert_called_once_with(1)  # honours Retry-After, not the backoff curve


def test_gives_up_on_429_and_raises_rate_limit_error():
    session = MagicMock()
    session.request.return_value = fake_response(429)
    with patch("rpg.transport.time.sleep"), pytest.raises(RateLimitError):
        request("GET", "https://x", provider="test", session=session, max_retries=2)
    assert session.request.call_count == 3  # initial + 2 retries


def test_retries_5xx_with_exponential_backoff():
    session = MagicMock()
    session.request.side_effect = [
        fake_response(503),
        fake_response(503),
        fake_response(200, json_body={}),
    ]
    with patch("rpg.transport.time.sleep") as sleep:
        request("GET", "https://x", provider="test", session=session)
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2]


def test_does_not_retry_4xx():
    session = MagicMock()
    session.request.return_value = fake_response(403)
    with pytest.raises(ApiError) as exc:
        request("GET", "https://x", provider="spotify", session=session)
    assert exc.value.status_code == 403
    assert session.request.call_count == 1


def test_api_error_carries_payload():
    session = MagicMock()
    session.request.return_value = fake_response(400, json_body={"error": "bad_request"})
    with pytest.raises(ApiError) as exc:
        request("GET", "https://x", provider="strava", session=session)
    assert exc.value.payload == {"error": "bad_request"}
    assert "strava" in str(exc.value)


def test_rate_limit_status_parses_strava_headers():
    status = RateLimitStatus.from_headers(
        {"X-RateLimit-Limit": "100,1000", "X-RateLimit-Usage": "12,340"}
    )
    assert status.short_limit == 100
    assert status.daily_limit == 1000
    assert status.short_usage == 12
    assert status.daily_usage == 340
    assert "12/100" in status.describe()


def test_rate_limit_status_handles_missing_headers():
    status = RateLimitStatus.from_headers({})
    assert status.short_limit is None
    assert status.describe() == "rate limit headers not reported"


def test_rate_limit_status_handles_malformed_headers():
    status = RateLimitStatus.from_headers({"X-RateLimit-Limit": "nonsense"})
    assert status.short_limit is None


def test_rate_limit_status_parses_read_specific_headers():
    """Strava sends a stricter read-only limit in its own headers; that's the one
    that binds for a read-only client."""
    status = RateLimitStatus.from_headers(
        {
            "X-RateLimit-Limit": "200,2000",
            "X-RateLimit-Usage": "1,4",
            "X-ReadRateLimit-Limit": "100,1000",
            "X-ReadRateLimit-Usage": "1,4",
        }
    )
    assert status.short_limit == 200 and status.daily_limit == 2000
    assert status.read_short_limit == 100 and status.read_daily_limit == 1000
    assert "binding one" in status.describe()


def test_rate_limit_status_without_read_headers_omits_that_clause():
    status = RateLimitStatus.from_headers(
        {"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": "1,4"}
    )
    assert status.read_short_limit is None
    assert "binding one" not in status.describe()
    assert "overall 1/200" in status.describe()
