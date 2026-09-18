"""Live Claude plan windows from unified rate-limit headers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PAIRS = (
    (
        "anthropic-ratelimit-requests-remaining",
        "anthropic-ratelimit-requests-limit",
        "anthropic-ratelimit-requests-reset",
        "requests",
    ),
    (
        "anthropic-ratelimit-tokens-remaining",
        "anthropic-ratelimit-tokens-limit",
        "anthropic-ratelimit-tokens-reset",
        "tokens",
    ),
)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        n = float(str(v).strip().rstrip("s"))
    except (TypeError, ValueError):
        return None
    if n != n or n < 0:
        return None
    return n


def reset_after_text(seconds: Any) -> str:
    s = _num(seconds)
    if s is None:
        return ""
    n = int(s)
    if n <= 0:
        return ""
    d, rem = divmod(n, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts: list[str] = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h" if d else f"{h} hr")
    if m and d == 0:
        parts.append(f"{m} min")
    return f"Resets in {' '.join(parts)}" if parts else "Resets soon"


def reset_at_text(ts: Any) -> str:
    n = _num(ts)
    if n is None or n <= 0:
        return ""
    if n > 10_000_000_000:
        n = n / 1000.0
    import datetime as _dt

    try:
        dt = _dt.datetime.fromtimestamp(n)
    except (OverflowError, OSError, ValueError):
        return ""
    now = _dt.datetime.now()
    delta = (dt - now).total_seconds()
    if delta > 0:
        pretty = reset_after_text(delta)
        if pretty:
            return pretty
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"Resets {dt.strftime('%a')} {hour}:{dt.strftime('%M %p')}"


def _pct_row(wid: str, label: str, used_pct: float, *, reset: str = "") -> dict[str, Any]:
    used = max(0.0, min(100.0, used_pct))
    row: dict[str, Any] = {
        "id": wid,
        "label": label,
        "used": used,
        "limit": 100.0,
        "readout": f"{int(round(used))}%",
    }
    if reset:
        row["reset"] = reset
    return row


def windows_from_unified(headers: Any) -> list[dict[str, Any]]:
    raw: dict[str, str] = {}
    items = headers.items() if headers is not None and hasattr(headers, "items") else []
    for k, v in items:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        raw[str(k or "").lower()] = str(v or "").strip()
    out: list[dict[str, Any]] = []
    for prefix, wid, label in (
        ("anthropic-ratelimit-unified-5h", "hourly", "5-hour limit"),
        ("anthropic-ratelimit-unified-7d", "weekly", "Weekly · all models"),
    ):
        util = _num(raw.get(f"{prefix}-utilization"))
        if util is None:
            continue
        pct = util * 100.0 if util <= 1.5 else util
        reset = reset_at_text(raw.get(f"{prefix}-reset"))
        out.append(_pct_row(wid, label, pct, reset=reset))
    return out


def windows_from_headers(headers: Any) -> list[dict[str, Any]]:
    unified = windows_from_unified(headers)
    if unified:
        return unified
    raw: dict[str, str] = {}
    items = headers.items() if headers is not None and hasattr(headers, "items") else []
    for k, v in items:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        raw[str(k or "").lower()] = str(v or "").strip()
    out: list[dict[str, Any]] = []
    for rem_k, lim_k, reset_k, unit in _PAIRS:
        rem = _num(raw.get(rem_k))
        lim = _num(raw.get(lim_k))
        if rem is None or lim is None or lim <= 0:
            continue
        used = max(0.0, lim - rem)
        out.append(
            {
                "id": unit,
                "label": unit[:1].upper() + unit[1:],
                "used": used,
                "limit": lim,
                "unit": unit,
                "reset": reset_at_text(raw.get(reset_k)) or reset_after_text(raw.get(reset_k)),
            }
        )
    return out


def _claude_oauth_token() -> str:
    roots = (Path.home() / ".claude", Path.home() / ".config" / "claude")
    names = (".credentials.json", "credentials.json")
    for root in roots:
        for name in names:
            path = root / name
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
            blob = oauth if isinstance(oauth, dict) else data if isinstance(data, dict) else {}
            token = str(blob.get("accessToken") or blob.get("access_token") or "").strip()
            if token:
                return token
    return ""


def _probe_headers(token: str = "", api_key: str = "") -> Any:
    import httpx

    headers: dict[str, str] = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    elif api_key:
        headers["x-api-key"] = api_key
    else:
        return None
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        },
        timeout=8.0,
        follow_redirects=True,
    )
    return r.headers


def fetch_usage(api_key: str, *, model: str = "") -> dict[str, Any]:
    token = _claude_oauth_token()
    key = (api_key or "").strip()
    try:
        hdrs = _probe_headers(token=token, api_key="" if token else key)
        rows = windows_from_headers(hdrs)
        if rows:
            return {"windows": rows}
    except Exception:
        pass
    if not key:
        return {"windows": []}
    try:
        import httpx

        r = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=8.0,
            follow_redirects=True,
        )
        return {"windows": windows_from_headers(r.headers)}
    except Exception:
        return {"windows": []}
