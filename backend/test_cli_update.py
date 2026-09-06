from __future__ import annotations

from pathlib import Path

from cli_update import (
    is_cli_too_old_error,
    parse_required_cli_version,
    parse_version_tuple,
    plugin_package_version,
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
    assert plugin_package_version() == "1.0.27"


if __name__ == "__main__":
    test_parse_cli_version()
    test_parse_required_from_fable_error()
    test_plugin_register_schedules_update()
    print("ok")
