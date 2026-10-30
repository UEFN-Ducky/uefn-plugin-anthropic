"""Claude Code's own login folder, what every other account shares with it, and the
one-time move of PR #3's second login into core's accounts.

Core keeps the accounts (``backend.agent.accounts``): ``default`` is Claude Code's usual
login (``~/.claude``, or the ``CLAUDE_CONFIG_DIR`` Ducky itself was started with); every
other account is its own Claude config folder, picked by setting ``CLAUDE_CONFIG_DIR``.
"""

from __future__ import annotations

import os
from pathlib import Path

PRIMARY = 1
SECONDARY = 2
CONFIG_ENV = "CLAUDE_CONFIG_DIR"
CORE_AGENT = "claude_code"

# Everything but the login is the default account's: Ducky's skills, the person's hooks,
# CLAUDE.md and saved sessions (Ducky reads session logs from the default folder).
SHARED_DIRS = ("projects", "skills", "agents", "commands", "plugins", "hooks", "output-styles")
SHARED_FILES = ("settings.json", "CLAUDE.md")


def account_config_dir(account: int = PRIMARY) -> Path:
    """Claude Code's own config folder (the ``default`` account)."""
    inherited = (os.environ.get(CONFIG_ENV) or "").strip()
    return Path(inherited) if inherited else Path.home() / ".claude"


def secondary_config_dir() -> Path:
    """Where PR #3 kept account 2; only read to move it into core once."""
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "claude-account-2"


def plugin_pref(key: str, default: object) -> object:
    """One of this plugin's Settings values, or ``default`` when unset / unreadable."""
    try:
        from frontend.ui_web.plugin_host_api import prefs_plugin_get

        prefs = prefs_plugin_get("anthropic")
    except Exception:
        return default
    return prefs.get(key, default)


def preferred_account() -> int:
    """PR #3's "Use account" pick (1 or 2)."""
    return SECONDARY if str(plugin_pref("claudeAccount", PRIMARY)).strip() == "2" else PRIMARY


def migrate_account2() -> str | None:
    """Move a logged-in PR #3 account 2 folder into a core account named "Account 2", once.

    The folder is moved (not copied), so a second call finds nothing to do. Its "Use account"
    pick and "Switch accounts" switch carry over. Returns the new account id, or None when
    there was nothing to move or the move failed.
    """
    old = secondary_config_dir()
    if not any((old / name).is_file() for name in (".credentials.json", "credentials.json")):
        return None
    from backend.agent import accounts as core

    new_id = core.add_account(CORE_AGENT, "Account 2", "login")
    try:
        target = core.account_config_dir(CORE_AGENT, new_id)
        target.rmdir()  # Created empty above; the old folder takes its place.
        os.replace(old, target)
    except OSError:
        core.remove_account(CORE_AGENT, new_id)
        return None
    try:
        if preferred_account() == SECONDARY:
            core.set_active_account(CORE_AGENT, new_id)
        if plugin_pref("accountFailover", True) is False:
            core.set_auto_switch(CORE_AGENT, False)
    except Exception:
        pass
    return new_id
