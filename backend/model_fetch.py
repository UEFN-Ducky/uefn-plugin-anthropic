"""Vendor model list / pricing fetch for this gateway plugin."""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

from backend.agent.model_fetch import (
    ModelInfo,
    _PricingRow,
    _cache_put,
    _float_from_record,
    _int_from_record,
    _merge_prices,
    _parse_price_cell,
    _per_million_from_token_rate,
)

_log = logging.getLogger(__name__)
_CACHE_MAX = 512
_CACHE_TTL_S = 6 * 3600.0


_ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
_DEPRECATIONS_URL = "https://platform.claude.com/docs/en/about-claude/model-deprecations"
_PROVIDER_PRICING_CACHE: dict[str, tuple[float, dict[str, _PricingRow]]] = {}
_COPY_ID_RE = re.compile(r"Copy model ID (claude-[a-z0-9-]+)", re.I)
_TR_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_DATED_ID_RE = re.compile(r"-(\d{8})$")


def canonical_model_id(model_id: str) -> str:
    """Drop a trailing YYYYMMDD snapshot so the picker shows the CLI alias."""
    return _DATED_ID_RE.sub("", (model_id or "").strip().lower())


def parse_active_model_ids(html: str) -> list[str]:
    """Active Claude API ids from the public deprecations table HTML."""
    seen: set[str] = set()
    out: list[str] = []
    for row in _TR_RE.findall(html or ""):
        copy = _COPY_ID_RE.search(row)
        if not copy:
            continue
        cells = [" ".join(_TAG_RE.sub("", c).split()) for c in _TD_RE.findall(row)]
        if len(cells) < 2 or cells[1].lower() != "active":
            continue
        mid = canonical_model_id(copy.group(1))
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def fetch_public_active_model_ids() -> list[str]:
    """No API key — Anthropic's public deprecations page is the live catalog."""
    import httpx

    # Short timeout: the cold-start path calls this inline from detect().
    r = httpx.get(_DEPRECATIONS_URL, follow_redirects=True, timeout=10.0)
    r.raise_for_status()
    return parse_active_model_ids(r.text)


def _pricing_catalog() -> dict[str, _PricingRow]:
    hit = _PROVIDER_PRICING_CACHE.get("anthropic")
    if hit is not None and (time.time() - hit[0]) < _CACHE_TTL_S:
        return hit[1]
    catalog = _fetch_anthropic_pricing_catalog()
    _cache_put(_PROVIDER_PRICING_CACHE, "anthropic", (time.time(), catalog))
    return catalog


def clear_model_cache() -> None:
    _PROVIDER_PRICING_CACHE.clear()


def fetch_models(api_key: str, **_kw: Any) -> list[ModelInfo]:
    return _fetch_anthropic(api_key)

def _anthropic_price_lookup_key(model_id: str, display_name: str) -> str | None:
    dn = (display_name or "").strip()
    if dn:
        return dn
    parts = (model_id or "").lower().split("-")
    if len(parts) < 3 or parts[0] != "claude":
        return None
    family = parts[1].title()
    if len(parts) >= 4 and parts[3].isdigit() and len(parts[3]) <= 2:
        return f"Claude {family} {parts[2]}.{parts[3]}"
    return f"Claude {family} {parts[2]}"


def _resolve_anthropic_price(
    catalog: dict[str, _PricingRow],
    model_id: str,
    display_name: str,
) -> _PricingRow | None:
    if not catalog:
        return None
    dn = (display_name or "").strip()
    if dn in catalog:
        return catalog[dn]
    key = _anthropic_price_lookup_key(model_id, display_name)
    if key and key in catalog:
        return catalog[key]
    if key:
        matches = [name for name in catalog if name.startswith(key)]
        if matches:
            return catalog[sorted(matches, reverse=True)[0]]
    return None


def _fetch_anthropic_pricing_catalog() -> dict[str, _PricingRow]:
    catalog: dict[str, _PricingRow] = {}
    try:
        import httpx

        r = httpx.get(_ANTHROPIC_PRICING_URL, follow_redirects=True, timeout=30.0)
        r.raise_for_status()
        text = html.unescape(r.text)
        row_re = re.compile(
            r"<td[^>]*>(Claude [^<]+)</td>"
            r"(?:<td[^>]*>\$[\d.]+ / MTok</td>){5}",
            re.I,
        )
        for m in row_re.finditer(text):
            name = m.group(1).strip()
            prices = [float(x) for x in re.findall(r"\$([\d.]+) / MTok", m.group(0))]
            if len(prices) < 5:
                continue
            catalog[name] = (prices[0], prices[4], prices[3], None)
    except Exception as exc:
        _log.warning("Anthropic pricing page unavailable: %s", exc)
    return catalog


def _anthropic_supports_tools(caps: dict[str, Any]) -> bool:
    """Claude models support tools; API capability flags are incomplete / lagging.

    Prefer True for any Claude id. Only return False when capabilities explicitly
    advertise no tool use (rare). Favorites picker filters on this field.
    """
    if not isinstance(caps, dict):
        return True
    cm = caps.get("context_management")
    if isinstance(cm, dict):
        clear_tools = cm.get("clear_tool_uses_20250919")
        if isinstance(clear_tools, dict) and clear_tools.get("supported") is False:
            # explicit deny only
            pass
        elif isinstance(clear_tools, dict) and clear_tools.get("supported"):
            return True
    so = caps.get("structured_outputs")
    if isinstance(so, dict) and so.get("supported"):
        return True
    # Default True — Anthropic /v1/models often omits tool capability flags.
    return True


def _anthropic_info_from_item(
    item: dict[str, Any],
    pricing_catalog: dict[str, _PricingRow] | None = None,
) -> ModelInfo | None:
    mid = (item.get("id") or item.get("name") or "").strip()
    if not mid:
        return None
    caps = item.get("capabilities") or {}
    image = caps.get("image_input") if isinstance(caps, dict) else {}
    vision = bool(isinstance(image, dict) and image.get("supported"))
    ctx = _int_from_record(item, "max_input_tokens", "context_window")
    display_name = str(item.get("display_name") or mid)
    price_in = _float_from_record(item, "input_price_per_million", "input_cost_per_million")
    price_out = _float_from_record(item, "output_price_per_million", "output_cost_per_million")
    cached = _float_from_record(item, "cached_input_price_per_million", "cache_read_price_per_million")
    price_in, price_out, cached, _cache_write = _merge_prices(
        (price_in, price_out, cached, None),
        _resolve_anthropic_price(pricing_catalog or {}, mid, display_name),
    )
    return ModelInfo(
        id=mid,
        display_name=display_name,
        supports_vision=vision,
        supports_tools=_anthropic_supports_tools(caps if isinstance(caps, dict) else {}),
        context_limit=ctx,
        price_in=price_in,
        price_out=price_out,
        price_cached_in=cached,
    )


def _fetch_anthropic(api_key: str) -> list[ModelInfo]:
    import httpx

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    pricing_catalog = _pricing_catalog()
    models: list[ModelInfo] = []
    seen: set[str] = set()
    after_id: str | None = None
    # Anthropic paginates; follow has_more / last_id until exhausted (cap pages).
    for _ in range(20):
        params: dict[str, Any] = {"limit": 100}
        if after_id:
            params["after_id"] = after_id
        r = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers=headers,
            params=params,
            timeout=30.0,
        )
        r.raise_for_status()
        body = r.json()
        page = body.get("data") or []
        for item in page:
            if not isinstance(item, dict):
                continue
            info = _anthropic_info_from_item(item, pricing_catalog)
            if info and info.id not in seen:
                models.append(info)
                seen.add(info.id)
        has_more = bool(body.get("has_more"))
        last_id = body.get("last_id") or (page[-1].get("id") if page and isinstance(page[-1], dict) else None)
        if not has_more or not last_id or last_id == after_id:
            break
        after_id = str(last_id)
    models.sort(key=lambda m: m.id, reverse=True)
    return models

