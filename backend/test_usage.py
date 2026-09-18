from __future__ import annotations

from usage import windows_from_headers, windows_from_unified


def test_unified_5h_and_7d() -> None:
    rows = windows_from_unified(
        {
            "anthropic-ratelimit-unified-5h-utilization": "1",
            "anthropic-ratelimit-unified-5h-reset": "2000000000",
            "anthropic-ratelimit-unified-7d-utilization": "0.34",
            "anthropic-ratelimit-unified-7d-reset": "2000000000",
        }
    )
    assert rows[0]["id"] == "hourly"
    assert rows[0]["label"] == "5-hour limit"
    assert rows[0]["used"] == 100
    assert rows[0]["readout"] == "100%"
    assert rows[1]["id"] == "weekly"
    assert rows[1]["label"] == "Weekly · all models"
    assert rows[1]["used"] == 34


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
    assert rows[0]["used"] == 60000
    assert rows[0]["unit"] == "tokens"
