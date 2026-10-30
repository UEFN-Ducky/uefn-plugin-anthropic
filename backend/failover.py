"""Run Claude Code chats on account 2 while account 1 is at its plan limit.

Saved state (``coding_agents/claude_accounts_state.json``) holds, per account, when its
5-hour or weekly limit resets (``exhausted_until``, epoch s) and which account the last
turn ran on. Nothing here raises: an unknown answer keeps chats on account 1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

try:
    from .accounts import (
        PRIMARY, SECONDARY, account_config_dir, account_credentials_paths, failover_enabled, normalize_account,
    )
except ImportError:
    from accounts import (
        PRIMARY, SECONDARY, account_config_dir, account_credentials_paths, failover_enabled, normalize_account,
    )

FALLBACK_WAIT_S = 15 * 60
_CHECK_TTL_S = 60.0
_LOGIN_TTL_S = 300.0
_LOCK = threading.Lock()
_CHECKED: dict[int, tuple[float, bool]] = {}
_LOGIN: dict[int, tuple[float, bool]] = {}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def other_account(account: int) -> int:
    return PRIMARY if normalize_account(account) == SECONDARY else SECONDARY


# ── saved state ─────────────────────────────────────────────────────────


def state_path() -> Path:
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "claude_accounts_state.json"


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict[str, Any]) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _until(state: dict[str, Any], account: int) -> float:
    accounts = state.get("accounts")
    entry = accounts.get(str(account)) if isinstance(accounts, dict) else None
    value = entry.get("exhausted_until") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


def exhausted_until(account: int) -> float:
    """When this account's limit resets (epoch s); 0 when it isn't known to be at a limit."""
    with _LOCK:
        return _until(_read_state(), normalize_account(account))


def mark_exhausted(account: int, until: float | None = None) -> float:
    """Record that this account is at its limit until ``until`` (default: 15 min from now)."""
    account = normalize_account(account)
    now = time.time()
    when = float(until) if until and until > now else now + FALLBACK_WAIT_S
    with _LOCK:
        state = _read_state()
        accounts = state.get("accounts") if isinstance(state.get("accounts"), dict) else {}
        entry = accounts.get(str(account)) if isinstance(accounts.get(str(account)), dict) else {}
        accounts[str(account)] = {**entry, "exhausted_until": when}
        state["accounts"] = accounts
        _write_state(state)
        _CHECKED.pop(account, None)
    return when


def last_active() -> int:
    with _LOCK:
        return normalize_account(_read_state().get("active", PRIMARY))


def set_active(account: int) -> int:
    """Remember the account this turn runs on; returns the one the previous turn used."""
    account = normalize_account(account)
    with _LOCK:
        state = _read_state()
        previous = normalize_account(state.get("active", PRIMARY))
        if previous != account or "active" not in state:
            state["active"] = account
            _write_state(state)
    return previous


# ── limits ──────────────────────────────────────────────────────────────


def full_until(rows: list[dict[str, Any]] | None, now: float | None = None) -> float | None:
    """When the 5-hour / weekly windows at 100% reset (the later one); None if none is full."""
    now = time.time() if now is None else now
    until: float | None = None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("id") not in ("hourly", "weekly"):
            continue
        try:
            used = float(row.get("used") or 0)
        except (TypeError, ValueError):
            continue
        if used < 100:
            continue
        at = row.get("resets_at")
        at = float(at) if isinstance(at, (int, float)) and at > 0 else None
        if at is not None and at <= now:
            continue  # Cached bars from before the reset.
        until = max(until or 0.0, at or now + FALLBACK_WAIT_S)
    return until


def _windows(account: int, *, max_age: float = 60.0) -> list[dict[str, Any]] | None:
    try:
        try:
            from .usage import account_windows
        except ImportError:
            from usage import account_windows
        return account_windows(account, max_age=max_age)
    except Exception:
        return None


def account_full(account: int) -> bool:
    """True when this account's 5-hour or weekly limit is used up (checked at most once a minute)."""
    account = normalize_account(account)
    now = time.time()
    if exhausted_until(account) > now:
        return True
    with _LOCK:
        hit = _CHECKED.get(account)
    if hit and now - hit[0] < _CHECK_TTL_S and not hit[1]:
        return False
    until = full_until(_windows(account), now)
    if until:
        mark_exhausted(account, until)
        return True
    with _LOCK:
        _CHECKED[account] = (now, False)
    return False


def note_limit_hit(account: int, reset_at: float | None = None) -> float:
    """A turn hit this account's limit: record until when (message, fresh check, or 15 min)."""
    now = time.time()
    until = reset_at if reset_at and reset_at > now else None
    if until is None:
        until = full_until(_windows(account, max_age=0), now)
    return mark_exhausted(account, until)


def has_credentials(account: int) -> bool:
    try:
        return any(path.is_file() for path in account_credentials_paths(account))
    except Exception:
        return False


def logged_in(account: int, cli_path: str = "") -> bool:
    """Account 1 is assumed logged in; account 2 asks ``claude auth status`` (cached 5 min)."""
    account = normalize_account(account)
    if account != SECONDARY:
        return True
    now = time.time()
    with _LOCK:
        hit = _LOGIN.get(account)
    if hit and now - hit[0] < _LOGIN_TTL_S:
        return hit[1]
    try:
        try:
            from .claude_auth import is_claude_logged_in
        except ImportError:
            from claude_auth import is_claude_logged_in
        ok = bool(is_claude_logged_in(cli_path, account=SECONDARY))
    except Exception:
        ok = False
    with _LOCK:
        _LOGIN[account] = (now, ok)
    return ok


def in_play() -> bool:
    """Account 2 is set up and Settings allows switching to it."""
    # Plugin tests set this so they never read this PC's real account 2 or its usage.
    if os.environ.get("DUCKY_CLAUDE_FAILOVER") == "off":
        return False
    try:
        return has_credentials(SECONDARY) and failover_enabled()
    except Exception:
        return False


def pick_account(cli_path: str = "") -> int:
    """The account the next Claude Code turn should run on (1 unless account 1 is at its limit)."""
    try:
        if not in_play():
            return PRIMARY
        if not account_full(PRIMARY):
            return PRIMARY
        if account_full(SECONDARY):
            first, second = exhausted_until(PRIMARY), exhausted_until(SECONDARY)
            return SECONDARY if 0 < second < first else PRIMARY
        return SECONDARY if logged_in(SECONDARY, cli_path) else PRIMARY
    except Exception:
        return PRIMARY


def can_switch_to(account: int, cli_path: str = "") -> bool:
    """Whether a turn that hit the other account's limit may move to ``account`` now."""
    try:
        account = normalize_account(account)
        if not failover_enabled():
            return False
        if account == SECONDARY and not (has_credentials(SECONDARY) and logged_in(SECONDARY, cli_path)):
            return False
        return not account_full(account)
    except Exception:
        return False


def current_account() -> int:
    """The account chats run on now, from saved state only (no network): feeds the usage meter."""
    try:
        if not in_play():
            return PRIMARY
        with _LOCK:
            state = _read_state()
        if normalize_account(state.get("active", PRIMARY)) == SECONDARY and _until(state, PRIMARY) > time.time():
            return SECONDARY
    except Exception:
        pass
    return PRIMARY


def begin_turn(account: int, session_id: str = "") -> int:
    """Remember the account this turn runs on and bring its session over from the other one.

    Returns the account the previous turn ran on. Without account 2 nothing is touched.
    """
    account = normalize_account(account)
    if account == PRIMARY and not in_play():
        return PRIMARY
    previous = set_active(account)
    if session_id:
        carry_session(session_id, other_account(account), account)
    return previous


# ── Claude Code's limit messages ────────────────────────────────────────

# "Claude AI usage limit reached|1750708800", "Usage limit reached · continuing at 5:50pm",
# "5-hour limit reached ∙ resets 3pm", "Weekly limit reached", "Opus weekly limit reached",
# "You've hit your limit · resets 3pm (Europe/Paris)", "You've hit your session limit",
# "You're out of extra usage · resets …". Not "Context limit reached" (that is /compact).
_LIMIT_RE = re.compile(
    r"usage limit reached"
    r"|\b(?:5-hour|five-hour|session|weekly|daily|opus|sonnet)(?: weekly)? limit reached"
    r"|\byou(?:'|’)?ve hit your (?:[\w-]+ ){0,2}limit"
    r"|\bout of extra usage",
    re.I,
)
_RESET_EPOCH_RE = re.compile(r"limit reached\s*\|\s*(\d{9,13})", re.I)


def is_usage_limit_error(*texts: Any) -> bool:
    """True when any text is Claude Code's plan-limit message (5-hour, weekly, session)."""
    return any(isinstance(text, str) and _LIMIT_RE.search(text) for text in texts)


def limit_reset_epoch(*texts: Any) -> float | None:
    """The reset time a limit message carries (the ``…|<epoch>`` form), else None."""
    for text in texts:
        match = _RESET_EPOCH_RE.search(text) if isinstance(text, str) else None
        if match:
            n = float(match.group(1))
            return n / 1000.0 if n > 10_000_000_000 else n
    return None


# ── sessions ────────────────────────────────────────────────────────────


def session_snapshot(session_id: str, account: int) -> dict[str, int]:
    """Size of each saved copy of this session on ``account``, keyed by its project folder."""
    try:
        if not session_id or not _SESSION_ID_RE.match(session_id):
            return {}
        root = account_config_dir(account) / "projects"
        return {src.parent.name: src.stat().st_size for src in root.glob(f"*/{session_id}.jsonl")}
    except Exception:
        return {}


def carry_session(
    session_id: str, from_account: int, to_account: int, *, upto: dict[str, int] | None = None,
) -> bool:
    """Copy a saved Claude Code session into the other account's folder so ``--resume`` finds it.

    Claude keeps sessions at ``<config>/projects/<cwd-slug>/<id>.jsonl``. The copy is made when
    missing or older than the source. ``upto`` (a ``session_snapshot``) copies only what the
    transcript held then: Claude appends each turn, so a turn that hit the limit and is replayed
    on the other account doesn't show the person's message twice. True when the session is
    available on ``to_account``.
    """
    try:
        if not session_id or not _SESSION_ID_RE.match(session_id):
            return False
        src_root = account_config_dir(from_account) / "projects"
        dst_root = account_config_dir(to_account) / "projects"
        if normalize_account(from_account) == normalize_account(to_account):
            return any(src_root.glob(f"*/{session_id}.jsonl"))
        found = False
        for src in src_root.glob(f"*/{session_id}.jsonl"):
            dst = dst_root / src.parent.name / src.name
            size = (upto or {}).get(src.parent.name)
            try:
                if size is not None:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_name(dst.name + ".tmp")
                    with src.open("rb") as fh:
                        tmp.write_bytes(fh.read(size))
                    tmp.replace(dst)
                elif not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_name(dst.name + ".tmp")
                    shutil.copy2(src, tmp)
                    tmp.replace(dst)
                found = True
            except OSError:
                found = found or dst.is_file()
        return found or any(dst_root.glob(f"*/{session_id}.jsonl"))
    except Exception:
        return False
