"""Claude Code auth helpers — status check + chat-driven login."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from backend.agent.coding_agents.base import which_cli
from frontend.settings import default_app_data_dir

_URL_RE = re.compile(
    r"https://(?:claude\.ai|claude\.com|platform\.claude\.com)[^\s\"'<>\x00-\x1f\x7f]+", re.I
)
# CSI (colors/cursor), OSC (hyperlinks: ESC ] 8 ;; url BEL) and stray BEL bytes.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x07")
_DONE_WORDS = frozenset({"done", "logged in", "ok", "ready", "continue", "yes", "y"})
_PENDING_PATH = default_app_data_dir() / "coding_agents" / "auth_pending.json"
SETTINGS_LOGIN_HREF = "ducky://settings.llms/anthropic#login"
SETTINGS_CONV = "__settings__"
_URL_WAIT_S = 12.0
_LOGIN_GEN = 0
_LOGIN_GEN_LOCK = threading.Lock()
_SESSION_DEAD = "The login tab closed. Use the new sign-in link, then paste a fresh code."


def _next_login_gen() -> int:
    global _LOGIN_GEN
    with _LOGIN_GEN_LOCK:
        _LOGIN_GEN += 1
        return _LOGIN_GEN


def _is_current_login_gen(gen: int) -> bool:
    with _LOGIN_GEN_LOCK:
        return gen == _LOGIN_GEN


def _default_claude_bin() -> str:
    home = Path.home()
    for p in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"):
        if p.is_file():
            return str(p.resolve())
    return ""


def _prefer_native_bin(path: str) -> str:
    """Prefer claude.exe over .cmd/.ps1 so login stdin is not a PowerShell prompt."""
    p = Path(path)
    if p.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return path
    for cand in (p.with_suffix(".exe"), p.parent / "claude.exe", Path.home() / ".local" / "bin" / "claude.exe"):
        if cand.is_file():
            return str(cand.resolve())
    return path


def resolve_claude_bin(override: str = "") -> str:
    found = which_cli("claude", override)
    if found:
        return _prefer_native_bin(found)
    return _default_claude_bin()


def claude_auth_status(cli_path: str = "") -> dict[str, Any]:
    """Return {loggedIn, ...} from `claude auth status --json`."""
    binary = resolve_claude_bin(cli_path)
    if not binary:
        return {"loggedIn": False, "error": "claude CLI not found"}
    try:
        from frontend.ui_web.terminal.path_env import env_with_fresh_path

        env = env_with_fresh_path()
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 20,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run([binary, "auth", "status", "--json"], **kwargs)
        text = (proc.stdout or proc.stderr or "").strip()
        if not text:
            return {"loggedIn": False, "error": "empty auth status"}
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        return {"loggedIn": False, "error": str(exc)}
    return {"loggedIn": False}


def is_claude_logged_in(cli_path: str = "") -> bool:
    return bool(claude_auth_status(cli_path).get("loggedIn"))


def _logout_argv(binary: str) -> list[str]:
    return [binary, "auth", "logout"]


def claude_logout(cli_path: str = "") -> dict[str, Any]:
    """Sign out of the Claude Code CLI so Settings can re-run the login flow."""
    binary = resolve_claude_bin(cli_path)
    if not binary:
        return {"ok": False, "error": "claude CLI not found"}
    try:
        from frontend.ui_web.terminal.path_env import env_with_fresh_path

        env = env_with_fresh_path()
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 20,
            "env": env,
            "input": "y\n",
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(_logout_argv(binary), **kwargs)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        if _PENDING_PATH.is_file():
            _PENDING_PATH.unlink()
    except OSError:
        pass
    if is_claude_logged_in(cli_path):
        err = ((proc.stderr or proc.stdout or "logout did not take effect").strip())[:300]
        return {"ok": False, "logged_in": True, "error": err}
    return {"ok": True, "logged_in": False, "message": "Logged out of Claude Code."}


def extract_auth_url(text: str) -> str:
    # Terminal output wraps the URL in an OSC-8 hyperlink + color codes; strip
    # them first or the escape bytes end up inside the URL we open.
    matches = _URL_RE.findall(_ANSI_RE.sub(" ", text or ""))
    for url in matches:
        if "oauth" in url.lower() or "authorize" in url.lower() or "login" in url.lower():
            return url.rstrip(").,]}")
    return matches[0].rstrip(").,]}") if matches else ""


def tail_says_logged_in(text: str) -> bool:
    s = _ANSI_RE.sub(" ", text or "").lower()
    return "logged in" in s and "not logged" not in s


def _load_pending() -> dict[str, Any]:
    try:
        if _PENDING_PATH.is_file():
            data = json.loads(_PENDING_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_pending(data: dict[str, Any]) -> None:
    _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_pending_auth(conv_id: str) -> dict[str, Any] | None:
    row = _load_pending().get(conv_id)
    return row if isinstance(row, dict) else None


def set_pending_auth(conv_id: str, payload: dict[str, Any]) -> None:
    data = _load_pending()
    data[conv_id] = payload
    _save_pending(data)


def clear_pending_auth(conv_id: str) -> None:
    data = _load_pending()
    if conv_id in data:
        data.pop(conv_id, None)
        _save_pending(data)


def looks_like_auth_code(text: str) -> bool:
    """One pasted token, not a sentence. Claude's code is `code#state` — keep `#`
    and any other URL-ish punctuation; only whitespace disqualifies."""
    s = (text or "").strip().strip("`")
    if not s or any(ch.isspace() for ch in s):
        return False
    if s.lower() in _DONE_WORDS:
        return False
    return 8 <= len(s) <= 2048


def settings_login_message() -> str:
    return "\n".join(
        [
            "## Claude Code login required",
            "",
            "Login happens in **Settings**, not in this chat. Do not paste a code here.",
            "",
            f"[Open Settings → Anthropic and log in]({SETTINGS_LOGIN_HREF})",
            "",
            "That opens Settings → LLMs → Anthropic and highlights **Log in**. "
            "Press it — a window shows the sign-in link and a box for the code.",
        ]
    )


def push_open_settings_login(push: Any) -> None:
    if not push:
        return
    try:
        push(
            {
                "type": "open_coding_agent_login",
                "provider_id": "anthropic",
                "title": "Log in to Claude Code",
                "text": (
                    "Press Log in. A window shows the sign-in link and a box "
                    "for the code — never paste a code in chat."
                ),
            }
        )
    except Exception:
        pass


def prompt_claude_login_in_settings(
    *,
    conv_id: str,
    deferred_prompt: str,
    push: Any,
) -> dict[str, Any]:
    """Chat path: open Settings + spotlight Log in. Never collect codes in chat."""
    set_pending_auth(
        conv_id,
        {
            "agent": "claude_code",
            "deferred_prompt": deferred_prompt,
            "started": time.time(),
            "via": "settings",
        },
    )
    push_open_settings_login(push)
    return {
        "ok": True,
        "needs_login": True,
        "logged_in": False,
        "message": settings_login_message(),
    }


LOGIN_NO_BROWSER = "ducky-do-not-open-browser"


def _login_env_extra() -> dict[str, str]:
    """Claude must not auto-open a browser — the modal link is the only open."""
    return {"BROWSER": LOGIN_NO_BROWSER}


def _pending_session(conv_id: str) -> tuple[dict[str, Any] | None, Any]:
    """Return (pending row, live terminal session or None)."""
    pending = get_pending_auth(conv_id)
    if not pending:
        return None, None
    sid = str(pending.get("terminal_session_id") or "").strip()
    if not sid:
        return pending, None
    try:
        from frontend.ui_web.terminal import get_terminal_manager

        session = get_terminal_manager().get_session(sid)
    except Exception:
        return pending, None
    if session is None or not session.is_alive():
        return pending, None
    return pending, session


def _url_from_session(session: Any, pending: dict[str, Any] | None = None) -> str:
    url = extract_auth_url(session.read_output_tail(16000) if session else "")
    if not url and pending:
        url = str(pending.get("auth_url") or "")
    return url


def _kill_login_session(session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    try:
        from frontend.ui_web.terminal import get_terminal_manager

        get_terminal_manager().kill(sid, push_close=True)
    except Exception:
        pass


def spawn_login_session(mgr: Any, workdir: str, conv_id: str, binary: str = "") -> dict[str, Any]:
    """Listed Claude Login session. Do not focus the editor tab (that kills the popup)."""
    common: dict[str, Any] = {
        "cwd": workdir,
        "title": "Claude Login",
        "push_open": True,
        "hidden": False,
        "conv_id": conv_id,
    }
    extra = _login_env_extra()
    if binary:
        try:
            return mgr.spawn(
                **common,
                command=[binary, "auth", "login"],
                env_extra=extra,
                activate=False,
            )
        except TypeError:
            try:
                return mgr.spawn(
                    **common,
                    command=[binary, "auth", "login"],
                    env_extra=extra,
                )
            except TypeError:
                pass
    try:
        spawn = mgr.spawn(shell="powershell", **common, activate=False)
    except TypeError:
        spawn = mgr.spawn(shell="powershell", **common)
    session_id = str(spawn.get("session_id") or spawn.get("id") or "").strip()
    session = mgr.get_session(session_id) if spawn.get("ok") and session_id else None
    if session is not None and binary:
        if os.name == "nt":
            session.run_command(f"& {_ps_quote(binary)} auth login", background=True)
        else:
            session.run_command(f"{binary} auth login", background=True)
    return spawn


def start_claude_login(
    *,
    conv_id: str,
    cwd: str,
    cli_path: str,
    deferred_prompt: str,
    push: Any,
) -> dict[str, Any]:
    """Start `claude auth login` as a listed terminal tab. User clicks the URL."""
    from frontend.ui_web.terminal import get_terminal_manager

    _ = push
    gen = _next_login_gen()
    old = get_pending_auth(conv_id) or {}
    _kill_login_session(str(old.get("terminal_session_id") or ""))

    binary = resolve_claude_bin(cli_path)
    if not binary:
        return {"ok": False, "error": "claude CLI not found"}
    mgr = get_terminal_manager()
    workdir = (cwd or "").strip() or os.getcwd()
    if not os.path.isdir(workdir):
        workdir = os.getcwd()

    spawn = spawn_login_session(mgr, workdir, conv_id, binary)
    if not spawn.get("ok"):
        return {"ok": False, "error": str(spawn.get("error") or "failed to start login")}

    session_id = str(spawn.get("session_id") or spawn.get("id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "login session missing id"}

    session = mgr.get_session(session_id)
    if session is None:
        return {"ok": False, "error": "login session disappeared"}

    url = ""
    deadline = time.time() + _URL_WAIT_S
    while time.time() < deadline and not url:
        time.sleep(0.25)
        if not _is_current_login_gen(gen):
            _kill_login_session(session_id)
            return {"ok": False, "error": "Login was restarted. Press Log in again."}
        url = extract_auth_url(session.read_output_tail(16000))

    if not _is_current_login_gen(gen):
        _kill_login_session(session_id)
        return {"ok": False, "error": "Login was restarted. Press Log in again."}

    set_pending_auth(
        conv_id,
        {
            "agent": "claude_code",
            "terminal_session_id": session_id,
            "auth_url": url,
            "deferred_prompt": deferred_prompt,
            "started": time.time(),
            "gen": gen,
        },
    )
    return {
        "ok": True,
        "needs_login": True,
        "logged_in": False,
        "login_ui": "code_modal",
        "auth_url": url,
        "terminal_session_id": session_id,
        "message": "Click the sign-in link, then paste the code in the box.",
    }


def claude_login_status(*, cli_path: str = "", conv_id: str = SETTINGS_CONV) -> dict[str, Any]:
    """Refresh the sign-in URL from the login tab; report logged_in when done."""
    pending, session = _pending_session(conv_id)
    if session is not None and tail_says_logged_in(session.read_output_tail(16000)):
        if is_claude_logged_in(cli_path):
            sid = str((pending or {}).get("terminal_session_id") or "")
            clear_pending_auth(conv_id)
            _kill_login_session(sid)
            return {"ok": True, "logged_in": True, "needs_login": False}
    url = _url_from_session(session, pending) if session is not None else str((pending or {}).get("auth_url") or "")
    if url and pending and session is not None and url != pending.get("auth_url"):
        pending = {**pending, "auth_url": url}
        set_pending_auth(conv_id, pending)
    if pending and session is None:
        return {
            "ok": True,
            "logged_in": False,
            "needs_login": True,
            "session_alive": False,
            "login_ui": "code_modal",
            "auth_url": url,
            "error": "The login tab closed. Press Log in again.",
            "terminal_session_id": "",
        }
    return {
        "ok": True,
        "logged_in": False,
        "needs_login": True,
        "session_alive": session is not None,
        "login_ui": "code_modal",
        "auth_url": url,
        "terminal_session_id": str((pending or {}).get("terminal_session_id") or ""),
    }


def submit_claude_login_code(
    *,
    code: str,
    cli_path: str = "",
    conv_id: str = SETTINGS_CONV,
    cwd: str = "",
    **_kw: Any,
) -> dict[str, Any]:
    """Write the OAuth code into the listed `claude auth login` tab, then close it."""
    text = (code or "").strip().strip("`")
    if not looks_like_auth_code(text):
        return {"ok": False, "error": "That doesn't look like a login code."}

    pending, session = _pending_session(conv_id)
    if session is None:
        if not pending:
            return {"ok": False, "error": _SESSION_DEAD}
        started = start_claude_login(
            conv_id=conv_id,
            cwd=cwd,
            cli_path=cli_path,
            deferred_prompt="",
            push=None,
        )
        return {
            "ok": False,
            "restarted": True,
            "auth_url": str(started.get("auth_url") or ""),
            "error": str(started.get("error") or _SESSION_DEAD),
        }

    offset = len(session.read_output_tail(16000))
    session.write(text + "\r\n")
    deadline = time.time() + 25.0
    next_cli = time.time() + 2.0
    while time.time() < deadline:
        time.sleep(0.35)
        tail = session.read_output_tail(16000)
        reject = _rejection_line(tail[offset:])
        if reject:
            return {"ok": False, "logged_in": False, "error": reject}
        if tail_says_logged_in(tail) or time.time() >= next_cli:
            next_cli = time.time() + 4.0
            if is_claude_logged_in(cli_path):
                sid = str((pending or {}).get("terminal_session_id") or session.id)
                clear_pending_auth(conv_id)
                _kill_login_session(sid)
                return {"ok": True, "logged_in": True, "message": "Logged in to Claude Code."}
    return {
        "ok": False,
        "logged_in": False,
        "error": "Still waiting for Claude to accept the code.",
    }


def cancel_claude_login(*, conv_id: str = SETTINGS_CONV, **_kw: Any) -> dict[str, Any]:
    """Close the Claude Login tab — cancel / modal dismiss."""
    pending = get_pending_auth(conv_id) or {}
    sid = str(pending.get("terminal_session_id") or "")
    clear_pending_auth(conv_id)
    _kill_login_session(sid)
    return {"ok": True, "cancelled": True}


def _ps_quote(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


_REJECT_WORDS = ("invalid", "expired", "incorrect", "denied")


def _rejection_line(new_output: str) -> str:
    """Claude's own complaint about the pasted code, or '' while it is still working."""
    for line in reversed(_ANSI_RE.sub(" ", new_output or "").splitlines()):
        s = line.strip()
        low = s.lower()
        if "commandnotfound" in low or "fullyqualifiederrorid" in low:
            continue
        if "oauth error" in low or "login failed" in low:
            return s[:200]
        if s and any(w in low for w in _REJECT_WORDS):
            return s[:200]
    return ""


def continue_claude_login(
    *,
    conv_id: str,
    user_text: str,
    cli_path: str,
    push: Any = None,
) -> dict[str, Any]:
    """Follow-up while Settings login is pending. Chat never accepts an auth code."""
    pending = get_pending_auth(conv_id)
    if not pending:
        return {"ok": False, "error": "no pending login"}

    if is_claude_logged_in(cli_path):
        deferred = str(pending.get("deferred_prompt") or "")
        clear_pending_auth(conv_id)
        return {"ok": True, "logged_in": True, "deferred_prompt": deferred, "message": "Login complete."}

    text = (user_text or "").strip().strip("`")
    low = text.lower()
    push_open_settings_login(push)

    if looks_like_auth_code(text):
        return {
            "ok": True,
            "needs_login": True,
            "message": (
                "Don't paste login codes in chat — they belong in Settings.\n\n"
                + settings_login_message()
            ),
        }

    if low in _DONE_WORDS:
        return {
            "ok": True,
            "needs_login": True,
            "message": (
                "Still not logged in. Use the highlighted **Log in** on "
                "Settings → LLMs → Anthropic, then reply `done`.\n\n"
                + settings_login_message()
            ),
        }

    return {
        "ok": True,
        "needs_login": True,
        "message": settings_login_message(),
    }
