"""Live Claude plan windows from unified rate-limit headers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_LAST_WINDOWS: list[dict[str, Any]] = []
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


def windows_from_oauth_usage(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    mapping = (
        ("five_hour", "hourly", "5-hour limit"),
        ("fiveHour", "hourly", "5-hour limit"),
        ("seven_day", "weekly", "Weekly · all models"),
        ("sevenDay", "weekly", "Weekly · all models"),
        ("seven_day_util", "weekly", "Weekly · all models"),
    )
    nodes: list[Any] = [data]
    limits = data.get("limits")
    if isinstance(limits, dict):
        nodes.append(limits)
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key, wid, label in mapping:
            if wid in seen:
                continue
            blob = node.get(key)
            if not isinstance(blob, dict):
                continue
            pct = _num(
                blob.get("utilization")
                or blob.get("used_percent")
                or blob.get("used_percentage")
                or blob.get("usedPercent")
            )
            if pct is None:
                continue
            pct = pct * 100.0 if pct <= 1.5 else pct
            reset = reset_at_text(blob.get("resets_at") or blob.get("reset") or blob.get("resetsAt"))
            if not reset:
                raw = str(blob.get("resets_at") or blob.get("reset_at") or "").strip()
                if raw and not raw.isdigit():
                    reset = reset_at_iso_text(raw)
            out.append(_pct_row(wid, label, pct, reset=reset))
            seen.add(wid)
    return out


def reset_at_iso_text(value: str) -> str:
    import datetime as _dt

    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    delta = (dt - _dt.datetime.now().astimezone()).total_seconds()
    if delta > 0:
        pretty = reset_after_text(delta)
        if pretty:
            return pretty
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"Resets {dt.strftime('%a')} {hour}:{dt.strftime('%M %p')}"


def _notice(message: str, *, action: str = "login", label: str = "Log in") -> dict[str, Any]:
    return {
        "windows": [],
        "notice": {
            "message": message,
            "action": action,
            "action_label": label,
            "provider_id": "anthropic",
            "agent_id": "claude_code",
        },
    }


_CLAUDE_OAUTH_CLIENT = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _claude_cred_paths() -> list[Path]:
    roots: list[Path] = []
    env = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if env:
        roots.append(Path(env))
    roots.extend((Path.home() / ".claude", Path.home() / ".config" / "claude"))
    names = (".credentials.json", "credentials.json")
    out: list[Path] = []
    for root in roots:
        for name in names:
            out.append(root / name)
    return out


def _load_claude_oauth() -> tuple[Path | None, dict[str, Any], dict[str, Any]]:
    env_tok = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if env_tok:
        return None, {}, {"accessToken": env_tok}
    for path in _claude_cred_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else None
        blob = oauth if isinstance(oauth, dict) else data
        token = str(blob.get("accessToken") or blob.get("access_token") or "").strip()
        if token:
            return path, data, blob
    return None, {}, {}


def _oauth_expired(oauth: dict[str, Any]) -> bool:
    n = _num(oauth.get("expiresAt") or oauth.get("expires_at"))
    if n is None:
        return False
    if n < 10_000_000_000:
        n *= 1000.0
    return n <= time.time() * 1000.0 + 30_000


def _save_claude_oauth(path: Path, data: dict[str, Any], oauth: dict[str, Any]) -> None:
    payload = dict(data)
    payload["claudeAiOauth"] = oauth
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _refresh_claude_oauth(oauth: dict[str, Any]) -> dict[str, Any] | None:
    rt = str(oauth.get("refreshToken") or oauth.get("refresh_token") or "").strip()
    if not rt:
        return None
    import httpx

    r = httpx.post(
        "https://console.anthropic.com/v1/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": _CLAUDE_OAUTH_CLIENT,
        },
        timeout=10.0,
    )
    if r.status_code >= 400:
        return None
    body = r.json()
    token = str(body.get("access_token") or "").strip()
    if not token:
        return None
    now = int(time.time() * 1000)
    expires_in = int(body.get("expires_in") or 3600)
    out = dict(oauth)
    out["accessToken"] = token
    if body.get("refresh_token"):
        out["refreshToken"] = str(body["refresh_token"])
    out["expiresAt"] = now + expires_in * 1000
    if body.get("refresh_token_expires_in"):
        out["refreshTokenExpiresAt"] = now + int(body["refresh_token_expires_in"]) * 1000
    return out


def _keep(rows: list[dict[str, Any]]) -> dict[str, Any]:
    global _LAST_WINDOWS
    _LAST_WINDOWS = list(rows)
    return {"windows": _LAST_WINDOWS}


def _last_or_empty() -> dict[str, Any]:
    return {"windows": list(_LAST_WINDOWS)} if _LAST_WINDOWS else {"windows": []}


def fetch_usage(api_key: str, *, model: str = "") -> dict[str, Any]:
    path, data, oauth = _load_claude_oauth()
    key = (api_key or "").strip()
    if oauth and _oauth_expired(oauth):
        try:
            refreshed = _refresh_claude_oauth(oauth)
        except Exception:
            refreshed = None
        if refreshed:
            oauth = refreshed
            if path is not None:
                try:
                    _save_claude_oauth(path, data, oauth)
                except Exception:
                    pass
        elif not key:
            return _notice("Claude login expired. Log in to see 5-hour and weekly limits.")
    token = str(oauth.get("accessToken") or oauth.get("access_token") or "").strip()
    if token:
        try:
            import httpx

            r = httpx.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "anthropic-version": "2023-06-01",
                },
                timeout=8.0,
                follow_redirects=True,
            )
            if r.status_code == 401:
                try:
                    refreshed = _refresh_claude_oauth(oauth)
                except Exception:
                    refreshed = None
                if refreshed and path is not None:
                    try:
                        _save_claude_oauth(path, data, refreshed)
                    except Exception:
                        pass
                    token = str(refreshed.get("accessToken") or "").strip()
                    r = httpx.get(
                        "https://api.anthropic.com/api/oauth/usage",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "anthropic-beta": "oauth-2025-04-20",
                            "anthropic-version": "2023-06-01",
                        },
                        timeout=8.0,
                        follow_redirects=True,
                    )
                if r.status_code == 401:
                    return _notice("Claude login expired. Log in to see 5-hour and weekly limits.")
            if r.status_code == 429:
                return _last_or_empty()
            if r.status_code < 400:
                rows = windows_from_oauth_usage(r.json())
                if rows:
                    return _keep(rows)
        except Exception:
            pass
    if not key:
        if token:
            return _notice(
                "Couldn't read Claude plan limits. Log in again, or press Refresh.",
                action="retry",
                label="Refresh",
            )
        return _notice("Log in with Claude Code to see 5-hour and weekly limits.")
    try:
        import httpx

        r = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=8.0,
            follow_redirects=True,
        )
        rows = windows_from_headers(r.headers)
        if rows:
            return _keep(rows)
    except Exception:
        pass
    return _notice("Couldn't read Claude plan limits. Log in to refresh, or press Refresh.", action="retry", label="Refresh")
