from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

import cli_update
from cli_update import (
    is_cli_too_old_error,
    parse_required_cli_version,
    parse_version_tuple,
    plugin_package_version,
    schedule_cli_update_on_plugin_load,
    update_claude_cli,
    version_less,
)

_ERR = (
    "API Error: 400 Claude Code 2.1.206 does not support this model; "
    "version 2.1.251 or newer is required. Run 'claude update', or update "
    "the Claude desktop app, then try again."
)


def test_parse_cli_version():
    assert parse_version_tuple("2.1.206 (Claude Code)") == (2, 1, 206)
    assert parse_version_tuple("not a version") is None


def test_parse_required_from_fable_error():
    assert parse_required_cli_version(_ERR) == (2, 1, 251)
    assert is_cli_too_old_error(_ERR)
    assert not is_cli_too_old_error("rate limit exceeded")
    assert version_less((2, 1, 206), (2, 1, 251))
    assert not version_less((2, 1, 251), (2, 1, 251))


def test_plugin_register_schedules_update():
    init = Path(__file__).with_name("__init__.py").read_text(encoding="utf-8")
    adapter = Path(__file__).with_name("claude_code_adapter.py").read_text(encoding="utf-8")
    assert "schedule_cli_update_on_plugin_load" in init
    assert "update_claude_cli" in adapter
    assert "is_cli_too_old_error" in adapter
    assert parse_version_tuple(plugin_package_version()), "plugin.json version unreadable"


def _core_reason(monkeypatch, reason):
    core = types.ModuleType("frontend.ide_apply")
    core.auto_apply_skip_reason = lambda: reason
    monkeypatch.setitem(sys.modules, "frontend.ide_apply", core)


@pytest.mark.parametrize("older_core", [False, True])
def test_sandboxed_ducky_never_runs_claude(monkeypatch, older_core):
    """A sandboxed test build (panel port 4300) ran `claude update` against the
    person's real ~/.local/bin/claude.exe; neither plugin load nor the heal may."""
    run = Mock(side_effect=AssertionError("claude CLI spawned"))
    started = []
    monkeypatch.setattr(cli_update, "_run", run)
    monkeypatch.setattr(cli_update, "needs_plugin_load_update", Mock(return_value=True))
    monkeypatch.setattr(cli_update, "threading", types.SimpleNamespace(
        Thread=lambda **kw: types.SimpleNamespace(start=lambda: started.append(kw["name"])),
    ))
    monkeypatch.delenv("DUCKY_SKIP_CLI_UPDATE", raising=False)
    monkeypatch.setenv("UEFN_DUCKY_PANEL_PORT", "4300")
    reason = "panel port 4300 is not the default 4200"
    if older_core:
        monkeypatch.setitem(sys.modules, "frontend.ide_apply", None)  # core without the check
    else:
        _core_reason(monkeypatch, reason)
    schedule_cli_update_on_plugin_load()
    assert started == []
    result = update_claude_cli("claude.exe")
    assert result["ok"] is False and reason in result["error"]
    run.assert_not_called()

    _core_reason(monkeypatch, "")  # the person's own Ducky still updates
    schedule_cli_update_on_plugin_load()
    assert started == ["claude-cli-update"]


if __name__ == "__main__":
    test_parse_cli_version()
    test_parse_required_from_fable_error()
    test_plugin_register_schedules_update()
    print("ok")
