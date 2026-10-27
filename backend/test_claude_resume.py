from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# Pytest roots the plugin, so its `backend` package hides the app's. Load the
# adapter under another package name with the app still first on sys.path.
_plugin_root = Path(__file__).resolve().parents[1]
_app = _plugin_root.parents[1] / "UEFN-Ducky-Release" / "ducky_app"
if _app.is_dir() and str(_app) not in sys.path:
    sys.path.insert(0, str(_app))
_pkg = types.ModuleType("_claude_adapter_under_test")
_pkg.__path__ = [str(Path(__file__).resolve().parent)]
_pkg.__package__ = "_claude_adapter_under_test"
sys.modules["_claude_adapter_under_test"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "_claude_adapter_under_test.claude_code_adapter",
    Path(__file__).with_name("claude_code_adapter.py"),
)
assert _spec and _spec.loader
sys.modules.pop("backend", None)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
build_claude_argv = _mod.build_claude_argv
claude_extra_dirs = _mod.claude_extra_dirs
claude_env = _mod.claude_env


def test_prompt_cache_ttl_and_expiry_come_from_step_usage(monkeypatch):
    ttl = _mod.prompt_cache_ttl_s
    assert ttl({"ephemeral_1h_input_tokens": 10, "ephemeral_5m_input_tokens": 0}) == 3600
    assert ttl({"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 7}) == 300
    assert ttl({"ephemeral_1h_input_tokens": 0}) == 0 and ttl(None) == 0

    monkeypatch.setattr(_mod.time, "time", lambda: 1000.0)
    state = _mod._StreamState("c", "r", lambda e: None)
    state._ingest_step_usage({"id": "m1", "usage": {"input_tokens": 5, "cache_creation_input_tokens": 50,
                                                     "cache_creation": {"ephemeral_1h_input_tokens": 50}}})
    monkeypatch.setattr(_mod.time, "time", lambda: 1200.0)
    state._ingest_step_usage({"id": "m2", "usage": {"input_tokens": 5, "cache_read_input_tokens": 50}})
    state._parse_result({"usage": {"input_tokens": 10}, "num_turns": 2})
    # A read refreshes the timer at the tier the session wrote: last use + 1 h.
    assert state.usage["cache_expires_at"] == 1200.0 + 3600

    cold = _mod._StreamState("c", "r", lambda e: None)
    cold._parse_result({"usage": {"input_tokens": 10}})
    assert "cache_expires_at" not in cold.usage


def test_auto_compact_setting_goes_in_as_settings_json():
    base = dict(binary="claude", prompt="hi", system_prompt="", model="sonnet", mcp_config_path="mcp.json",
                session_id="", permission_mode="acceptEdits")
    argv = build_claude_argv(extra_args="--settings user.json", auto_compact_window=300000, **base)
    first = argv.index("--settings")
    assert argv[first + 1] == '{"autoCompactWindow": 300000}'
    assert argv.index("--settings", first + 1) > first  # the user's own --settings comes after
    assert "--settings" not in build_claude_argv(extra_args="", **base)


def test_auto_compact_pref_reads_digits_only(monkeypatch):
    import frontend.ui_web.plugin_host_api as host_api

    for raw, want in (("300000", 300000), ("", 0), ("abc", 0)):
        monkeypatch.setattr(host_api, "prefs_plugin_get", lambda _pid, raw=raw: {"autoCompactWindow": raw})
        assert _mod.auto_compact_window() == want


def test_tool_search_is_forced_unless_the_user_set_it(monkeypatch):
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    env = claude_env({"DUCKY_CONV_ID": "c1"})
    assert env["ENABLE_TOOL_SEARCH"] == "true"
    assert env["MCP_TIMEOUT"] == "60000" and env["DUCKY_CONV_ID"] == "c1"
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    assert "ENABLE_TOOL_SEARCH" not in claude_env({})

src = Path(__file__).with_name("claude_code_adapter.py").read_text(encoding="utf-8")


def test_followup_passes_resume_flag():
    assert "argv.extend([\"--resume\", session_id])" in src
    assert "resume=True" in src


def test_first_turn_only_resumes_when_id_set():
    assert "if session_id:" in src
    assert "argv.extend([\"--resume\", session_id])" in src


def test_recent_project_is_add_dir_but_cwd_is_not(tmp_path, monkeypatch):
    active = tmp_path / "ExampleProject1"
    other = tmp_path / "Roguelike"
    active.mkdir()
    other.mkdir()
    import frontend.ui_web.recent_projects as recent

    monkeypatch.setattr(recent, "load_recent_projects", lambda: [str(active), str(other)])
    dirs = claude_extra_dirs(str(active), [])
    argv = build_claude_argv(
        binary="claude",
        prompt="hi",
        system_prompt="",
        model="sonnet",
        mcp_config_path="",
        extra_args="",
        session_id="",
        permission_mode="acceptEdits",
        image_dirs=dirs,
    )
    added = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--add-dir"]
    assert str(other.resolve()) in added
    assert str(active.resolve()) not in added


def test_approval_card_hook_rides_with_the_uefn_bridge():
    base = dict(
        binary="claude",
        prompt="hi",
        system_prompt="",
        model="sonnet",
        extra_args="",
        session_id="",
        permission_mode="acceptEdits",
    )
    with_bridge = build_claude_argv(mcp_config_path="mcp.json", **base)
    i = with_bridge.index("--permission-prompt-tool")
    assert with_bridge[i + 1] == "mcp__uefn__ducky_permission_prompt"
    # No bridge, no hook: the tool would not exist.
    assert "--permission-prompt-tool" not in build_claude_argv(mcp_config_path="", **base)


def test_with_ducky_attached_claude_code_keeps_only_tool_search():
    base = dict(
        binary="claude",
        prompt="hi",
        system_prompt="",
        model="sonnet",
        session_id="",
        permission_mode="acceptEdits",
    )
    argv = build_claude_argv(mcp_config_path="mcp.json", extra_args="--tools default --verbose", **base)
    assert argv.count("--tools") == 1
    assert argv[argv.index("--tools") + 1] == "ToolSearch"
    assert "default" not in argv
    # No Ducky bridge: no tools to fall back on, so Claude Code keeps its own.
    assert "--tools" not in build_claude_argv(mcp_config_path="", extra_args="", **base)
