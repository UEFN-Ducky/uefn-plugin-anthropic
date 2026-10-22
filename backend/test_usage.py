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

    u._BLOCKED_UNTIL = 0.0
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


def test_429_without_last_says_when_it_checks_again(monkeypatch) -> None:
    import httpx
    import usage as u

    u._LAST_WINDOWS = []
    u._BLOCKED_UNTIL = 0.0

    class _R:
        status_code = 429
        headers = {"retry-after": "2307"}

    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok"}))
    monkeypatch.setattr(u, "_oauth_expired", lambda oauth: False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    out = u.fetch_usage("")
    assert out["windows"] == []
    assert out["notice"]["message"] == "Claude is limiting plan checks. Next check in 39 min."
    assert "action_label" not in out["notice"]


def test_waits_out_retry_after_before_calling_again(monkeypatch) -> None:
    import httpx
    import usage as u

    u._LAST_WINDOWS = []
    u._BLOCKED_UNTIL = 0.0
    calls: list[str] = []

    class _R:
        status_code = 429
        headers = {"retry-after": "600"}

    def fake_get(*a, **k):
        calls.append(a[0])
        return _R()

    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok"}))
    monkeypatch.setattr(u, "_oauth_expired", lambda oauth: False)
    monkeypatch.setattr(httpx, "get", fake_get)
    u.fetch_usage("")
    u.fetch_usage("")
    assert len(calls) == 1
    monkeypatch.setattr(u.time, "time", lambda: u._BLOCKED_UNTIL + 1)
    u.fetch_usage("")
    assert len(calls) == 2

