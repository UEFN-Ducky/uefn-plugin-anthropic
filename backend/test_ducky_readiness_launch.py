"""Config preflight regressions; mocked CLI success is not live MCP readiness."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
_app = _root.parents[1] / "UEFN-Ducky-Release" / "ducky_app"
sys.path.insert(0, str(_app))
_pkg = types.ModuleType("_claude_readiness_test")
_pkg.__path__ = [str(_root / "backend")]
sys.modules[_pkg.__name__] = _pkg
_spec = importlib.util.spec_from_file_location(
    _pkg.__name__ + ".claude_code_adapter", _root / "backend/claude_code_adapter.py"
)
assert _spec and _spec.loader
adapter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = adapter
# Do not leave an orphaned backend package for the existing resume tests,
# which also load the plugin alongside the app's identically named package.
_saved_backend = {
    key: sys.modules.pop(key) for key in list(sys.modules)
    if key == "backend" or key.startswith("backend.")
}
try:
    _spec.loader.exec_module(adapter)
    _app_backend = {
        key: value for key, value in sys.modules.items()
        if key == "backend" or key.startswith("backend.")
    }
finally:
    for _key in list(sys.modules):
        if _key == "backend" or _key.startswith("backend."):
            del sys.modules[_key]
    sys.modules.update(_saved_backend)

ERROR = "Ducky tools unavailable: invalid or unreadable MCP configuration."
MARKER = "SYNTHETIC_PRIVATE_CONFIG_MARKER"


@pytest.fixture
def launch(tmp_path, monkeypatch):
    for key, value in _app_backend.items():
        monkeypatch.setitem(sys.modules, key, value)
    # All ordinary tests must explicitly opt into mocked CLI execution.
    def forbidden(*args, **kwargs):
        pytest.fail("CLI resolution/process/setup reached before preflight passed")

    monkeypatch.setattr(adapter, "resolve_claude_bin", forbidden)
    monkeypatch.setattr(adapter, "run_streaming_process", forbidden)
    monkeypatch.setattr(adapter, "claude_extra_dirs", forbidden)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    events = []

    def run(path, session="", **kwargs):
        return adapter.ClaudeCodeAdapter().launch(
            prompt="test user task", system_prompt="", cwd=str(tmp_path),
            conv_id="readiness-test", model="sonnet", mcp_config_path=path,
            extra_args="", cli_path="", env={}, push=events.append,
            session_id=session, **kwargs,
        )

    run.events = events
    return run


BAD_CONFIGS = [
    b"", b"{", b"\xff\xfe\x80", b"[]", b"null", b"7",
    b"{}", b'{"mcpServers":[]}', b'{"mcpServers":{}}',
    b'{"mcpServers":{"uefn":null}}', b'{"mcpServers":{"uefn":{}}}',
    b'{"mcpServers":{"uefn":{"command":"node","args":7}}}',
    b'{"mcpServers":{"uefn":{"command":"node","args":[7]}}}',
    b'{"mcpServers":{"uefn":{"command":7}}}',
    b'{"mcpServers":{"uefn":{"command":" "}}}',
    b'{"mcpServers":{"uefn":{"command":"node","env":{"KEY":7}}}}',
    b'{"mcpServers":{"uefn":{"type":"unknown","command":"node"}}}',
    b'{"mcpServers":{"uefn":{"url":"https://example.invalid/mcp"}}}',
    b'{"mcpServers":{"uefn":{"type":"http","url":7}}}',
    b'{"mcpServers":{"uefn":{"type":"http","url":"file:///bad"}}}',
    b'{"mcpServers":{"uefn":{"type":"http","url":"https://"}}}',
    b'{"mcpServers":{"uefn":{"type":"http","url":"https://example.invalid","headers":[]}}}',
]


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("raw", BAD_CONFIGS)
def test_invalid_config_fails_before_cli(tmp_path, launch, session, raw):
    path = tmp_path / "mcp.json"
    path.write_bytes(raw)
    result = launch(str(path), session)
    assert not result.ok and result.status == "error"
    assert result.error == ERROR
    assert result.upstream_session_id == session
    assert launch.events == []


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("kind", ["empty", "missing", "directory", "unreadable"])
def test_unavailable_config(tmp_path, monkeypatch, launch, session, kind):
    path = tmp_path / "mcp.json"
    if kind == "directory":
        path.mkdir()
    if kind == "unreadable":
        def denied(*args, **kwargs):
            raise PermissionError(MARKER)
        monkeypatch.setattr(Path, "read_text", denied)
    result = launch("" if kind == "empty" else str(path), session)
    assert (result.ok, result.status, result.error, result.upstream_session_id) == (
        False, "error", ERROR, session,
    )
    assert MARKER not in repr(result) + repr(launch.events)


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("exception", [OSError, RuntimeError])
def test_ordinary_read_exception_is_sanitized(tmp_path, monkeypatch, launch, session, exception):
    def fail(*args, **kwargs):
        raise exception(MARKER)
    monkeypatch.setattr(Path, "read_text", fail)
    result = launch(str(tmp_path / "mcp.json"), session)
    assert result.error == ERROR and result.upstream_session_id == session
    assert MARKER not in repr(result) + repr(launch.events)


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_control_flow_propagates(tmp_path, monkeypatch, launch, exception):
    def fail(*args, **kwargs):
        raise exception()
    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(exception):
        launch(str(tmp_path / "mcp.json"))


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("during_read", [False, True])
def test_cancellation_does_not_start_cli(tmp_path, monkeypatch, launch, session, during_read):
    cancel = threading.Event()
    def read(*args, **kwargs):
        cancel.set()
        return '{"mcpServers":{"uefn":{"command":"node"}}}'
    if during_read:
        monkeypatch.setattr(Path, "read_text", read)
    else:
        cancel.set()
    result = launch(str(tmp_path / "mcp.json"), session, cancel=cancel)
    assert not result.ok and result.status == "cancelled"
    assert result.upstream_session_id == session


@pytest.mark.parametrize("session", ["", "previous-session"])
@pytest.mark.parametrize("server", [
    {"command": "node", "args": ["bridge.js"], "env": {"SYNTHETIC": "test"}},
    {"type": "stdio", "command": "node"},
    {"type": "http", "url": "https://example.invalid/mcp", "headers": {"X-Test": "test"}},
    {"type": "sse", "url": "http://example.invalid/sse"},
])
def test_valid_config_preserves_launch(tmp_path, monkeypatch, launch, session, server):
    from frontend.settings import PanelSettings
    from backend.agent.coding_agents.proc_exec import ProcResult

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"uefn": server}}), encoding="utf-8")
    monkeypatch.setattr(adapter, "resolve_claude_bin", lambda _: "claude-test")
    monkeypatch.setattr(PanelSettings, "load", lambda: object())
    monkeypatch.setattr(adapter, "coding_agent_cfg", lambda *_: {})
    monkeypatch.setattr(adapter, "claude_extra_dirs", lambda *_: [])
    monkeypatch.setattr(adapter, "_with_chat_captures", lambda *_: [])
    monkeypatch.setattr(adapter, "_core_has_permission_prompt", lambda: True)
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        kwargs["on_line"](json.dumps({"type": "result", "subtype": "success",
                                     "result": "mock answer", "session_id": "returned-session"}))
        return ProcResult(returncode=0)
    monkeypatch.setattr(adapter, "run_streaming_process", run)
    result = launch(str(path), session)
    assert result.ok and result.reply_text == "mock answer"
    assert result.upstream_session_id == "returned-session"
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[argv.index("--mcp-config") + 1] == str(path)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-prompt-tool") + 1] == adapter._PERMISSION_PROMPT_TOOL
    assert calls[0]["stdin_data"] == "test user task"
    assert ("--resume" in argv) == bool(session)
    if session:
        assert argv[argv.index("--resume") + 1] == session


def test_init_does_not_claim_tools_ready():
    events = []
    state = adapter._StreamState("test", "run", events.append)
    state.on_line(json.dumps({"type": "system", "subtype": "init", "model": "sonnet",
                             "mcp_servers": [{"name": "uefn", "status": "failed"}], "tools": []}))
    assert events and "ready" not in events[-1]["text"].lower()
