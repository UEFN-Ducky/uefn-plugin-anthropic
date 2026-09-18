"""Live quota windows from vendor rate-limit headers. No hardcoded caps."""

from __future__ import annotations

from typing import Any

_PAIRS = (
    ("x-ratelimit-remaining-requests", "x-ratelimit-limit-requests", "x-ratelimit-reset-requests", "requests"),
    ("x-ratelimit-remaining-tokens", "x-ratelimit-limit-tokens", "x-ratelimit-reset-tokens", "tokens"),
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
    (
        "anthropic-ratelimit-input-tokens-remaining",
        "anthropic-ratelimit-input-tokens-limit",
        "anthropic-ratelimit-input-tokens-reset",
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


def _kind(reset: str) -> str:
    s = (reset or "").strip().lower()
    if any(x in s for x in ("7d", "week", "604800")):
        return "weekly"
    if any(x in s for x in ("30d", "month", "2592000")):
        return "monthly"
    if any(x in s for x in ("1h", "hour", "3600")):
        return "hourly"
    return ""


def _label(kind: str, unit: str) -> str:
    if kind == "hourly":
        return "Hourly"
    if kind == "weekly":
        return "Weekly"
    if kind == "monthly":
        return "Monthly"
    return unit[:1].upper() + unit[1:] if unit else "Usage"


def windows_from_headers(headers: Any) -> list[dict[str, Any]]:
    raw: dict[str, str] = {}
    items = headers.items() if headers is not None and hasattr(headers, "items") else []
    for k, v in items:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        raw[str(k or "").lower()] = str(v or "").strip()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rem_k, lim_k, reset_k, unit in _PAIRS:
        rem = _num(raw.get(rem_k))
        lim = _num(raw.get(lim_k))
        if rem is None or lim is None or lim <= 0:
            continue
        kind = _kind(raw.get(reset_k) or "") or unit
        if kind in seen:
            kind = f"{kind}-{unit}"
        seen.add(kind)
        used = max(0.0, lim - rem)
        out.append(
            {
                "id": kind,
                "label": _label(kind.split("-", 1)[0], unit),
                "used": used,
                "limit": lim,
                "unit": unit,
            }
        )
    return out


def fetch_live(api_key: str, url: str, headers: list[tuple[str, str]]) -> dict[str, Any]:
    key = (api_key or "").strip()
    if not key:
        return {"windows": []}
    try:
        import httpx

        r = httpx.get(url, headers=dict(headers), timeout=8.0, follow_redirects=True)
        return {"windows": windows_from_headers(r.headers)}
    except Exception:
        return {"windows": []}


def fetch_usage(api_key: str, *, model: str = "") -> dict[str, Any]:
    key = (api_key or "").strip()
    return fetch_live(
        key,
        "https://api.anthropic.com/v1/models",
        [("x-api-key", key), ("anthropic-version", "2023-06-01")],
    )
