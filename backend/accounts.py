"""Two Claude Code accounts on one PC: where each one lives and how to run as it.

Account 1 is Claude Code's usual login (``~/.claude``, or the ``CLAUDE_CONFIG_DIR``
Ducky itself was started with). Account 2 is a second login kept in its own Claude
config folder under Ducky's data, picked by setting ``CLAUDE_CONFIG_DIR`` on the
``claude`` process. Each folder holds that account's credentials, settings and
saved sessions, so the two logins never overwrite each other.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

PRIMARY = 1
SECONDARY = 2
ACCOUNTS = (PRIMARY, SECONDARY)
CONFIG_ENV = "CLAUDE_CONFIG_DIR"


def normalize_account(value: object) -> int:
    """1 or 2; anything else (missing, garbled) is account 1."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return PRIMARY
    return n if n in ACCOUNTS else PRIMARY


def account_label(account: int) -> str:
    return f"Account {normalize_account(account)}"


def secondary_config_dir() -> Path:
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "claude-account-2"


def account_config_dir(account: int) -> Path:
    """The Claude config folder this account reads and writes."""
    if normalize_account(account) == SECONDARY:
        return secondary_config_dir()
    inherited = (os.environ.get(CONFIG_ENV) or "").strip()
    return Path(inherited) if inherited else Path.home() / ".claude"


def account_env(account: int) -> dict[str, str]:
    """Extra environment that makes a ``claude`` process run as this account.

    Empty for account 1 so its process keeps exactly the environment it had before
    two accounts existed.
    """
    if normalize_account(account) != SECONDARY:
        return {}
    folder = secondary_config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    share_config(account_config_dir(PRIMARY), folder)
    return {CONFIG_ENV: str(folder)}


# Everything but the login is account 1's: Ducky's skills, the person's hooks, CLAUDE.md
# and saved sessions (Ducky reads session logs from account 1's folder).
SHARED_DIRS = ("projects", "skills", "agents", "commands", "plugins", "hooks", "output-styles")
SHARED_FILES = ("settings.json", "CLAUDE.md")


def _link_dir(src: Path, dst: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(src), str(dst))  # No admin / Developer Mode needed.
    else:
        os.symlink(src, dst, target_is_directory=True)


def share_config(primary: Path, secondary: Path) -> None:
    """Link account 1's shared folders into account 2's and copy its newer shared files.

    A folder account 2 already has for real is left alone (its sessions are carried over
    per turn instead). Never raises.
    """
    if primary.resolve() == secondary.resolve():
        return
    for name in SHARED_DIRS:
        src, dst = primary / name, secondary / name
        try:
            if name == "projects":
                src.mkdir(parents=True, exist_ok=True)
            if src.is_dir() and not os.path.lexists(dst):
                _link_dir(src, dst)
        except OSError:
            pass
    for name in SHARED_FILES:
        src, dst = primary / name, secondary / name
        try:
            if src.is_file() and (not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime):
                shutil.copy2(src, dst)
        except OSError:
            pass


def account_credentials_paths(account: int) -> list[Path]:
    root = account_config_dir(account)
    return [root / ".credentials.json", root / "credentials.json"]


def plugin_pref(key: str, default: object) -> object:
    """One of this plugin's Settings values, or ``default`` when unset / unreadable."""
    try:
        from frontend.ui_web.plugin_host_api import prefs_plugin_get

        prefs = prefs_plugin_get("anthropic")
    except Exception:
        return default
    return prefs.get(key, default)


def failover_enabled() -> bool:
    """Settings → "Switch accounts when one is at its limit" (on by default)."""
    return plugin_pref("accountFailover", True) is not False


PREFERRED_PREF = "claudeAccount"


def preferred_account() -> int:
    """The account the person picked for Claude Code turns (Settings or the model picker)."""
    return normalize_account(plugin_pref(PREFERRED_PREF, PRIMARY))


def set_preferred_account(account: object) -> int:
    from frontend.ui_web.plugin_host_api import prefs_plugin_get, prefs_plugin_set

    n = normalize_account(account)
    prefs_plugin_set("anthropic", {**prefs_plugin_get("anthropic"), PREFERRED_PREF: str(n)})
    return n
