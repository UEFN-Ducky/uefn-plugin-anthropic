"""Public-interface fakes: these prove runner ordering, not vendor/live readiness."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

spec = importlib.util.spec_from_file_location(
    "_duplex_runner_test", Path(__file__).with_name("claude_duplex_runner.py"),
)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
MARKER = "SYNTHETIC_PRIVATE_STATUS_CONFIG"
NAMES = ["ducky_get_tools", "ducky_call_tool", "ducky_permission_prompt"]


class Result:
    is_error = False
    session_id = "new-session"


def status(state="connected", names=None, server="uefn"):
    return {"mcpServers": [{"name": server, "status": state,
            "tools": [{"name": name} for name in (NAMES if names is None else names)],
            "config": {"secret": MARKER}, "error": MARKER}]}


class Client:
    def __init__(self):
        self.calls = []
        self.responses = [status()]
        self.delay = {}
        self.errors = {}
        self.cancel_at = None
        self.cancel = threading.Event()
        self.options = None
        self.active = set()

    def factory(self, *, options):
        self.options = options
        return self

    async def step(self, name):
        self.calls.append(name)
        self.active.add(name)
        try:
            await anyio.sleep(self.delay.get(name, 0))
            if name in self.errors:
                raise self.errors[name]
            if name == self.cancel_at:
                self.cancel.set()
        finally:
            self.active.remove(name)

    async def connect(self, prompt=None):
        assert prompt is None, "initialization must not contain a user frame"
        self.sent = anyio.Event()
        await self.step("connect")

    async def get_mcp_status(self):
        await self.step("status")
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    async def query(self, prompt, session_id="default"):
        self.prompt = prompt
        assert session_id == "default"
        await self.step("query")
        self.sent.set()

    async def receive_response(self):
        await self.sent.wait()
        await self.step("receive")
        yield Result()

    async def disconnect(self):
        await self.step("disconnect")


def invoke(client, session="", **kwargs):
    options = kwargs.pop("options", SimpleNamespace(resume=session or None))
    return runner.run_turn(
        options=options, prompt="original user task", upstream_session_id=session,
        cancel=client.cancel, binding=runner.SDKBinding(runner.SDK_VERSION, client.factory, Result),
        startup_timeout=kwargs.pop("startup_timeout", .15),
        turn_timeout=.1, cleanup_timeout=.05, poll_interval=.001, **kwargs,
    )


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("prefixed", [False, True])
def test_same_client_gate_before_single_original_prompt(session, prefixed):
    client = Client()
    if prefixed:
        client.responses = [status(names=["mcp__uefn__" + n for n in NAMES])]
    options = SimpleNamespace(resume=session or None, model="model", system_prompt="system",
                              add_dirs=["attachments"], settings_sources=["project"],
                              allowed_tools=["Read"], disallowed_tools=["Write"], hooks=object(),
                              permission_mode="default", can_use_tool=object())
    result = invoke(client, session, options=options)
    assert result.status == "success" and result.submitted
    assert result.upstream_session_id == "new-session" and result.cleanup_complete
    assert client.calls == ["connect", "status", "query", "receive", "disconnect"]
    assert client.prompt == "original user task" and client.options is options
    assert not client.active


BAD_STATUS = [
    None, [], {}, {"mcpServers": {}}, {"mcpServers": [None]},
    status("failed"), status("needs-auth"), status("disabled"), status("CONNECTED"),
    status(names=[]), status(names=NAMES[:1]), status(server="other"),
    status(names=["mcp__other__" + n for n in NAMES]),
    status(names=["prefix" + n for n in NAMES]),
    {"mcpServers": [{"name": "uefn", "status": "connected"}]},
    {"mcpServers": [{"name": "uefn", "status": "connected", "tools": NAMES}]},
    {"mcpServers": status()["mcpServers"] * 2},
]


@pytest.mark.parametrize("session", ["", "prior-session"])
@pytest.mark.parametrize("response", BAD_STATUS)
def test_invalid_connection_or_catalog_never_sends(session, response):
    client = Client()
    client.responses = [response]
    result = invoke(client, session)
    assert result.status == "error" and not result.submitted
    assert result.upstream_session_id == session
    assert result.error == runner.READINESS_ERROR and MARKER not in repr(result)
    assert client.calls == ["connect", "status", "disconnect"]
    assert not client.active


def test_pending_cached_tools_cannot_pass():
    client = Client()
    client.responses = [status("pending")]
    result = invoke(client, "prior-session", startup_timeout=.015)
    assert result.error == runner.TIMEOUT_ERROR and not result.submitted
    assert "query" not in client.calls and client.calls[-1] == "disconnect"


def test_pending_then_connected_uses_same_client():
    client = Client()
    client.responses = [status("pending"), status()]
    assert invoke(client).status == "success"
    assert client.calls[:4] == ["connect", "status", "status", "query"]


@pytest.mark.parametrize("stage", ["connect", "status", "query", "receive", "disconnect"])
def test_ordinary_errors_sanitized_and_never_replayed(stage):
    client = Client()
    client.errors[stage] = OSError(MARKER)
    result = invoke(client, "prior-session")
    assert result.status == "error" and result.upstream_session_id == "prior-session"
    assert MARKER not in repr(result)
    assert client.calls.count("query") <= 1 and client.calls.count("disconnect") == 1
    assert result.submitted == (stage in ("query", "receive", "disconnect"))
    assert result.cleanup_complete == (stage != "disconnect")
    assert not client.active


@pytest.mark.parametrize("stage", ["connect", "status", "query", "receive", "disconnect"])
def test_cooperative_timeouts_cleanup_without_background_work(stage):
    client = Client()
    client.delay[stage] = 5
    started = time.monotonic()
    result = invoke(client, "prior-session", startup_timeout=.02)
    assert time.monotonic() - started < 1
    assert result.status == "error" and result.upstream_session_id == "prior-session"
    assert result.submitted == (stage in ("query", "receive", "disconnect"))
    assert not client.active
    assert result.cleanup_complete == (stage != "disconnect")


def test_one_budget_for_connect_and_status():
    client = Client()
    client.delay.update(connect=.035, status=.035)
    result = invoke(client, startup_timeout=.05)
    assert not result.submitted and result.error == runner.TIMEOUT_ERROR


@pytest.mark.parametrize("stage", ["before", "connect", "status", "query", "receive"])
def test_event_cancellation_checks_last_point_before_query(stage):
    client = Client()
    if stage == "before":
        client.cancel.set()
    else:
        client.cancel_at = stage
        if stage in ("query", "receive"):
            client.delay["receive"] = .02
    result = invoke(client, "prior-session")
    assert result.status == "cancelled" and result.upstream_session_id == "prior-session"
    if stage in ("before", "connect", "status"):
        assert not result.submitted and "query" not in client.calls
    assert not client.active


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
@pytest.mark.parametrize("stage", ["connect", "status", "query", "receive", "disconnect"])
def test_control_flow_propagates_and_disconnects(exception, stage):
    client = Client()
    client.errors[stage] = exception()
    with pytest.raises(BaseException) as caught:
        invoke(client)
    def includes(exc):
        return isinstance(exc, exception) or any(includes(e) for e in getattr(exc, "exceptions", []))
    assert includes(caught.value)
    assert client.calls[-1] == "disconnect" and not client.active


def test_no_sdk_safe_import_and_actionable_failure(monkeypatch):
    def unavailable():
        raise ImportError(MARKER)
    monkeypatch.setattr(runner, "_load_sdk", unavailable)
    result = runner.run_turn(options=SimpleNamespace(resume="old"), prompt="task", upstream_session_id="old")
    assert result.error == runner.UNAVAILABLE and not result.submitted
    assert result.upstream_session_id == "old" and MARKER not in repr(result)


def test_wrong_version_never_constructs_client():
    def forbidden(**kwargs):
        pytest.fail("wrong SDK must not launch")
    result = runner.run_turn(options=None, prompt="task", binding=runner.SDKBinding("0.1.0", forbidden, Result))
    assert result.error == runner.UNAVAILABLE


def test_metadata_version_checked_before_sdk_import(monkeypatch):
    monkeypatch.setattr(runner.metadata, "version", lambda name: "0.1.0")
    monkeypatch.setattr(runner, "import_module", lambda name: pytest.fail("must not import unsupported SDK"))
    assert runner.run_turn(options=None, prompt="task").error == runner.UNAVAILABLE


def test_active_loop_sync_bridge_fails_without_starting_client():
    async def check():
        client = Client()
        result = invoke(client, "prior-session")
        assert "asynchronous runner" in result.error and not client.calls
    anyio.run(check)


def test_resume_mismatch_fails_without_connect():
    client = Client()
    assert invoke(client, "prior-session", options=SimpleNamespace(resume=None)).status == "error"
    assert not client.calls


def test_permission_catalog_required_by_default_but_optional_for_other_hooks():
    client = Client()
    client.responses = [status(names=NAMES[:2])]
    assert not invoke(client).submitted
    assert invoke(client, require_permission_tool=False).status == "success"


def test_async_message_consumer_and_query_run_concurrently():
    client = Client()
    messages = []
    async def consume(message):
        await anyio.sleep(0)
        messages.append(message)
    assert invoke(client, on_message=consume).status == "success"
    assert len(messages) == 1 and isinstance(messages[0], Result)
    assert MARKER not in repr(messages)


def test_startup_output_is_drained_while_status_waits():
    class StreamingClient(Client):
        async def connect(self, prompt=None):
            await super().connect(prompt)
            self.drained = anyio.Event()

        async def get_mcp_status(self):
            await self.drained.wait()
            return await super().get_mcp_status()

        async def receive_response(self):
            yield SimpleNamespace(config=MARKER)
            self.drained.set()
            async for message in super().receive_response():
                yield message
    client = StreamingClient()
    seen = []
    async def consume(message):
        seen.append(message)
    assert invoke(client, on_message=consume).status == "success"
    assert len(seen) == 1 and isinstance(seen[0], Result)


@pytest.mark.parametrize("stage", ["connect", "status", "query", "receive"])
def test_cancel_during_blocked_operation_leaves_no_tasks(stage):
    async def check():
        client = Client()
        client.delay[stage] = 5
        result = None
        async with anyio.create_task_group() as tasks:
            async def trigger():
                while stage not in client.calls:
                    await anyio.sleep(.001)
                client.cancel.set()
            tasks.start_soon(trigger)
            result = await runner.run_turn_async(
                options=SimpleNamespace(resume="old"), prompt="task", upstream_session_id="old",
                cancel=client.cancel, poll_interval=.001,
                binding=runner.SDKBinding(runner.SDK_VERSION, client.factory, Result),
            )
        assert result.status == "cancelled" and result.upstream_session_id == "old"
        assert result.submitted == (stage in ("query", "receive"))
        assert not client.active
    anyio.run(check)


def test_cleanup_timeout_takes_precedence_over_cancelled_turn():
    client = Client()
    client.cancel_at = "status"
    client.delay["disconnect"] = 5
    result = invoke(client, "old")
    assert result.error == runner.CLEANUP_ERROR and not result.cleanup_complete
    assert result.upstream_session_id == "old" and not result.submitted


def test_shielded_sdk_cleanup_is_explicitly_not_a_hard_deadline():
    class Shielded(Client):
        async def disconnect(self):
            with anyio.CancelScope(shield=True):
                await anyio.sleep(.08)
    started = time.monotonic()
    result = invoke(Shielded())
    assert time.monotonic() - started >= .08
    assert not result.cleanup_complete and result.error == runner.CLEANUP_ERROR


def test_callback_failure_is_sanitized_after_submission():
    async def consume(message):
        raise RuntimeError(MARKER)
    client = Client()
    result = invoke(client, "old", on_message=consume)
    assert result.error == runner.TURN_ERROR and result.submitted
    assert result.upstream_session_id == "old" and MARKER not in repr(result)
    assert not client.active


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_deadline_never_constructs_client(timeout):
    client = Client()
    assert not invoke(client, startup_timeout=timeout).submitted
    assert not client.calls


def test_sdk_failure_result_preserves_original_session():
    class FailureResult(Result):
        is_error = True
        session_id = "uncertain-new-session"
    class FailureClient(Client):
        async def receive_response(self):
            await self.sent.wait()
            yield FailureResult()
    result = invoke(FailureClient(), "old")
    assert result.status == "error" and result.submitted and result.upstream_session_id == "old"


def test_shielded_late_status_never_submits_after_startup_budget():
    class ShieldedStatus(Client):
        async def get_mcp_status(self):
            with anyio.CancelScope(shield=True):
                await anyio.sleep(.04)
                return status()
    client = ShieldedStatus()
    result = invoke(client, startup_timeout=.01)
    assert result.error == runner.TIMEOUT_ERROR and not result.submitted
    assert "query" not in client.calls


def test_cancel_arriving_during_cleanup_retains_session():
    client = Client()
    client.cancel_at = "disconnect"
    result = invoke(client, "old")
    assert result.status == "cancelled" and result.upstream_session_id == "old"
    assert result.submitted and result.cleanup_complete


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError])
@pytest.mark.parametrize("stage", ["connect", "status", "query", "receive", "disconnect"])
def test_control_flow_exact_identity_after_cleanup(exception, stage):
    client = Client()
    original = exception(MARKER)
    client.errors[stage] = original
    with pytest.raises(BaseException) as caught:
        invoke(client, "old")
    assert caught.value is original
    assert client.calls[-1] == "disconnect" and not client.active


@pytest.mark.parametrize("session", ["", "old"])
def test_failed_result_never_reaches_callback(session):
    class Failed(Result):
        is_error = True
        result = MARKER
        errors = [MARKER]
    class FailedClient(Client):
        async def receive_response(self):
            await self.sent.wait()
            yield Failed()
    seen = []
    async def consume(message):
        seen.append(message)
    result = invoke(FailedClient(), session, on_message=consume)
    assert result.error == runner.TURN_ERROR and result.upstream_session_id == session
    assert seen == [] and MARKER not in repr(result)


@pytest.mark.parametrize("session", ["", "old"])
def test_immediate_status_and_buffered_final_never_query(session):
    class Buffered(Client):
        async def get_mcp_status(self):
            self.calls.append("status")
            return status()
        async def receive_response(self):
            yield Result()
    client = Buffered()
    result = invoke(client, session)
    assert not result.submitted and "query" not in client.calls
    assert result.upstream_session_id == session and result.status == "error"


def test_synchronous_ready_without_receiver_start_fails_closed():
    class Immediate(Client):
        async def get_mcp_status(self):
            return status()
    client = Immediate()
    result = invoke(client, "old")
    assert not result.submitted and result.error == runner.READINESS_ERROR
    assert "query" not in client.calls


def test_startup_final_before_status_completion_never_submits():
    class Buffered(Client):
        async def connect(self, prompt=None):
            await super().connect(prompt)
            self.final_yielded = anyio.Event()
        async def receive_response(self):
            self.final_yielded.set()
            yield Result()
        async def get_mcp_status(self):
            await self.final_yielded.wait()
            return status()
    client = Buffered()
    result = invoke(client, "old")
    assert not result.submitted and result.upstream_session_id == "old"
    assert "query" not in client.calls


@pytest.mark.parametrize("boundary", ["eof", "callback_timeout"])
def test_stream_boundary_failure_retains_session_and_cleans(boundary):
    class Stream(Client):
        async def receive_response(self):
            await self.sent.wait()
            if boundary != "eof":
                yield Result()
    async def callback(message):
        await anyio.sleep(5)
    client = Stream()
    result = invoke(client, "old", on_message=callback)
    assert result.error == runner.TURN_ERROR and result.submitted
    assert result.upstream_session_id == "old" and result.cleanup_complete
    assert client.calls[-1] == "disconnect" and not client.active


def test_successful_answer_and_tool_messages_preserved():
    tool = SimpleNamespace(type="tool", content="legitimate tool content")
    answer = Result()
    answer.result = "legitimate answer"
    class Stream(Client):
        async def receive_response(self):
            await self.sent.wait()
            yield tool
            yield answer
    seen = []
    async def callback(message):
        seen.append(message)
    result = invoke(Stream(), on_message=callback)
    assert result.status == "success" and seen == [tool, answer]


@pytest.mark.parametrize("cleanup_exception", [GeneratorExit, OSError])
def test_primary_control_and_cleanup_failure_both_preserved(cleanup_exception):
    client = Client()
    primary, secondary = GeneratorExit("primary"), cleanup_exception("secondary")
    client.errors.update(status=primary, disconnect=secondary)
    with pytest.raises(BaseExceptionGroup) as caught:
        invoke(client)
    assert caught.value.exceptions == (primary, secondary)
    assert not client.active


@pytest.mark.parametrize("session", ["", "old"])
@pytest.mark.parametrize("buffered", [False, True])
def test_default_production_route_rejects_uncertified_ordering(monkeypatch, session, buffered):
    class LoadedClient(Client):
        async def receive_response(self):
            if buffered:
                yield Result()
            else:
                async for message in super().receive_response():
                    yield message
    client = LoadedClient()
    monkeypatch.setattr(runner, "_load_sdk", lambda: runner.SDKBinding(
        runner.SDK_VERSION, client.factory, Result))
    seen = []
    async def callback(message):
        seen.append(message)
    result = runner.run_turn(options=SimpleNamespace(resume=session or None),
        prompt="original user task", upstream_session_id=session, on_message=callback)
    assert result.status == "error" and not result.submitted
    assert result.upstream_session_id == session
    assert "response ownership" in result.error.lower()
    assert client.calls == [] and seen == []
