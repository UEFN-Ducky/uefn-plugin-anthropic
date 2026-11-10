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

from backend.uefn_plugins.plugin_host import backends as host  # noqa: E402

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
    # Core owns account login, status and the picker switch now.
    rpcs = {c.args[0] for c in api.register_panel_rpc.call_args_list}
    assert not rpcs & {"account2_login", "account2_status", "account2_logout", "set_claude_account"}


def test_login_runs_claude_auth_login_in_the_folder(monkeypatch):
    auth = importlib.import_module(_NAME + ".claude_auth")
    monkeypatch.setattr(auth, "resolve_claude_bin", lambda *_a: "claude.exe")
    monkeypatch.setattr(plugin, "_account_cli", lambda: "")
    out = plugin._account_login("C:/acc")
    assert out == {"argv": ["claude.exe", "auth", "login"], "env": {"CLAUDE_CONFIG_DIR": "C:/acc"}}


def _no_cli(monkeypatch):
    auth = importlib.import_module(_NAME + ".claude_auth")
    monkeypatch.setattr(auth, "resolve_claude_bin", lambda *_a: "")
    monkeypatch.setattr(plugin, "_account_cli", lambda: "")
    monkeypatch.setattr(cli_update, "_busy", False)
    monkeypatch.setattr(cli_update, "_last", {})
    monkeypatch.setattr(cli_update, "_failed_at", 0.0)


def _fake_install(monkeypatch, result):
    import threading

    gate, calls = threading.Event(), []

    def fake(_binary=""):
        calls.append(1)
        gate.wait(5)
        cli_update._last = result  # update_claude_cli records its own result
        return result

    monkeypatch.setattr(cli_update, "update_claude_cli", fake)
    return gate, calls


def _wait_idle():
    import time

    for _ in range(100):
        if not cli_update._busy:
            return
        time.sleep(0.02)
    raise AssertionError("install never finished")


def test_login_with_no_cli_installs_it_and_the_row_follows(monkeypatch):
    """Nov 10 2026: Log in with only Claude Desktop did nothing but 'Claude Code CLI not found.'"""
    _no_cli(monkeypatch)
    gate, calls = _fake_install(monkeypatch, {"ok": True, "cli_path": "claude.exe"})
    import pytest

    with pytest.raises(ValueError, match="installing it now"):
        plugin._account_login(None)
    assert plugin._account_status(None) == {"loggedIn": False, "pending": True}
    with pytest.raises(ValueError):
        plugin._account_login(None)  # a second press while installing starts no second install
    gate.set()
    _wait_idle()
    assert calls == [1]
    assert plugin._account_status(None) == {"loggedIn": False}  # installed: plain Log in again


def test_a_failed_install_shows_the_installer_error_line(monkeypatch):
    _no_cli(monkeypatch)
    gate, calls = _fake_install(monkeypatch, {"ok": False, "error": "Downloading...\nirm : The remote name could not be resolved\n"})
    gate.set()
    import pytest

    with pytest.raises(ValueError):
        plugin._account_login(None)
    _wait_idle()
    assert plugin._account_status(None) == {
        "loggedIn": False, "error": "Claude Code install failed: irm : The remote name could not be resolved"}
    with pytest.raises(ValueError):
        plugin._account_login(None)  # Log in again retries at once
    _wait_idle()
    assert calls == [1, 1]


def test_setup_check_with_no_cli_starts_the_install(monkeypatch):
    _no_cli(monkeypatch)
    adapter_mod = importlib.import_module(_NAME + ".claude_code_adapter")
    monkeypatch.setattr(adapter_mod, "resolve_claude_bin", lambda *_a: "")
    monkeypatch.setattr(adapter_mod, "coding_agent_cfg", lambda *_a: {})
    gate, calls = _fake_install(monkeypatch, {"ok": False, "error": "boom"})
    info = adapter_mod.ClaudeCodeAdapter().detect(None)
    assert info.available is False and "Installing Claude Code CLI" in info.status
    gate.set()
    _wait_idle()
    info = adapter_mod.ClaudeCodeAdapter().detect(None)
    assert info.status == "Claude Code install failed: boom"
    assert calls == [1]  # a failed install waits 2 min before Check retries it


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
