"""Claude Code's hooks for core's account registry, and the one-time account 2 move."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import Mock

_pkg = sys.modules.get("backend")
if _pkg is not None and Path(getattr(_pkg, "__file__", "") or ".").resolve().parent == Path(__file__).resolve().parent:
    for _k in [k for k in sys.modules if k == "backend" or k.startswith("backend.")]:
        del sys.modules[_k]

from backend.uefn_plugins import host  # noqa: E402

# Loaded the way Ducky loads the plugin, so its relative imports work.
plugin = host._import_backend("claude_account_hooks_test", Path(__file__).resolve().parents[1], "backend")
_NAME = plugin.__name__
accounts = importlib.import_module(_NAME + ".accounts")
failover = importlib.import_module(_NAME + ".failover")
cli_update = importlib.import_module(_NAME + ".cli_update")


def test_registration_carries_the_account_hooks(monkeypatch):
    monkeypatch.setattr(cli_update, "schedule_cli_update_on_plugin_load", lambda: None)
    monkeypatch.setattr(accounts, "migrate_account2", lambda: None)
    api = Mock()
    plugin.register(api)
    reg = api.register_coding_agent.call_args.kwargs
    assert reg["account_env"]("C:/acc") == {"CLAUDE_CONFIG_DIR": "C:/acc"}
    assert reg["account_env"](None) == {}
    assert reg["is_limit_error"] is failover.is_usage_limit_error
    assert reg["account_shared"]["dirs"] == list(accounts.SHARED_DIRS)
    assert reg["account_shared"]["files"] == list(accounts.SHARED_FILES)
    for hook in ("account_login", "account_status", "account_logout", "account_usage"):
        assert callable(reg[hook])


def test_login_runs_claude_auth_login_in_the_folder(monkeypatch):
    auth = importlib.import_module(_NAME + ".claude_auth")
    monkeypatch.setattr(auth, "resolve_claude_bin", lambda *_a: "claude.exe")
    monkeypatch.setattr(plugin, "_account_cli", lambda: "")
    out = plugin._account_login("C:/acc")
    assert out == {"argv": ["claude.exe", "auth", "login"], "env": {"CLAUDE_CONFIG_DIR": "C:/acc"}}


def test_status_runs_as_the_folder(monkeypatch):
    auth = importlib.import_module(_NAME + ".claude_auth")
    seen = []
    monkeypatch.setattr(auth, "claude_auth_status", lambda cli, **kw: seen.append(kw) or {
        "loggedIn": True, "email": "a@b.c", "subscriptionType": "max"})
    monkeypatch.setattr(plugin, "_account_cli", lambda: "")
    assert plugin._account_status("C:/acc") == {"loggedIn": True, "email": "a@b.c", "plan": "max"}
    assert seen == [{"config_dir": "C:/acc"}]


def test_account2_moves_into_core_once(monkeypatch, tmp_path):
    old = tmp_path / "claude-account-2"
    old.mkdir()
    (old / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(accounts, "secondary_config_dir", lambda: old)
    monkeypatch.setattr(accounts, "preferred_account", lambda: accounts.SECONDARY)
    # Other test files reload the app's `backend`: patch the module migrate_account2 imports now,
    # never this PC's real account store.
    core = importlib.import_module("backend.agent.accounts")
    monkeypatch.setattr(core, "_data_root", lambda: tmp_path / "data")

    new_id = accounts.migrate_account2()
    assert new_id
    assert accounts.migrate_account2() is None  # Nothing left to move.
    rows = core.list_accounts("claude_code")
    assert [(r["label"], r["kind"]) for r in rows] == [("Default", "login"), ("Account 2", "login")]
    assert core.active_account("claude_code") == new_id
    assert (core.account_config_dir("claude_code", new_id) / ".credentials.json").is_file()
    assert not old.exists()
