"""Ducky mode contract and no-replay boundaries; no live CLI/provider."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import dataclasses
import importlib
import importlib.util
import inspect
import itertools
import json
from pathlib import Path
import subprocess
import sys
import threading
import types
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT.parents[1] / "UEFN-Ducky-Release"
if str(APP / "ducky_app") not in sys.path:
    sys.path.insert(0, str(APP / "ducky_app"))
sys.modules.pop("backend", None)
pkg = types.ModuleType("_claude_modes_test")
pkg.__path__ = [str(ROOT / "backend")]
sys.modules[pkg.__name__] = pkg
a = importlib.import_module(pkg.__name__ + ".claude_code_adapter")
updater = importlib.import_module(pkg.__name__ + ".cli_update")
from backend.agent.coding_agents import base, cli_shared
from backend.agent.coding_agents.proc_exec import ProcResult


@pytest.fixture
def launch(tmp_path, monkeypatch):
    from frontend.settings import PanelSettings
    calls, events = [], []
    resolver = Mock(return_value="mock-claude")
    update = Mock(return_value={"ok": True})
    monkeypatch.setattr(a, "resolve_claude_bin", resolver)
    monkeypatch.setattr(a, "claude_extra_dirs", lambda *_: [])
    monkeypatch.setattr(a, "_with_chat_captures", lambda *_: [])
    monkeypatch.setattr(a, "_dbg_tool_pairing", lambda *_: None)
    monkeypatch.setattr(a, "coding_agent_cfg", lambda *_: {"permission_mode": "acceptEdits"})
    monkeypatch.setattr(a, "_core_has_permission_prompt", lambda: True)
    monkeypatch.setattr(PanelSettings, "load", lambda: object())
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers":{"uefn":{"command":"mock-only"}}}', encoding="utf-8")
    def process(**kw):
        calls.append(kw)
        emit_success(kw)
        return ProcResult(returncode=0)
    monkeypatch.setattr(a, "run_streaming_process", process)
    monkeypatch.setattr(updater, "update_claude_cli", update)
    kwargs = dict(prompt="user payload", system_prompt="", cwd=str(tmp_path), conv_id="test",
                  model="sonnet", mcp_config_path=str(config), extra_args="", cli_path="",
                  env={"MOCK_ENV": "kept"}, push=events.append)
    def run(session="", **overrides):
        return a.ClaudeCodeAdapter().launch(**dict(kwargs, session_id=session, **overrides))
    return types.SimpleNamespace(run=run, kwargs=kwargs, calls=calls, events=events,
                                 update=update, resolver=resolver, process=process)


def emit_init(kw):
    kw["on_line"](json.dumps({"type": "system", "subtype": "init", "mcp_servers": [{"name": "uefn", "status": "connected"}]}))


def emit_success(kw):
    emit_init(kw)
    kw["on_line"](json.dumps({"type": "result", "subtype": "success", "result": "answer",
                             "session_id": "saved-session", "usage": {"output_tokens": 3}}))


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("mode", ["agent", " Agent ", None, ""])
def test_agent_mode_success_and_stream_metadata(launch, session, mode):
    result = launch.run(session, mode=mode)
    assert result.ok and result.upstream_session_id == "saved-session"
    assert result.requested_mode == result.effective_mode == "agent"
    assert launch.events
    assert all(e["requested_mode"] == "agent" and e["effective_mode"] == "" for e in launch.events)
    argv = launch.calls[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert ("--resume" in argv) == bool(session)
    assert a.ClaudeCodeAdapter.capabilities.supported_modes == ("agent", "ask", "plan")


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("mode", ["invalid", 42])
def test_restricted_invalid_refusal_precedes_all_setup(launch, monkeypatch, session, mode):
    def forbidden(*args, **kwargs):
        raise AssertionError("mode refusal must precede setup")
    monkeypatch.setattr(a, "_valid_ducky_mcp_config", forbidden)
    monkeypatch.setattr(a, "resolve_claude_bin", forbidden)
    monkeypatch.setattr(a, "coding_agent_cfg", forbidden)
    monkeypatch.setattr("backend.agent.coding_agents.mcp_inject.write_prompt_file", forbidden)
    result = launch.run(session, mode=mode)
    assert not result.ok and result.status == "error"
    assert result.upstream_session_id == session and not result.effective_mode
    assert result.requested_mode == (mode.strip().lower() if isinstance(mode, str) and mode.strip().lower() in ("ask", "plan") else "")
    assert not launch.calls and not launch.events
    launch.update.assert_not_called()


def test_same_session_mode_switches_apply_current_mode(launch):
    for mode in ("agent", "ask", "plan", "agent", "plan", "ask"):
        before = len(launch.calls)
        result = launch.run("prior-session", mode=mode)
        assert result.ok
        assert len(launch.calls) - before == 1
        assert result.requested_mode == mode
        assert result.effective_mode == mode


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("terminal", ["cancelled", "timed_out"])
@pytest.mark.parametrize("marker", ["unknown option --append-system-prompt-file", "unknown option --include-partial-messages", "does not support this model; claude update"])
@pytest.mark.parametrize("partial", [False, True])
def test_terminal_process_never_classifies_or_replays(launch, monkeypatch, session, terminal, marker, partial):
    def process(**kw):
        launch.calls.append(kw)
        if partial:
            emit_success(kw)
        return ProcResult(returncode=1, stderr_tail=marker, raw_tail=marker, **{terminal: True})
    monkeypatch.setattr(a, "run_streaming_process", process)
    classify = Mock(side_effect=AssertionError("terminal classifier"))
    monkeypatch.setattr(updater, "is_cli_too_old_error", classify)
    result = launch.run(session, system_prompt="system payload")
    assert len(launch.calls) == 1
    launch.update.assert_not_called()
    classify.assert_not_called()
    assert result.status == ("cancelled" if terminal == "cancelled" else "timeout")
    assert result.upstream_session_id == ("saved-session" if partial else session)
    assert result.reply_text == ("answer" if partial else "")
    if partial:
        assert result.usage["output_tokens"] == 3
    assert result.requested_mode == "agent" and result.effective_mode == ""


@pytest.mark.parametrize("status", ["cancelled", "timeout"])
def test_finalizer_terminal_precedes_every_fallback(launch, monkeypatch, status):
    monkeypatch.setattr(a, "run_streaming_process", lambda **kw: launch.calls.append(kw) or ProcResult(returncode=1, stderr_tail="unknown option --include-partial-messages; does not support this model"))
    real = a.finalize_cli_turn
    def finalizer(**kw):
        result = real(**kw)
        result.status = status
        return result
    monkeypatch.setattr(a, "finalize_cli_turn", finalizer)
    classifier = Mock(side_effect=AssertionError("terminal classifier"))
    monkeypatch.setattr(updater, "is_cli_too_old_error", classifier)
    result = launch.run()
    assert result.status == status and len(launch.calls) == 1
    classifier.assert_not_called()
    launch.update.assert_not_called()


@pytest.mark.parametrize("session", ["", "prior-session"])
def test_safe_fallback_chain_preserves_effective_payload(launch, monkeypatch, session):
    markers = iter(["unknown option '--append-system-prompt-file'", "unknown option '--include-partial-messages'", "does not support this model; claude update", ""])
    def process(**kw):
        launch.calls.append(kw)
        marker = next(markers)
        if not marker:
            emit_success(kw)
        return ProcResult(returncode=int(bool(marker)), stderr_tail=marker)
    monkeypatch.setattr(a, "run_streaming_process", process)
    result = launch.run(session, system_prompt="SYSTEM_PAYLOAD")
    assert len(launch.calls) == 4 and launch.update.call_count == 1
    assert all("SYSTEM_PAYLOAD" in c["stdin_data"] and "user payload" in c["stdin_data"] for c in launch.calls[1:])
    assert launch.calls[1]["stdin_data"] == launch.calls[2]["stdin_data"] == launch.calls[3]["stdin_data"]
    for c in launch.calls:
        argv = c["argv"]
        assert argv[argv.index("--mcp-config") + 1] == launch.kwargs["mcp_config_path"]
        assert "--strict-mcp-config" in argv and "--permission-prompt-tool" in argv
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert c["env_extra"] == {"MOCK_ENV": "kept", "MCP_TIMEOUT": "60000"}
        assert ("--resume" in argv) == bool(session)
    assert result.ok


@pytest.mark.parametrize("marker", ["unknown option '--required-policy'", "unknown option '--include-partial-messages-extra'"])
def test_unknown_required_flag_never_strips_optional_flag(launch, monkeypatch, marker):
    monkeypatch.setattr(a, "run_streaming_process", lambda **kw: launch.calls.append(kw) or ProcResult(returncode=1, stderr_tail=marker))
    result = launch.run()
    assert not result.ok and len(launch.calls) == 1
    launch.update.assert_not_called()


@pytest.mark.parametrize("stage", ["process", "classifier", "status", "updater", "resolver"])
def test_observed_cancel_stops_repair_without_relabeling_error(launch, monkeypatch, stage):
    cancel = threading.Event()
    def process(**kw):
        launch.calls.append(kw)
        if stage == "process":
            cancel.set()
        return ProcResult(returncode=1, stderr_tail="does not support this model; claude update")
    monkeypatch.setattr(a, "run_streaming_process", process)
    def classify(*args):
        if stage == "classifier":
            cancel.set()
        return True
    monkeypatch.setattr(updater, "is_cli_too_old_error", Mock(side_effect=classify))
    def push(e):
        launch.events.append(e)
        if stage == "status" and "updating" in e.get("text", ""):
            cancel.set()
    def update(*args):
        if stage == "updater":
            cancel.set()
        return {"ok": True}
    launch.update.side_effect = update
    def resolve(*args):
        if stage == "resolver" and launch.resolver.call_count > 1:
            cancel.set()
        return "mock-claude"
    launch.resolver.side_effect = resolve
    result = launch.run(cancel=cancel, push=push)
    assert result.status == "error" and not result.ok
    assert len(launch.calls) == 1
    assert launch.update.call_count == int(stage in ("updater", "resolver"))


@pytest.mark.parametrize("partial", ["reply", "tool"])
def test_uncertain_work_never_enters_update(launch, monkeypatch, partial):
    def process(**kw):
        launch.calls.append(kw)
        if partial == "reply":
            emit_success(kw)
        else:
            kw["on_line"](json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}]}}))
        return ProcResult(returncode=1, stderr_tail="does not support this model; claude update")
    monkeypatch.setattr(a, "run_streaming_process", process)
    launch.run()
    assert len(launch.calls) == 1
    launch.update.assert_not_called()


@pytest.mark.parametrize("failure", ["config", "model", "pre_cancel"])
def test_early_error_metadata_and_session(launch, failure):
    overrides = {"mode": "agent"}
    if failure == "config":
        overrides["mcp_config_path"] = "missing-config"
    if failure == "model":
        overrides["model"] = "default"
    if failure == "pre_cancel":
        event = threading.Event(); event.set(); overrides["cancel"] = event
    result = launch.run("prior-session", **overrides)
    assert not result.ok and result.requested_mode == "agent" and result.effective_mode == ""
    assert result.upstream_session_id == "prior-session" and not launch.calls


@pytest.fixture(scope="module")
def historical_classes():
    # Actual published 1.2.361 definitions; no installed-old-app claim.
    source = subprocess.check_output(["git", "show", "f2094f09307472a19fdd1edf762065649aa734dc:ducky_app/backend/agent/coding_agents/base.py"], cwd=APP, text=True, encoding="utf-8")
    wanted = {"CodingAgentCapabilities", "CodingAgentInfo", "CodingAgentLaunchResult"}
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name in wanted]
    namespace = dict(dataclass=dataclasses.dataclass, field=dataclasses.field, Any=object)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<published-f209-base>", "exec", dont_inherit=True), namespace)
    assert "supported_modes" not in inspect.signature(namespace["CodingAgentCapabilities"]).parameters
    assert "effective_mode" not in inspect.signature(namespace["CodingAgentLaunchResult"]).parameters
    return namespace


@contextmanager
def registered_profile(launch, monkeypatch, historical_classes, helper, caps, result_shape):
    from backend.uefn_plugins import host
    # Other plugin suites deliberately reload the host namespace at collection.
    # Patch the modules the real loader will import, not a stale collected alias.
    host_base = importlib.import_module("backend.agent.coding_agents.base")
    host_shared = importlib.import_module("backend.agent.coding_agents.cli_shared")
    before, path_before = dict(sys.modules), list(sys.path)
    try:
        with monkeypatch.context() as patch:
            if not helper:
                patch.delattr(host_base, "normalize_coding_mode")
            if not caps:
                patch.setattr(host_base, "CodingAgentCapabilities", historical_classes["CodingAgentCapabilities"])
            old = historical_classes["CodingAgentLaunchResult"]
            result_type = host_base.CodingAgentLaunchResult
            if result_shape != "current":
                result_type = old if result_shape == "old" else dataclasses.make_dataclass("PartialModeResult", [(result_shape, str, dataclasses.field(default=""))], bases=(old,))
                patch.setattr(host_base, "CodingAgentLaunchResult", result_type)
                patch.setattr(host_shared, "CodingAgentLaunchResult", result_type)
            if hasattr(host_base, "CodingAgentInfo"):
                patch.setattr(host_base, "CodingAgentInfo", historical_classes["CodingAgentInfo"])
            package = host._import_backend("anthropic_mode_compat", ROOT, "backend")
            module = importlib.import_module(package.__name__ + ".claude_code_adapter")
            cli = importlib.import_module(package.__name__ + ".cli_update")
            patch.setattr(cli, "schedule_cli_update_on_plugin_load", Mock())
            for name in ("resolve_claude_bin", "run_streaming_process", "claude_extra_dirs", "_with_chat_captures", "_dbg_tool_pairing", "coding_agent_cfg", "_core_has_permission_prompt"):
                patch.setattr(module, name, getattr(a, name))
            patch.setattr(cli, "update_claude_cli", launch.update)
            api = Mock()
            package.register(api)
            assert api.register_coding_agent.call_args.args == ("claude_code",)
            agent = api.register_coding_agent.call_args.kwargs["factory"]()
            yield module, agent, result_type
    finally:
        for name in set(sys.modules) - set(before):
            sys.modules.pop(name, None)
        sys.modules.update(before)
        sys.path[:] = path_before


PROFILES = list(itertools.product([False, True], [False, True], ["old", "current"])) + [(True, True, "requested_mode"), (True, True, "effective_mode")]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_historical_actual_host_registration_and_agent(launch, monkeypatch, historical_classes, profile, session):
    with registered_profile(launch, monkeypatch, historical_classes, *profile) as (module, agent, result_type):
        result = agent.launch(**launch.kwargs, session_id=session)
        assert type(result) is result_type and result.ok
        assert result.to_dict()["reply_text"] == "answer"
        assert result.upstream_session_id == "saved-session"
        parameters = inspect.signature(result_type).parameters
        for name in ("requested_mode", "effective_mode"):
            if name in parameters:
                assert getattr(result, name) == "agent"
        assert getattr(agent.capabilities, "supported_modes", ("agent", "ask", "plan")) == ("agent", "ask", "plan")
        before = len(launch.calls)
        for mode in ("ask", "plan"):
            failure = agent.launch(**launch.kwargs, session_id=session, mode=mode)
            assert type(failure) is result_type and failure.ok
        assert len(launch.calls) == before + 2


def test_mandatory_import_error_is_not_hidden(launch, monkeypatch, historical_classes):
    monkeypatch.delattr(importlib.import_module("backend.agent.coding_agents.base"), "CodingAgentInfo")
    with pytest.raises(ImportError, match="CodingAgentInfo"):
        with registered_profile(launch, monkeypatch, historical_classes, False, True, "current"):
            pytest.fail("mandatory import was hidden")


@pytest.mark.parametrize("terminal", ["cancelled", "timed_out"])
@pytest.mark.parametrize("attempt", [2, 3, 4])
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_terminal_on_each_later_attempt_retains_partial_blocks(launch, monkeypatch, terminal, attempt, session):
    markers = ["unknown option --append-system-prompt-file", "unknown option --include-partial-messages", "does not support this model; claude update"]
    def process(**kw):
        launch.calls.append(kw)
        index = len(launch.calls)
        if index < attempt:
            return ProcResult(returncode=1, stderr_tail=markers[index - 1])
        emit_init(kw)
        kw["on_line"](json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "partial text"},
            {"type": "tool_use", "id": "read1", "name": "Read", "input": {"file_path": "mock"}},
        ]}}))
        emit_success(kw)
        return ProcResult(returncode=1, stderr_tail="does not support this model; claude update", **{terminal: True})
    monkeypatch.setattr(a, "run_streaming_process", process)
    result = launch.run(session, system_prompt="SYSTEM_PAYLOAD")
    assert len(launch.calls) == attempt
    assert launch.update.call_count == int(attempt == 4)
    assert result.status == ("cancelled" if terminal == "cancelled" else "timeout")
    assert result.reply_text == "answer" and result.upstream_session_id == "saved-session"
    assert result.blocks and "partial text" in json.dumps(result.blocks)
    assert "read1" in json.dumps(result.blocks) and result.usage["output_tokens"] == 3
    assert result.requested_mode == "agent" and result.effective_mode == ""


@pytest.mark.parametrize("failure", ["config", "model", "cancelled", "timeout"])
def test_historical_error_type_serialization_and_session(launch, monkeypatch, historical_classes, failure):
    with registered_profile(launch, monkeypatch, historical_classes, False, False, "old") as (module, agent, result_type):
        kwargs = dict(launch.kwargs, session_id="prior-session")
        if failure == "config":
            kwargs["mcp_config_path"] = "missing-config"
        elif failure == "model":
            kwargs["model"] = "default"
        else:
            def process(**kw):
                launch.calls.append(kw)
                return ProcResult(returncode=1, stderr_tail="unknown option --include-partial-messages; claude update", **{"cancelled" if failure == "cancelled" else "timed_out": True})
            monkeypatch.setattr(module, "run_streaming_process", process)
        result = agent.launch(**kwargs)
        assert type(result) is result_type and not result.ok
        assert result.upstream_session_id == "prior-session"
        assert "requested_mode" not in result.to_dict() and "effective_mode" not in result.to_dict()
        assert result.status == (failure if failure in ("cancelled", "timeout") else "error")
        launch.update.assert_not_called()


def test_unrelated_result_constructor_error_remains_visible(launch, monkeypatch):
    def broken(**kwargs):
        raise TypeError("unrelated constructor defect")
    monkeypatch.setattr(a, "CodingAgentLaunchResult", broken)
    with pytest.raises(TypeError, match="unrelated constructor defect"):
        launch.run(mode="invalid")


@pytest.mark.parametrize("marker", ["unknown option --append-system-prompt-file", "unknown option --include-partial-messages"])
def test_cancel_at_parser_boundary_never_replays(launch, monkeypatch, marker):
    cancel = threading.Event()
    def process(**kw):
        launch.calls.append(kw)
        cancel.set()
        return ProcResult(returncode=1, stderr_tail=marker)
    monkeypatch.setattr(a, "run_streaming_process", process)
    result = launch.run(cancel=cancel, system_prompt="SYSTEM_PAYLOAD")
    assert result.status == "error" and len(launch.calls) == 1
    launch.update.assert_not_called()


def test_cancel_from_start_status_prevents_process(launch):
    cancel = threading.Event()
    result = launch.run(cancel=cancel, push=lambda event: cancel.set())
    assert result.status == "cancelled" and not launch.calls
    assert result.requested_mode == "agent" and result.effective_mode == ""
    launch.update.assert_not_called()


def test_safe_error_only_json_repair_is_bounded(launch, monkeypatch):
    def process(**kw):
        launch.calls.append(kw)
        kw["on_line"](json.dumps({"type": "system", "subtype": "init", "session_id": "prior-session", "mcp_servers": [{"name": "uefn", "status": "connected"}]}))
        kw["on_line"](json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                 "result": "does not support this model; claude update"}))
        return ProcResult(returncode=1)
    monkeypatch.setattr(a, "run_streaming_process", process)
    result = launch.run("prior-session")
    assert not result.ok and len(launch.calls) == 2 and launch.update.call_count == 1
    assert result.requested_mode == "agent" and result.effective_mode == ""


@pytest.mark.parametrize("evidence", ["tokens", "cost", "system", "opaque"])
def test_uncertain_result_evidence_prevents_classifier(launch, monkeypatch, evidence):
    def process(**kw):
        launch.calls.append(kw)
        if evidence == "system":
            kw["on_line"](json.dumps({"type": "system", "subtype": "unknown_task_progress"}))
        if evidence == "opaque":
            kw["on_line"]("unstructured task output")
        kw["on_line"](json.dumps({"type": "result", "is_error": True,
            "result": "does not support this model; claude update",
            "usage": {"input_tokens": int(evidence == "tokens")},
            "total_cost_usd": 0.01 if evidence == "cost" else 0}))
        return ProcResult(returncode=1)
    monkeypatch.setattr(a, "run_streaming_process", process)
    classify = Mock(side_effect=AssertionError("uncertain work classifier"))
    monkeypatch.setattr(updater, "is_cli_too_old_error", classify)
    result = launch.run()
    assert not result.ok and len(launch.calls) == 1
    classify.assert_not_called()
    launch.update.assert_not_called()


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("content", ["assistant", "result", "tool"])
@pytest.mark.parametrize("terminal", ["", "cancelled", "timed_out"])
def test_nonzero_partial_is_failure_without_losing_content(launch, monkeypatch, session, content, terminal):
    def process(**kw):
        emit_init(kw)
        launch.calls.append(kw)
        if content == "result":
            kw["on_line"](json.dumps({"type": "result", "result": "legitimate partial",
                "session_id": "kept", "usage": {"output_tokens": 2}}))
        else:
            parts = [{"type": "text", "text": "legitimate partial"}]
            if content == "tool":
                parts.append({"type": "tool_use", "id": "partial-tool", "name": "Read", "input": {}})
            kw["on_line"](json.dumps({"type": "assistant", "session_id": "kept", "message": {"content": parts}}))
        return ProcResult(returncode=1, stderr_tail="PRIVATE_SYNTHETIC does not support this model; claude update",
                          raw_tail="PRIVATE_SYNTHETIC", **({terminal: True} if terminal else {}))
    monkeypatch.setattr(a, "run_streaming_process", process)
    classifier = Mock(side_effect=AssertionError("partial work classifier"))
    monkeypatch.setattr(updater, "is_cli_too_old_error", classifier)
    result = launch.run(session)
    assert not result.ok and result.status == ({"cancelled": "cancelled", "timed_out": "timeout"}.get(terminal, "error"))
    assert result.upstream_session_id == "kept"
    assert "legitimate partial" in result.reply_text + json.dumps(result.blocks)
    if content == "tool":
        assert "partial-tool" in json.dumps(result.blocks)
    if content == "result":
        assert result.usage["output_tokens"] == 2
    assert result.requested_mode == "agent" and result.effective_mode == ""
    assert "PRIVATE_SYNTHETIC" not in json.dumps(result.to_dict()) + json.dumps(launch.events)
    if not terminal:
        assert result.error == "Claude Code exited unsuccessfully. Partial output was retained; review it before retrying."
        assert result.output_tail == ""
    assert len(launch.calls) == 1
    classifier.assert_not_called()
    launch.update.assert_not_called()


@pytest.mark.parametrize("mode", [{}, False, " PRIVATE_SYNTHETIC "])
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_invalid_mode_never_serializes_raw_value(launch, monkeypatch, mode, session):
    for name in ("_valid_ducky_mcp_config", "resolve_claude_bin", "coding_agent_cfg", "run_streaming_process"):
        monkeypatch.setattr(a, name, Mock(side_effect=AssertionError("invalid mode setup")))
    result = launch.run(session, mode=mode)
    assert not result.ok and result.status == "error"
    assert result.requested_mode == result.effective_mode == ""
    assert result.error == "Invalid Claude Code Ducky mode." and result.upstream_session_id == session
    assert "PRIVATE_SYNTHETIC" not in json.dumps(result.to_dict()) + json.dumps(launch.events)
    assert not launch.events and not launch.calls
    launch.update.assert_not_called()


MODEL_WORK = [
    {"inputTokens": 10}, {"outputTokens": 3}, {"costUSD": 0.1},
    {"input_tokens": 10}, {"output_tokens": 3}, {"cost_usd": 0.1},
    {"cacheReadInputTokens": 2}, {"cacheCreationInputTokens": 2},
    {"cache_read_input_tokens": 2}, {"cache_creation_input_tokens": 2},
    {"numTurns": 1}, {"num_turns": 1}, {"webSearchRequests": 1},
    {"usage": {"input_tokens": 10}}, {"future_work_metric": 3},
    {"inputTokens": "10"}, ["uncertain activity"], "uncertain activity",
]


@pytest.mark.parametrize("container", ["modelUsage", "model_usage"])
@pytest.mark.parametrize("entry", MODEL_WORK)
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_per_model_work_stops_before_classifier(launch, monkeypatch, container, entry, session):
    def process(**kw):
        launch.calls.append(kw)
        kw["on_line"](json.dumps({"type": "result", "is_error": True,
            "session_id": "kept", "result": "does not support this model; claude update",
            container: {"sonnet": entry}}))
        return ProcResult(returncode=1)
    monkeypatch.setattr(a, "run_streaming_process", process)
    classifier = Mock(side_effect=AssertionError("per-model work classifier"))
    monkeypatch.setattr(updater, "is_cli_too_old_error", classifier)
    result = launch.run(session)
    assert not result.ok and result.effective_mode == "" and result.upstream_session_id == "kept"
    assert result.usage["input_tokens"] == result.usage["output_tokens"] == 0  # No invented totals.
    assert len(launch.calls) == 1
    classifier.assert_not_called()
    launch.update.assert_not_called()


@pytest.mark.parametrize("first", [
    {"usage": {"input_tokens": 10}},
    {"modelUsage": {"sonnet": {"inputTokens": 10}}, "model_usage": {}},
    {"modelUsage": {}, "model_usage": {"sonnet": {"input_tokens": 10}}},
    {"modelUsage": [{"inputTokens": 10}]},
])
def test_work_evidence_survives_later_empty_result(launch, monkeypatch, first):
    def process(**kw):
        launch.calls.append(kw)
        for evidence in (first, {}):
            kw["on_line"](json.dumps({"type": "result", "is_error": True,
                "result": "does not support this model; claude update", **evidence}))
        return ProcResult(returncode=1)
    monkeypatch.setattr(a, "run_streaming_process", process)
    classifier = Mock(side_effect=AssertionError("lost prior work evidence"))
    monkeypatch.setattr(updater, "is_cli_too_old_error", classifier)
    assert not launch.run().ok
    assert len(launch.calls) == 1
    classifier.assert_not_called()
    launch.update.assert_not_called()


@pytest.mark.parametrize("container", ["modelUsage", "model_usage"])
def test_zero_model_usage_allows_bounded_prework_repair(launch, monkeypatch, container):
    def process(**kw):
        launch.calls.append(kw)
        if len(launch.calls) == 1:
            kw["on_line"](json.dumps({"type": "result", "is_error": True,
                "result": "does not support this model; claude update",
                container: {"sonnet": {"inputTokens": 0, "outputTokens": 0, "costUSD": 0,
                                       "contextWindow": 200000, "maxOutputTokens": 64000}}}))
            return ProcResult(returncode=1)
        emit_success(kw)
        return ProcResult(returncode=0)
    monkeypatch.setattr(a, "run_streaming_process", process)
    result = launch.run()
    assert result.ok and result.effective_mode == "agent"
    assert len(launch.calls) == 2 and launch.update.call_count == 1


CAPACITY_KEYS = ["contextWindow", "context_window", "maxOutputTokens", "max_output_tokens"]


@pytest.mark.parametrize("container", ["modelUsage", "model_usage"])
@pytest.mark.parametrize("key", CAPACITY_KEYS)
@pytest.mark.parametrize("value", [{"inputTokens": 7}, ["unknown activity"], "200000", True,
                                    -1, 1.5, float("inf"), float("nan"), None, {}])
@pytest.mark.parametrize("session,suppress", [("", False), ("prior-session", True)])
def test_uncertain_capacity_is_sticky_before_callback_parsing(launch, monkeypatch, container, key, value, session, suppress):
    swallowed = []
    def process(**kw):
        launch.calls.append(kw)
        for evidence in ({container: {"sonnet": {key: value}}}, {}):
            line = json.dumps({"type": "result", "is_error": True, "session_id": "kept",
                               "result": "does not support this model; claude update", **evidence})
            if suppress:
                # Actual proc_exec callback policy: ordinary Exception is swallowed.
                try:
                    kw["on_line"](line)
                except Exception as exc:
                    swallowed.append(exc)
            else:
                kw["on_line"](line)
        return ProcResult(returncode=1)
    monkeypatch.setattr(a, "run_streaming_process", process)
    classifier = Mock(side_effect=AssertionError("uncertain work reached repair classifier"))
    monkeypatch.setattr(updater, "is_cli_too_old_error", classifier)
    result = launch.run(session)
    assert not swallowed
    assert not result.ok and result.requested_mode == "agent" and result.effective_mode == ""
    assert result.upstream_session_id == "kept" and "does not support" in result.reply_text
    assert result.usage["input_tokens"] == result.usage["output_tokens"] == 0
    assert len(launch.calls) == 1
    classifier.assert_not_called()
    launch.update.assert_not_called()


@pytest.mark.parametrize("key", CAPACITY_KEYS)
@pytest.mark.parametrize("value", [0, 200000, 200000.0])
def test_valid_scalar_capacity_has_no_work(key, value):
    data = {"modelUsage": {"sonnet": {key: value}}, "model_usage": {"other": {"inputTokens": 0}}}
    assert not a._StreamState._result_has_work(data)
    assert a._StreamState._context_limit_from_result(data) == (int(value) or None if key.startswith("context") else None)


@pytest.mark.parametrize("reverse", [False, True])
def test_combined_capacity_containers_keep_uncertainty_and_valid_limit(reverse):
    containers = ["modelUsage", "model_usage"]
    if reverse:
        containers.reverse()
    data = {containers[0]: {"a": {"contextWindow": {"future_usage": 1}}},
            containers[1]: {"b": {"context_window": 200000}}}
    assert a._StreamState._result_has_work(data)
    assert a._StreamState._context_limit_from_result(data) == 200000


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_capacity_evidence_does_not_swallow_control_flow(exception):
    class Interrupted(dict):
        def items(self):
            raise exception()
    with pytest.raises(exception):
        a._StreamState._result_has_work({"modelUsage": {"sonnet": Interrupted()}})


def test_invalid_mode_is_never_stringified(launch):
    class PrivateMode:
        def __str__(self):
            raise AssertionError("private mode stringified")
    result = launch.run(mode=PrivateMode())
    assert not result.ok and result.requested_mode == result.effective_mode == ""
    assert not launch.calls and not launch.events
    launch.resolver.assert_not_called()
    launch.update.assert_not_called()


@pytest.mark.parametrize("mode", ["ask", "plan"])
@pytest.mark.parametrize("session", ["", "prior-session"])
def test_read_only_modes_override_saved_permissions_and_extra_args(launch, monkeypatch, mode, session):
    monkeypatch.setattr(a, "coding_agent_cfg", lambda *_: {"permission_mode": "bypassPermissions"})
    result = launch.run(session, mode=mode,
        extra_args="--permission-mode bypassPermissions --dangerously-skip-permissions --allowedTools Write")
    assert result.ok and result.reply_text == "answer"
    assert result.requested_mode == result.effective_mode == mode
    argv = launch.calls[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv.count("--permission-mode") == 1
    assert "--dangerously-skip-permissions" not in argv
    assert set(argv[argv.index("--disallowedTools") + 1].split(",")) >= {
        "Edit", "Write", "NotebookEdit", "Bash", "ExitPlanMode"}
    assert "Do not" in launch.calls[0]["stdin_data"]
    if mode == "plan":
        assert "ducky_create_plan" in launch.calls[0]["stdin_data"]
        assert "ducky_plan_update_node" in launch.calls[0]["stdin_data"]
    assert ("--resume" in argv) == bool(session)


@pytest.mark.parametrize("mode", ["ask", "plan"])
def test_read_only_turn_contract_has_zero_project_changes(launch, monkeypatch, tmp_path, mode):
    target = tmp_path / "project.txt"
    target.write_text("original", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    plans = []
    def cli_contract(**kw):
        emit_init(kw)
        argv = kw["argv"]
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        assert "Write" in denied and argv[argv.index("--permission-mode") + 1] == "plan"
        kw["on_line"](json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "attempt", "name": "Write", "input": {"file_path": str(target)}}]}}))
        kw["on_line"](json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "attempt", "is_error": True,
             "content": "Write refused: tool is disallowed in plan permission mode"}]}}))
        if mode == "plan":
            assert "ducky_create_plan" in kw["stdin_data"]
            plans.append({"nodes": [{"content": "Inspect then implement"}]})
        emit_success(kw)
        return ProcResult(returncode=0)
    monkeypatch.setattr(a, "run_streaming_process", cli_contract)
    result = launch.run(mode=mode)
    assert result.ok
    assert bool(plans) == (mode == "plan")
    assert any(e.get("type") == "tool_done" and not e["success"]
               and "refused" in e["tool"]["result"] for e in launch.events)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("servers", [None, [], [{"name": "other", "status": "connected"}],
                                      [{"name": "uefn", "status": "failed"}], "malformed", "no-init"])
def test_missing_ducky_connection_cannot_answer(launch, monkeypatch, session, servers):
    def process(**kw):
        launch.calls.append(kw)
        if servers != "no-init":
            kw["on_line"](json.dumps({"type": "system", "subtype": "init", "mcp_servers": servers}))
        kw["on_line"](json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                     "delta": {"type": "text_delta", "text": "unavailable answer"}}}))
        kw["on_line"](json.dumps({"type": "result", "result": "unavailable answer"}))
        return ProcResult(returncode=0)
    monkeypatch.setattr(a, "run_streaming_process", process)
    result = launch.run(session)
    assert not result.ok and result.error == "Ducky's tools didn't connect"
    assert not result.reply_text and not result.blocks and not result.streamed
    assert not any(e.get("type") == "text_delta" for e in launch.events)
    assert len(launch.calls) == 1
    launch.update.assert_not_called()


@pytest.mark.parametrize("session", ["", "prior-session"])
def test_cold_server_then_first_claude_tool(launch, monkeypatch, session):
    from backend.agent.coding_agents import readiness
    from backend.bridge import shared_mcp
    now = [0.0]
    monkeypatch.setattr(readiness.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(readiness.time, "sleep", lambda n: now.__setitem__(0, now[0] + n))
    monkeypatch.setattr(shared_mcp, "start_daemon_from_app", lambda: {"ok": True})
    monkeypatch.setattr(shared_mcp, "_daemon_answers", lambda **kw: now[0] >= 8)
    def process(**kw):
        assert now[0] == 8
        assert kw["env_extra"]["MCP_TIMEOUT"] == "60000"
        emit_init(kw)
        kw["on_line"](json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "first", "name": "mcp__uefn__ducky_get_plan", "input": {}}]}}))
        kw["on_line"](json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "first", "content": "plan"}]}}))
        emit_success(kw)
        return ProcResult(returncode=0)
    monkeypatch.setattr(a, "run_streaming_process", process)
    result = readiness.launch_with_ready_tools(a.ClaudeCodeAdapter(), **dict(launch.kwargs, session_id=session))
    assert result.ok and "mcp__uefn__ducky_get_plan" in json.dumps(result.blocks)
    assert any(e.get("type") == "tool_done" and e.get("success") for e in launch.events)
