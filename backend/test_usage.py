from __future__ import annotations

from usage import windows_from_headers, windows_from_oauth_usage, windows_from_unified


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


def test_oauth_usage_five_hour_and_weekly() -> None:
    rows = windows_from_oauth_usage(
        {
            "five_hour": {"utilization": 1, "resets_at": 2000000000},
            "seven_day": {"utilization": 0.34, "resets_at": 2000000000},
        }
    )
    assert rows[0]["label"] == "5-hour limit"
    assert rows[0]["used"] == 100
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


def test_429_keeps_last_windows_without_notice(monkeypatch) -> None:
    import httpx
    import usage as u

    u._LAST_WINDOWS = [
        {"id": "hourly", "label": "5-hour limit", "used": 40, "limit": 100, "readout": "40%"}
    ]

    class _R:
        status_code = 429

    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok"}))
    monkeypatch.setattr(u, "_oauth_expired", lambda oauth: False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    out = u.fetch_usage("")
    assert out["windows"][0]["used"] == 40
    assert "notice" not in out


def test_429_without_last_is_empty(monkeypatch) -> None:
    import httpx
    import usage as u

    u._LAST_WINDOWS = []

    class _R:
        status_code = 429

    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok"}))
    monkeypatch.setattr(u, "_oauth_expired", lambda oauth: False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    out = u.fetch_usage("")
    assert out == {"windows": []}

