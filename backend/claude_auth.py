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

_URL_RE = re.compile(r"https://(?:claude\.ai|claude\.com|platform\.claude\.com)[^\s\"'<>]+", re.I)
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


def extract_auth_url(text: str) -> str:
    matches = _URL_RE.findall(text or "")
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
    s = (text or "").strip()
    if not s or "\n" in s or " " in s:
        return False
    if s.lower() in ("done", "logged in", "ok", "ready", "continue"):
        return False
    # OAuth codes are typically long tokens
    return 8 <= len(s) <= 512 and re.fullmatch(r"[A-Za-z0-9._\-+/=]+", s) is not None


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


def continue_claude_login(
    *,
    conv_id: str,
    user_text: str,
    cli_path: str,
) -> dict[str, Any]:
    """Handle the user's follow-up (auth code or 'done') during pending login."""
    pending = get_pending_auth(conv_id)
    if not pending:
        return {"ok": False, "error": "no pending login"}

    if is_claude_logged_in(cli_path):
        deferred = str(pending.get("deferred_prompt") or "")
        clear_pending_auth(conv_id)
        return {"ok": True, "logged_in": True, "deferred_prompt": deferred, "message": "Login complete."}

    text = (user_text or "").strip()
    session_id = str(pending.get("terminal_session_id") or "")
    low = text.lower()

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
        try:
            session.write(text + "\r\n")
        except Exception as exc:
            return {"ok": False, "error": f"Could not send code to terminal: {exc}"}

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
        return {
            "ok": False,
            "needs_login": True,
            "message": (
                "Code sent, but Claude is not logged in yet. "
                "Finish in the browser, then reply `done`, or paste a new code."
            ),
            "auth_url": str(pending.get("auth_url") or ""),
            "terminal_session_id": session_id,
        }

    if low in ("done", "logged in", "ok", "ready", "continue"):
        if is_claude_logged_in(cli_path):
            deferred = str(pending.get("deferred_prompt") or "")
            clear_pending_auth(conv_id)
            return {
                "ok": True,
                "logged_in": True,
                "deferred_prompt": deferred,
                "message": "Login complete — running your original request…",
            }
        url = str(pending.get("auth_url") or "")
        return {
            "ok": False,
            "needs_login": True,
            "message": (
                "Still not logged in. Open the URL in Chrome, finish authorize, "
                "then reply `done` again — or paste the auth code here.\n\n"
                + (url if url else "")
            ),
            "auth_url": url,
            "terminal_session_id": session_id,
        }

    # Any other message while pending: remind
    url = str(pending.get("auth_url") or "")
    return {
        "ok": False,
        "needs_login": True,
        "message": (
            "Claude login is still pending.\n\n"
            "Paste the auth **code** from the browser, or reply `done` after signing in.\n\n"
            + (f"Login URL:\n{url}" if url else "Check the Claude Login terminal for the URL.")
        ),
        "auth_url": url,
        "terminal_session_id": session_id,
    }
