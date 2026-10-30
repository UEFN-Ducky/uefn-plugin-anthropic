"""Settings → "Second Claude account": log in, check and log out account 2.

The Settings button calls these through ``plugin_call`` and shows ``{ok, connected,
detail, error}``. Account 1's own Log in modal (``claude_auth.start_claude_login``)
is separate and unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

try:
    from . import claude_auth as auth
    from .accounts import SECONDARY, account_env
except ImportError:
    import claude_auth as auth
    from accounts import SECONDARY, account_env

LOGIN_TITLE = "Claude Login (account 2)"
# A phone / Remote View reaches plugin_call over the direct channel, which gives up
# after 30 s; the desktop window has no limit. Stay under the shorter one.
LOGIN_WAIT_S = 25.0
POLL_S = 2.0
_NOT_LOGGED_IN = "Account 2 isn't logged in."
_FINISH = f"Finish signing in in the {LOGIN_TITLE} tab, then press Log in account 2 again."


def _cli_path() -> str:
    """The Claude Code CLI path set in Settings, or '' for the default lookup."""
    try:
        from backend.agent.coding_agents.settings_helpers import coding_agent_cfg
        from frontend.settings import PanelSettings

        return str(coding_agent_cfg(PanelSettings.load(), "claude_code").get("cli_path") or "")
    except Exception:
        return ""


def _terminal_manager() -> Any:
    from frontend.ui_web.terminal import get_terminal_manager

    return get_terminal_manager()


def _who(status: dict[str, Any]) -> str:
    """'someone@x.com · Max' from `claude auth status --json`."""
    name = str(status.get("email") or status.get("orgName") or "logged in")
    plan = str(status.get("subscriptionType") or "").strip()
    return f"{name} · {plan.capitalize()}" if plan else name


def account2_status(**_kw: Any) -> dict[str, Any]:
    cli_path = _cli_path()
    status = auth.claude_auth_status(cli_path, account=SECONDARY)
    if not status.get("loggedIn"):
        error = str(status.get("error") or "")
        if error:
            return {"ok": False, "connected": False, "error": f"Couldn't check account 2: {error}"}
        return {"ok": True, "connected": False, "detail": _NOT_LOGGED_IN}
    email = str(status.get("email") or "").strip().lower()
    first = auth.claude_auth_status(cli_path)
    if email and first.get("loggedIn") and email == str(first.get("email") or "").strip().lower():
        return {
            "ok": False,
            "connected": True,
            "detail": (
                f"Account 2 is the same Claude account as account 1 ({status.get('email')}). "
                "Log out account 2 and sign in with a different Claude account, "
                "or switching accounts won't help."
            ),
        }
    return {"ok": True, "connected": True, "detail": f"Account 2: {_who(status)}"}


def account2_login(**_kw: Any) -> dict[str, Any]:
    """Open the account 2 login tab and wait a little for the sign-in to finish."""
    cli_path = _cli_path()
    binary = auth.resolve_claude_bin(cli_path)
    if not binary:
        return {"ok": False, "connected": False, "error": "Claude Code CLI not found. Install it, then try again."}
    if auth.is_claude_logged_in(cli_path, account=SECONDARY):
        return account2_status()
    key = auth.ACCOUNT2_LOGIN_CONV
    mgr = _terminal_manager()
    old = auth.get_pending_auth(key) or {}
    session_id = str(old.get("terminal_session_id") or "")
    running = mgr.get_session(session_id) if session_id else None
    # Pressing again while the tab is still open (sign-in took longer than one wait)
    # keeps waiting on that tab instead of restarting the login.
    if running is None or not running.is_alive():
        auth._kill_login_session(session_id)
        auth.clear_pending_auth(key)
        # No BROWSER override: Claude opens the sign-in page itself, and the tab still
        # takes a pasted code when the page shows one.
        spawn = auth.spawn_login_session(
            mgr, str(Path.home()), key, binary, title=LOGIN_TITLE, env_extra=account_env(SECONDARY)
        )
        session_id = str(spawn.get("session_id") or spawn.get("id") or "").strip()
        if not spawn.get("ok") or not session_id:
            return {"ok": False, "connected": False, "error": str(spawn.get("error") or "Couldn't open the login tab.")}
        auth.set_pending_auth(key, {"agent": "claude_code", "account": SECONDARY,
                                    "terminal_session_id": session_id, "started": time.time()})

    url = str(old.get("auth_url") or "") if old.get("terminal_session_id") == session_id else ""
    deadline = time.monotonic() + LOGIN_WAIT_S
    while True:
        time.sleep(POLL_S)
        session = mgr.get_session(session_id)
        alive = bool(session is not None and session.is_alive())
        if session is not None and not url:
            url = auth.extract_auth_url(session.read_output_tail(16000))
        if auth.is_claude_logged_in(cli_path, account=SECONDARY):
            auth.clear_pending_auth(key)
            auth._kill_login_session(session_id)
            return {**account2_status(), "connected": True}
        if (auth.get_pending_auth(key) or {}).get("terminal_session_id") != session_id:
            return {"ok": True, "connected": False, "detail": "Login was restarted."}
        if not alive:
            auth.clear_pending_auth(key)
            return {"ok": False, "connected": False, "error": f"The {LOGIN_TITLE} tab closed before sign-in finished. Press Log in account 2 again."}
        if time.monotonic() >= deadline:
            break
    if url:
        auth.set_pending_auth(key, {**(auth.get_pending_auth(key) or {}), "auth_url": url})
        return {"ok": True, "connected": False, "detail": f"{_FINISH} Sign-in link: {url}"}
    return {"ok": True, "connected": False, "detail": _FINISH}


def account2_logout(**_kw: Any) -> dict[str, Any]:
    old = auth.get_pending_auth(auth.ACCOUNT2_LOGIN_CONV) or {}
    auth._kill_login_session(str(old.get("terminal_session_id") or ""))
    res = auth.claude_logout(_cli_path(), account=SECONDARY)
    if not res.get("ok"):
        return {"ok": False, "connected": bool(res.get("logged_in")), "error": str(res.get("error") or "Logout failed.")}
    return {"ok": True, "connected": False, "detail": "Logged out of account 2."}


def register_account_rpcs(api: Any) -> None:
    api.register_panel_rpc("account2_login", account2_login)
    api.register_panel_rpc("account2_status", account2_status)
    api.register_panel_rpc("account2_logout", account2_logout)
