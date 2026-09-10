"""Claude Code auth helpers — status check + chat-driven login."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import webbrowser
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


def _default_claude_bin() -> str:
    home = Path.home()
    for p in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"):
        if p.is_file():
            return str(p.resolve())
    return ""


def resolve_claude_bin(override: str = "") -> str:
    found = which_cli("claude", override)
    if found:
        return found
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


def open_auth_url(url: str) -> bool:
    if not url:
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


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


def start_claude_login(
    *,
    conv_id: str,
    cwd: str,
    cli_path: str,
    deferred_prompt: str,
    push: Any,
) -> dict[str, Any]:
    """Open a terminal running `claude auth login`, open browser when URL appears."""
    from frontend.ui_web.terminal import get_terminal_manager

    binary = resolve_claude_bin(cli_path) or "claude"
    mgr = get_terminal_manager()
    workdir = (cwd or "").strip() or os.getcwd()
    if not os.path.isdir(workdir):
        workdir = os.getcwd()

    spawn = mgr.spawn(
        shell="powershell",
        cwd=workdir,
        title="Claude Login",
        push_open=True,
        conv_id=conv_id,
    )
    if not spawn.get("ok"):
        return {"ok": False, "error": str(spawn.get("error") or "failed to open terminal")}

    session_id = str(spawn.get("session_id") or spawn.get("id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "terminal session missing id"}

    session = mgr.get_session(session_id)
    if session is None:
        return {"ok": False, "error": "terminal session disappeared"}
    _push_terminal_open(push, session, conv_id)

    if os.name == "nt":
        cmd = f"& {_ps_quote(binary)} auth login"
    else:
        cmd = f"{binary} auth login"

    session.run_command(cmd, background=True)

    url = ""
    deadline = time.time() + 25.0
    while time.time() < deadline and not url:
        time.sleep(0.4)
        url = extract_auth_url(session.read_output_tail(12000))
        if is_claude_logged_in(cli_path):
            clear_pending_auth(conv_id)
            return {
                "ok": True,
                "logged_in": True,
                "terminal_session_id": session_id,
                "message": "Claude Code is already logged in.",
            }

    opened = open_auth_url(url) if url else False
    set_pending_auth(
        conv_id,
        {
            "agent": "claude_code",
            "terminal_session_id": session_id,
            "auth_url": url,
            "deferred_prompt": deferred_prompt,
            "started": time.time(),
        },
    )

    lines = [
        "## Claude Code login required",
        "",
        "You're not signed in to Claude Code yet. Ducky started the login flow.",
    ]
    if url:
        lines += [
            "",
            "1. A browser window should open (or open this URL):",
            "",
            url,
            "",
        ]
        if opened:
            lines.append("Browser open was requested from Ducky.")
        else:
            lines.append("Browser did not open automatically — paste the URL into Chrome.")
        lines += [
            "",
            "2. Sign in / authorize in the browser.",
            "3. If Claude asks for a code, **paste that code here as your next chat message**.",
            "4. Or reply `done` after the browser finishes.",
            "",
            "Your original request will run automatically after login.",
        ]
    else:
        lines += [
            "",
            "No login URL appeared yet — check the **Claude Login** terminal tab.",
            "When you see the URL, open it in Chrome, then paste the code here (or reply `done`).",
        ]

    msg = "\n".join(lines)
    if push:
        try:
            push({"type": "status", "text": "Claude login required…", "conv_id": conv_id})
        except Exception:
            pass
    return {
        "ok": True,
        "needs_login": True,
        "logged_in": False,
        "auth_url": url,
        "browser_opened": opened,
        "terminal_session_id": session_id,
        "message": msg,
    }


def _ps_quote(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


_REJECT_WORDS = ("invalid", "expired", "incorrect", "failed", "error", "denied")


def _rejection_line(new_output: str) -> str:
    """Claude's own complaint about the pasted code, or '' while it is still working."""
    for line in reversed(_ANSI_RE.sub(" ", new_output or "").splitlines()):
        s = line.strip()
        if s and any(w in s.lower() for w in _REJECT_WORDS):
            return s[:200]
    return ""


def _push_terminal_open(push: Any, session: Any, conv_id: str) -> None:
    """Open/raise the login tab via the run's own push. The manager's push hook
    is only wired once by the panel window; the run push always reaches the chat."""
    if not push:
        return
    try:
        push(
            {
                "type": "terminal_open",
                "session_id": session.id,
                "shell": session.shell,
                "title": session.title,
                "cwd": session.cwd,
                "ws_url": session.ws_url,
                "conv_id": conv_id,
            }
        )
    except Exception:
        pass


def continue_claude_login(
    *,
    conv_id: str,
    user_text: str,
    cli_path: str,
    push: Any = None,
) -> dict[str, Any]:
    """Handle the user's follow-up (auth code or 'done') during pending login."""
    pending = get_pending_auth(conv_id)
    if not pending:
        return {"ok": False, "error": "no pending login"}

    if is_claude_logged_in(cli_path):
        deferred = str(pending.get("deferred_prompt") or "")
        clear_pending_auth(conv_id)
        return {"ok": True, "logged_in": True, "deferred_prompt": deferred, "message": "Login complete."}

    text = (user_text or "").strip().strip("`")
    session_id = str(pending.get("terminal_session_id") or "")
    low = text.lower()
    url = str(pending.get("auth_url") or "")

    if looks_like_auth_code(text) and session_id:
        from frontend.ui_web.terminal import get_terminal_manager

        mgr = get_terminal_manager()
        session = mgr.get_session(session_id)
        if session is None or not session.is_alive():
            return {
                "ok": False,
                "error": "Login terminal closed — send another message to restart login.",
                "restart": True,
                "deferred_prompt": str(pending.get("deferred_prompt") or ""),
            }
        _push_terminal_open(push, session, conv_id)
        try:
            session.write(text + "\r\n")
        except Exception as exc:
            return {"ok": False, "error": f"Could not send code to terminal: {exc}"}

        before = len(session.read_output_tail(60000))
        deadline = time.time() + 45.0
        while time.time() < deadline:
            time.sleep(0.5)
            if is_claude_logged_in(cli_path):
                deferred = str(pending.get("deferred_prompt") or "")
                clear_pending_auth(conv_id)
                return {
                    "ok": True,
                    "logged_in": True,
                    "deferred_prompt": deferred,
                    "message": "Login complete — running your original request…",
                }
            rejected = _rejection_line(session.read_output_tail(60000)[before:])
            if rejected:
                # A rejected OAuth code is single-use — only a fresh URL can recover.
                return {
                    "ok": False,
                    "restart": True,
                    "deferred_prompt": str(pending.get("deferred_prompt") or ""),
                    "error": f"Claude rejected that code: {rejected}",
                }
        return {
            "ok": True,
            "needs_login": True,
            "message": (
                "Code sent — Claude is still finishing sign-in. "
                "Reply `done` in a moment and I'll run your request."
            ),
            "auth_url": url,
            "terminal_session_id": session_id,
        }

    if low in _DONE_WORDS:
        if is_claude_logged_in(cli_path):
            deferred = str(pending.get("deferred_prompt") or "")
            clear_pending_auth(conv_id)
            return {
                "ok": True,
                "logged_in": True,
                "deferred_prompt": deferred,
                "message": "Login complete — running your original request…",
            }
        return {
            "ok": True,
            "needs_login": True,
            "message": (
                "Still not logged in. Open the URL in Chrome, finish authorize, "
                "then reply `done` again — or paste the auth code here.\n\n"
                + url
            ),
            "auth_url": url,
            "terminal_session_id": session_id,
        }

    # Any other message while pending: remind
    return {
        "ok": True,
        "needs_login": True,
        "message": (
            "Claude login is still pending.\n\n"
            "Paste the auth **code** from the browser, or reply `done` after signing in.\n\n"
            + (f"Login URL:\n{url}" if url else "Check the Claude Login terminal for the URL.")
        ),
        "auth_url": url,
        "terminal_session_id": session_id,
    }
