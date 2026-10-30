"""Live Claude plan windows from unified rate-limit headers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:
    from .accounts import PRIMARY, SECONDARY, account_credentials_paths, account_label, normalize_account
except ImportError:
    from accounts import PRIMARY, SECONDARY, account_credentials_paths, account_label, normalize_account

# Account 1's last bars and 429 block; account 2 keeps its own in _ACCOUNT2_*.
_LAST_WINDOWS: list[dict[str, Any]] = []
# Anthropic's plan-usage endpoint answers 429 with Retry-After (often 30+ min). Calling it
# again before then only extends the block, so wait it out and show the last bars.
_BLOCKED_UNTIL = 0.0
_ACCOUNT2_LAST: list[dict[str, Any]] = []
_ACCOUNT2_BLOCKED_UNTIL = 0.0
# Core accounts are keyed by their config folder (a str) instead of 1 / 2.
_DIR_LAST: dict[str, list[dict[str, Any]]] = {}
_DIR_BLOCKED_UNTIL: dict[str, float] = {}
# When each account's plan windows were last read from Anthropic (time.time()).
_FETCHED_AT: dict[Any, float] = {}
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


def reset_epoch(value: Any) -> float | None:
    """A window's reset as epoch seconds, from epoch s/ms or an ISO time; None if unreadable."""
    n = _num(value)
    if n is not None:
        if n <= 0:
            return None
        return n / 1000.0 if n > 10_000_000_000 else n
    text = str(value or "").strip()
    if not text:
        return None
    import datetime as _dt

    try:
        dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _pct_row(wid: str, label: str, used_pct: float, *, reset: str = "", resets_at: float | None = None) -> dict[str, Any]:
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
    if resets_at:
        row["resets_at"] = resets_at
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
        out.append(_pct_row(wid, label, pct, reset=reset, resets_at=reset_epoch(raw.get(f"{prefix}-reset"))))
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
            when = reset_epoch(
                blob.get("resets_at") or blob.get("reset") or blob.get("resetsAt") or blob.get("reset_at")
            )
            out.append(_pct_row(wid, label, pct, reset=reset, resets_at=when))
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


def _read_oauth(paths: list[Path]) -> tuple[Path | None, dict[str, Any], dict[str, Any]]:
    for path in paths:
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


def _load_claude_oauth() -> tuple[Path | None, dict[str, Any], dict[str, Any]]:
    env_tok = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if env_tok:
        return None, {}, {"accessToken": env_tok}
    return _read_oauth(_claude_cred_paths())


def _load_account_oauth(account: int) -> tuple[Path | None, dict[str, Any], dict[str, Any]]:
    """Account 1: Claude Code's usual login. Account 2 / a config folder: only its own credentials file."""
    if isinstance(account, str):
        return _read_oauth([Path(account) / ".credentials.json", Path(account) / "credentials.json"])
    if account == SECONDARY:
        return _read_oauth(account_credentials_paths(SECONDARY))
    return _load_claude_oauth()


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


def _last(account: int = PRIMARY) -> list[dict[str, Any]]:
    if isinstance(account, str):
        return _DIR_LAST.get(account, [])
    return _ACCOUNT2_LAST if account == SECONDARY else _LAST_WINDOWS


def _blocked_until(account: int = PRIMARY) -> float:
    if isinstance(account, str):
        return _DIR_BLOCKED_UNTIL.get(account, 0.0)
    return _ACCOUNT2_BLOCKED_UNTIL if account == SECONDARY else _BLOCKED_UNTIL


def _keep(rows: list[dict[str, Any]], account: int = PRIMARY) -> dict[str, Any]:
    global _LAST_WINDOWS, _ACCOUNT2_LAST
    if isinstance(account, str):
        _DIR_LAST[account] = list(rows)
    elif account == SECONDARY:
        _ACCOUNT2_LAST = list(rows)
    else:
        _LAST_WINDOWS = list(rows)
    return {"windows": _last(account)}


def _last_or_blocked(account: int = PRIMARY) -> dict[str, Any]:
    if _last(account):
        return {"windows": list(_last(account))}
    wait = max(60, int(_blocked_until(account) - time.time()))
    return {
        "windows": [],
        "notice": {
            "message": f"Claude is limiting plan checks. Next check in {(wait + 59) // 60} min.",
            "provider_id": "anthropic",
            "agent_id": "claude_code",
        },
    }


def _block_from(r: Any, account: int = PRIMARY) -> None:
    global _BLOCKED_UNTIL, _ACCOUNT2_BLOCKED_UNTIL
    headers = getattr(r, "headers", None) or {}
    wait = _num(headers.get("retry-after"))
    until = time.time() + (wait if wait is not None and wait > 0 else 300.0)
    if isinstance(account, str):
        _DIR_BLOCKED_UNTIL[account] = until
    elif account == SECONDARY:
        _ACCOUNT2_BLOCKED_UNTIL = until
    else:
        _BLOCKED_UNTIL = until


def _get_oauth_usage(token: str, timeout: float) -> Any:
    import httpx

    return httpx.get(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        },
        timeout=timeout,
        follow_redirects=True,
    )


def _plan_usage(account: int, *, keep_expired: bool = False, timeout: float = 8.0) -> tuple[str, list[dict[str, Any]]]:
    """This account's plan windows: ("ok", rows), or "none" / "expired" / "blocked" / "failed"."""
    path, data, oauth = _load_account_oauth(account)
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
        elif not keep_expired:
            return "expired", []
    token = str(oauth.get("accessToken") or oauth.get("access_token") or "").strip()
    if not token:
        return "none", []
    if time.time() < _blocked_until(account):
        return "blocked", []
    try:
        r = _get_oauth_usage(token, timeout)
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
                r = _get_oauth_usage(token, timeout)
            if r.status_code == 401:
                return "expired", []
        if r.status_code == 429:
            _block_from(r, account)
            return "blocked", []
        if r.status_code < 400:
            rows = windows_from_oauth_usage(r.json())
            if rows:
                _keep(rows, account)
                _FETCHED_AT[account] = time.time()
                return "ok", rows
    except Exception:
        pass
    return "failed", []


def account_windows(account: int, *, max_age: float = 60.0) -> list[dict[str, Any]] | None:
    """This account's 5-hour / weekly rows, reused for ``max_age`` s; None when unknown.

    Unknown = not logged in, login expired, network error, or blocked with nothing cached.
    ``account`` may be a core account's config folder (str).
    """
    if not isinstance(account, str):
        account = normalize_account(account)
    if max_age > 0 and time.time() - _FETCHED_AT.get(account, 0.0) < max_age:
        return list(_last(account))
    status, rows = _plan_usage(account, timeout=4.0)
    if status == "ok":
        return list(rows)
    if status == "blocked":
        return list(_last(account)) or None
    return None


def _failover() -> Any:
    try:
        from . import failover
    except ImportError:
        import failover
    return failover


def _meter_account() -> int:
    try:
        return _failover().current_account()
    except Exception:
        return PRIMARY


def _account_switch() -> dict[str, Any] | None:
    """The model picker's Account 1 / Account 2 toggle; None until account 2 is logged in."""
    try:
        fo = _failover()
        if not fo.account2_ready():
            return None
        value = str(fo.preferred())
    except Exception:
        return None
    return {
        "plugin_id": "anthropic",
        "rpc": "set_claude_account",
        "value": value,
        "options": [{"id": str(n), "label": account_label(n)} for n in (PRIMARY, SECONDARY)],
    }


def fetch_usage(api_key: str, *, model: str = "") -> dict[str, Any]:
    account = _meter_account()
    switch = _account_switch()
    out = _usage_for(account, api_key)
    if switch is None:
        return out
    # Account 2's rows are labelled already; label account 1's too once there are two.
    return {**(out if account == SECONDARY else _for_account(out, account, always=True)), "switch": switch}


def _for_account(out: dict[str, Any], account: int, *, always: bool = False) -> dict[str, Any]:
    """Bars and notices say which account they are about (account 2, or both once two exist)."""
    if account != SECONDARY and not always:
        return out
    prefix = account_label(account)
    out = dict(out)
    out["windows"] = [{**row, "label": f"{prefix} · {row.get('label') or ''}"} for row in out.get("windows") or []]
    if isinstance(out.get("notice"), dict):
        out["notice"] = {**out["notice"], "message": f"{prefix}: {out['notice'].get('message') or ''}"}
    return out


def _usage_for(account: int, api_key: str) -> dict[str, Any]:
    key = (api_key or "").strip() if account == PRIMARY else ""
    status, rows = _plan_usage(account, keep_expired=bool(key))
    if status == "ok":
        return _for_account({"windows": list(rows)}, account)
    if status == "blocked":
        return _for_account(_last_or_blocked(account), account)
    if status == "expired":
        return _for_account(_notice("Claude login expired. Log in to see 5-hour and weekly limits."), account)
    if not key:
        if status == "failed":
            return _for_account(_notice(
                "Couldn't read Claude plan limits. Log in again, or press Refresh.",
                action="retry",
                label="Refresh",
            ), account)
        return _for_account(_notice("Log in with Claude Code to see 5-hour and weekly limits."), account)
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
