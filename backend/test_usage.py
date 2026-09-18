from __future__ import annotations

from usage import windows_from_headers


def test_hourly_requests_from_openai_headers() -> None:
    rows = windows_from_headers(
        {
            "x-ratelimit-limit-requests": "40",
            "x-ratelimit-remaining-requests": "28",
            "x-ratelimit-reset-requests": "1h",
        }
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "hourly"
    assert rows[0]["used"] == 12
    assert rows[0]["limit"] == 40
    assert rows[0]["unit"] == "requests"


def test_empty_without_limits() -> None:
    assert windows_from_headers({}) == []
    assert windows_from_headers(None) == []


def test_anthropic_token_headers() -> None:
    rows = windows_from_headers(
        {
            "anthropic-ratelimit-tokens-limit": "100000",
            "anthropic-ratelimit-tokens-remaining": "40000",
            "anthropic-ratelimit-tokens-reset": "1h",
        }
    )
    assert rows[0]["id"] == "hourly"
    assert rows[0]["used"] == 60000
    assert rows[0]["unit"] == "tokens"
