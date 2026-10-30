"""Claude Code on core's accounts: which login a turn runs as, and moving on when one is full.

Core picks the account before launch (``coding_agent_account_env``) and keeps which one
is exhausted until when. This module reads those answers for the adapter, reports a limit
hit, asks core for the next account, and carries the chat's saved session between account
folders so ``--resume`` finds it. Nothing here raises: an unknown answer keeps the turn as is.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

try:
    from .accounts import PRIMARY, account_config_dir as _plugin_config_dir
except ImportError:
    from accounts import PRIMARY, account_config_dir as _plugin_config_dir

AGENT = "claude_code"
CONFIG_ENV = "CLAUDE_CONFIG_DIR"
FALLBACK_WAIT_S = 15 * 60
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _core() -> Any:
    from backend.agent import accounts

    return accounts


def in_play() -> bool:
    """Two or more Claude Code accounts exist."""
    # Plugin tests set this so they never read or mark this PC's real accounts.
    if os.environ.get("DUCKY_CLAUDE_FAILOVER") == "off":
        return False
    try:
        return len(_core().list_accounts(AGENT)) > 1
    except Exception:
        return False


def current_account() -> str:
    """The account core launched this turn as."""
    try:
        return _core().current_account(AGENT)
    except Exception:
        return "default"


def picked_account() -> str:
    try:
        return _core().active_account(AGENT)
    except Exception:
        return "default"


def label(account: str) -> str:
    try:
        return next((a["label"] for a in _core().list_accounts(AGENT) if a["id"] == account), account)
    except Exception:
        return account


def config_dir(account: str) -> Path:
    """The Claude config folder this account reads and writes (``default`` = Claude's own)."""
    try:
        folder = _core().account_config_dir(AGENT, account)
    except Exception:
        folder = None
    return Path(folder) if folder else _plugin_config_dir(PRIMARY)


def account_dirs() -> list[Path]:
    try:
        return [config_dir(a["id"]) for a in _core().list_accounts(AGENT)]
    except Exception:
        return []


def run_env(env: dict[str, str], account_env: dict[str, str]) -> dict[str, str]:
    """The turn's env with this account's (``{}`` = Claude's own login) in place of the launch one."""
    base = {k: v for k, v in env.items() if k != CONFIG_ENV}
    return {**base, **account_env}


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


def _windows(account: str) -> list[dict[str, Any]] | None:
    try:
        try:
            from .usage import account_windows
        except ImportError:
            from usage import account_windows
        folder = _core().account_config_dir(AGENT, account)
        return account_windows(str(folder) if folder else PRIMARY, max_age=0)
    except Exception:
        return None


def report_limit(account: str, reset_at: float | None = None) -> None:
    """A turn hit this account's limit: core marks it full until the message's reset, a fresh
    usage check's, or 15 min."""
    now = time.time()
    until = reset_at if reset_at and reset_at > now else full_until(_windows(account), now)
    try:
        _core().report_limit(AGENT, account, until)
    except Exception:
        pass


def next_account(account: str) -> tuple[str | None, dict[str, str]]:
    """Core's next pick after ``account`` filled up, with its launch env; (None, {}) when it is
    ``account``, or full too (auto-switch off, or every account is at its limit)."""
    try:
        env = _core().coding_agent_account_env(AGENT)
        other = _core().current_account(AGENT)
        if other == account or _core().exhausted_until(AGENT, other):
            return None, {}
    except Exception:
        return None, {}
    return other, env


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


def session_snapshot(session_id: str, folder: Path) -> dict[str, int]:
    """Size of each saved copy of this session in a config folder, keyed by its project folder."""
    try:
        if not session_id or not _SESSION_ID_RE.match(session_id):
            return {}
        root = Path(folder) / "projects"
        return {src.parent.name: src.stat().st_size for src in root.glob(f"*/{session_id}.jsonl")}
    except Exception:
        return {}


def carry_session(
    session_id: str, from_dir: Path, to_dir: Path, *, upto: dict[str, int] | None = None,
) -> bool:
    """Copy a saved Claude Code session into another account's folder so ``--resume`` finds it.

    Claude keeps sessions at ``<config>/projects/<cwd-slug>/<id>.jsonl``. The copy is made when
    missing or older than the source. ``upto`` (a ``session_snapshot``) copies only what the
    transcript held then: Claude appends each turn, so a turn that hit the limit and is replayed
    on another account doesn't show the person's message twice. True when the session is
    available in ``to_dir``.
    """
    try:
        if not session_id or not _SESSION_ID_RE.match(session_id):
            return False
        src_root, dst_root = Path(from_dir) / "projects", Path(to_dir) / "projects"
        if Path(from_dir).resolve() == Path(to_dir).resolve():
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


def gather_session(session_id: str, folder: Path) -> None:
    """Bring the newest copy of this session from any account's folder into ``folder``."""
    if not session_id:
        return
    for other in account_dirs():
        carry_session(session_id, other, folder)
