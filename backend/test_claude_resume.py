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
