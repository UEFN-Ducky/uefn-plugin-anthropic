"""Disposable local Windows processes only; no SDK/provider or shared service."""
import asyncio
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

_spec = importlib.util.spec_from_file_location("_owned_test", Path(__file__).with_name("claude_owned_transport.py"))
t = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = t
_spec.loader.exec_module(t)
MARKER = "PRIVATE_TRANSPORT_MARKER"


def spec(tmp_path, source="import time; time.sleep(60)", **limits):
    script = tmp_path / "child.py"
    script.write_text(source, encoding="utf-8")
    return t.LaunchSpec((sys.executable, "-B", "-u", str(script)),
        tuple(os.environ.items()), str(tmp_path),
        t.Ownership.DISPOSABLE_TREE_EXTERNAL_HOST_SERVICES,
        t.Limits(read=2, cleanup=3, **limits))


def run(coro):
    return asyncio.run(coro)


def test_real_roundtrip_and_idempotent_cleanup(tmp_path):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, 'import sys,json\nfor line in sys.stdin: print(json.dumps({"echo":line.strip()}),flush=True)'))
        try:
            await transport.connect()
            await transport.write('original prompt\n')
            stream = transport.read_messages()
            assert await anext(stream) == {"echo": "original prompt"}
            await transport.end_input()
            assert [item async for item in stream] == []
            assert transport.write_attempts == 1
        finally:
            await transport.close()
        await transport.close()
        assert transport.cleanup_complete and not transport.is_ready()
        assert not transport._ops and transport._pump_task is None
    run(case())


def test_grandchild_survives_parent_then_job_kills_it_not_external_sentinel(tmp_path):
    import win32api, win32event
    sentinel = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"], creationflags=0x08000000)
    async def case():
        child = "import time; time.sleep(60)"
        source = f'import subprocess,sys,json,os\np=subprocess.Popen([sys.executable,"-B","-c",{child!r}])\nprint(json.dumps({{"pid":p.pid}}),flush=True)\nos._exit(0)'
        transport = t.OwnedTransport(spec(tmp_path, source))
        handle = None
        try:
            await transport.connect()
            msg = await anext(transport.read_messages())
            handle = win32api.OpenProcess(0x100000, False, msg["pid"])
            assert win32event.WaitForSingleObject(handle, 0) == 258
            # Parent exit cannot release a descendant from the job.
            deadline = time.monotonic() + 2
            while transport._win.api.WaitForSingleObject(transport._process, 0) != 0:
                assert time.monotonic() < deadline
                await asyncio.sleep(.002)
            await transport.close()
            assert win32event.WaitForSingleObject(handle, 0) == 0
            assert sentinel.poll() is None
            assert transport.cleanup_complete
        finally:
            await transport.close()
            if handle is not None: handle.Close()
    try:
        run(case())
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


def test_assignment_rejection_never_executes_suspended_child(tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    def reject(*args): raise OSError(MARKER)
    monkeypatch.setattr(t._Windows, "assign", reject)
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, f'open({str(marker)!r},"w").write("bad")'))
        with pytest.raises(t.TransportError) as caught:
            await transport.connect()
        assert MARKER not in str(caught.value)
        assert not marker.exists()
        assert transport.cleanup_complete
    run(case())


@pytest.mark.parametrize("output", [b'\xff\xfe\n', b'{"secret":"PRIVATE_TRANSPORT_MARKER"\n', b'{}', b'[]\n', b'x'*513])
def test_malformed_partial_oversized_frames_fail_closed(tmp_path, output):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, f'import sys; sys.stdout.buffer.write({output!r}); sys.stdout.flush()', frame_bytes=512))
        try:
            await transport.connect()
            with pytest.raises(t.TransportError) as caught:
                await anext(transport.read_messages())
            assert MARKER not in str(caught.value)
            assert transport.cleanup_complete
        finally: await transport.close()
    run(case())


def test_blocked_stdin_write_deadline_and_uncertainty(tmp_path):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, write=.08, frame_bytes=4*1024*1024))
        try:
            await transport.connect()
            with pytest.raises(t.TransportError): await transport.write("x"*2_000_000)
            assert transport.partial_write_possible and transport.write_attempts == 1
            assert transport.cleanup_complete
        finally: await transport.close()
    run(case())


def test_saturated_outputs_bounded_and_private_stderr(tmp_path):
    source = 'import sys\nfor i in range(100000):\n sys.stderr.write("PRIVATE_TRANSPORT_MARKER"*100+"\\n"); sys.stderr.flush()\n print("{}",flush=True)'
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, source, ordinary_frames=2))
        try:
            await transport.connect()
            deadline = time.monotonic()+2
            while not transport._error:
                assert time.monotonic() < deadline
                await asyncio.sleep(.002)
            assert len(transport._ordinary) <= 2
            assert transport.stderr_bytes > 0
            assert MARKER not in transport.stderr_diagnostic
        finally: await transport.close()
        assert transport.cleanup_complete
    run(case())


def test_control_frames_have_reserved_capacity(tmp_path):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, 'import time\nprint(\'{"type":"assistant"}\',flush=True)\nprint(\'{"type":"control_request","id":"permission"}\',flush=True)\ntime.sleep(60)', ordinary_frames=1))
        try:
            await transport.connect()
            deadline = time.monotonic()+2
            while transport.ingress_count < 2:
                assert time.monotonic() < deadline
                await asyncio.sleep(.002)
            stream = transport.read_messages()
            assert (await anext(stream))["type"] == "control_request"
            assert (await anext(stream))["type"] == "assistant"
        finally: await transport.close()
    run(case())


@pytest.mark.parametrize("phase", ["connect", "read", "write"])
def test_cancellation_reaps_owned_resources(tmp_path, monkeypatch, phase):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, frame_bytes=4*1024*1024))
        try:
            if phase == "connect":
                original = t._Windows.assign
                def assign(win, job, proc):
                    original(win, job, proc)
                    asyncio.current_task().cancel("owned cancellation")
                monkeypatch.setattr(t._Windows, "assign", assign)
                operation = transport.connect()
            else:
                await transport.connect()
                operation = anext(transport.read_messages()) if phase == "read" else transport.write("x"*2_000_000)
            task = asyncio.create_task(operation)
            if phase != "connect":
                await asyncio.sleep(.04)
                task.cancel("owned cancellation")
            with pytest.raises(asyncio.CancelledError): await task
            assert transport.cleanup_complete and transport._pump_task is None
        finally: await transport.close()
    run(case())


@pytest.mark.parametrize("exception", [GeneratorExit(), KeyboardInterrupt(), SystemExit(), asyncio.CancelledError()])
def test_control_flow_identity_on_assignment(tmp_path, monkeypatch, exception):
    def reject(*args): raise exception
    monkeypatch.setattr(t._Windows, "assign", reject)
    async def case():
        transport = t.OwnedTransport(spec(tmp_path))
        with pytest.raises(type(exception)) as caught: await transport.connect()
        assert caught.value is exception
        assert transport.cleanup_complete
    run(case())


@pytest.mark.parametrize("changes", [{"argv":("relative",)}, {"env":(("A","1"),("a","2"))}, {"limits":t.Limits(cleanup="bad")}, {"ownership":None}])
def test_invalid_spec_sanitized_without_spawn(tmp_path, changes):
    async def case():
        transport = t.OwnedTransport(replace(spec(tmp_path), **changes))
        with pytest.raises(t.TransportError): await transport.connect()
        assert transport.pid is None and transport.cleanup_complete
    run(case())


@pytest.mark.parametrize("reject_nested", [False, True])
def test_real_nested_job_assignment(tmp_path, monkeypatch, reject_nested):
    win = t._Windows()
    outer = win.make_job()
    marker = tmp_path / "nested-executed"
    original = t._Windows.assign
    def nested(self, job, process):
        original(self, outer, process)
        if reject_nested:
            # A zero active-process limit produces a real kernel quota failure
            # while the child is nested and suspended, without depending on
            # the host's UI-restriction nesting policy.
            info = win.job.QueryInformationJobObject(job, win.job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win.job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info["BasicLimitInformation"]["ActiveProcessLimit"] = 0
            win.job.SetInformationJobObject(job, win.job.JobObjectExtendedLimitInformation, info)
        original(self, job, process)
    monkeypatch.setattr(t._Windows, "assign", nested)
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, f'import time\nopen({str(marker)!r},"w").write("ok")\nprint("{{}}",flush=True)\ntime.sleep(60)'))
        try:
            if reject_nested:
                with pytest.raises(t.TransportError): await transport.connect()
                assert not marker.exists()
            else:
                await transport.connect()
                stream = transport.read_messages()
                assert await anext(stream) == {}
                assert marker.read_text() == "ok"
                assert win.job.IsProcessInJob(transport._process, outer)
                assert win.job.IsProcessInJob(transport._process, transport._job)
        finally: await transport.close()
        assert transport.cleanup_complete
        assert win.active(outer) == 0
    try: run(case())
    finally:
        win.job.TerminateJobObject(outer, 1)
        outer.Close()


def test_real_job_and_handle_inheritance_flags(tmp_path, monkeypatch):
    original = t._Windows.spawn_suspended
    captured = []
    def spawn(win, launch, children):
        captured.extend(win.handles.GetHandleInformation(h) for h in children)
        return original(win, launch, children)
    monkeypatch.setattr(t._Windows, "spawn_suspended", spawn)
    async def case():
        transport = t.OwnedTransport(spec(tmp_path))
        try:
            await transport.connect()
            win = transport._win
            assert win.handles.GetHandleInformation(transport._job) & 1 == 0
            assert all(win.handles.GetHandleInformation(h) & 1 == 0 for h in transport._pipes)
            flags = win.job.QueryInformationJobObject(transport._job, win.job.JobObjectExtendedLimitInformation)["BasicLimitInformation"]["LimitFlags"]
            assert flags & win.job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            assert not flags & (win.job.JOB_OBJECT_LIMIT_BREAKAWAY_OK | win.job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)
            assert captured == [1, 1, 1]
        finally: await transport.close()
    run(case())


def test_read_timeout_reaps_child(tmp_path):
    async def case():
        launch = replace(spec(tmp_path), limits=t.Limits(read=.06, cleanup=3))
        transport = t.OwnedTransport(launch)
        try:
            await transport.connect()
            with pytest.raises(t.TransportError): await anext(transport.read_messages())
            assert transport.cleanup_complete
        finally: await transport.close()
    run(case())


def test_startup_deadline_prevents_execution(tmp_path, monkeypatch):
    marker = tmp_path / "late"
    original = t._Windows.assign
    def slow(self, job, process):
        original(self, job, process)
        time.sleep(.02)  # model cooperative deadline overrun in synchronous setup
    monkeypatch.setattr(t._Windows, "assign", slow)
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, f'open({str(marker)!r},"w").close()', startup=.005))
        with pytest.raises(t.TransportError): await transport.connect()
        assert transport.cleanup_complete and not marker.exists()
    run(case())


def test_serialized_writes_keep_frames_intact(tmp_path):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, 'import sys\nfor line in sys.stdin: print(line, end="",flush=True)'))
        try:
            await transport.connect()
            await asyncio.gather(*(transport.write(json.dumps({"i": i})+"\n") for i in range(15)))
            await transport.end_input()
            result = [m async for m in transport.read_messages()]
            assert sorted(m["i"] for m in result) == list(range(15))
        finally: await transport.close()
    run(case())


def test_end_input_deadline_when_writer_holds_lock(tmp_path):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, write=.04))
        try:
            await transport.connect()
            await transport._write_lock.acquire()
            with pytest.raises(t.TransportError): await transport.end_input()
            assert transport.cleanup_complete
        finally:
            if transport._write_lock.locked(): transport._write_lock.release()
            await transport.close()
    run(case())


def test_repeated_sessions_no_handle_or_task_growth(tmp_path):
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    def count():
        value = ctypes.c_ulong()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value
    async def case():
        # Warm up lazy pywin32 and event-loop initialization first.
        warm = t.OwnedTransport(spec(tmp_path))
        await warm.connect(); await warm.close()
        before = count()
        tasks = set(asyncio.all_tasks())
        for _ in range(8):
            transport = t.OwnedTransport(spec(tmp_path))
            try: await transport.connect()
            finally: await transport.close()
            assert transport.cleanup_complete
        assert set(asyncio.all_tasks()) == tasks
        assert count() == before
    run(case())


def test_cleanup_unconfirmed_is_explicit_and_retryable(tmp_path, monkeypatch):
    async def case():
        transport = t.OwnedTransport(replace(spec(tmp_path), limits=t.Limits(cleanup=.03)))
        await transport.connect()
        original = transport._win.active
        monkeypatch.setattr(transport._win, "active", lambda job: 1)
        try:
            with pytest.raises(t.TransportError, match="cleanup could not be confirmed"):
                await transport.close()
            assert not transport.cleanup_complete
            assert transport._job is not None
            assert transport._pump_task.done()
        finally:
            monkeypatch.setattr(transport._win, "active", original)
            await transport.close()
        assert transport.cleanup_complete
    run(case())


def test_unsupported_platform_import_safe(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(t, "os", SimpleNamespace(name="posix"))
    with pytest.raises(t.TransportError, match="requires Windows"): t._Windows()


@pytest.mark.parametrize("version", [None, "0.2.164", "0.2.165", "99"])
def test_sdk_pin_probe_never_installs_or_imports_sdk(monkeypatch, version):
    def installed(name):
        assert name == "claude-agent-sdk"
        if version is None: raise t.importlib.metadata.PackageNotFoundError(name)
        return version
    monkeypatch.setattr(t.importlib.metadata, "version", installed)
    assert t.sdk_available() is (version == "0.2.165")


@pytest.mark.parametrize("option", ["hooks", "session_store", "can_use_tool", "mcp_servers", "stderr", "allowed_tools", "setting_sources", "resume", "model"])
def test_unmapped_sdk_options_not_silently_accepted(tmp_path, option):
    launch = spec(tmp_path)
    with pytest.raises(TypeError): t.LaunchSpec(launch.argv, launch.env, launch.cwd, launch.ownership, **{option: object()})


def test_close_concurrent_with_blocked_writer(tmp_path):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, frame_bytes=4*1024*1024))
        writer = None
        try:
            await transport.connect()
            writer = asyncio.create_task(transport.write("x"*2_000_000))
            while transport.write_attempts == 0: await asyncio.sleep(.002)
            await transport.close()
            with pytest.raises(t.TransportError): await writer
            assert transport.cleanup_complete and not transport._ops
        finally:
            if writer is not None and not writer.done():
                writer.cancel()
                try: await writer
                except asyncio.CancelledError: pass
            await transport.close()
    run(case())


def test_read_write_failure_markers_are_sanitized(tmp_path, monkeypatch):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path))
        try:
            await transport.connect()
            def fail(*args): raise t.TransportError(MARKER)
            monkeypatch.setattr(transport, "_new_op", fail)
            with pytest.raises(t.TransportError) as caught: await transport.write("data\n")
            assert MARKER not in str(caught.value)
        finally: await transport.close()
    run(case())


def test_setup_transport_error_marker_is_not_trusted(tmp_path, monkeypatch):
    def fail(*args): raise t.TransportError(MARKER)
    monkeypatch.setattr(t._Windows, "assign", fail)
    async def case():
        transport = t.OwnedTransport(spec(tmp_path))
        with pytest.raises(t.TransportError) as caught: await transport.connect()
        assert MARKER not in str(caught.value)
        assert transport.cleanup_complete
    run(case())


@pytest.mark.parametrize("exception", [GeneratorExit(), KeyboardInterrupt(), SystemExit(), asyncio.CancelledError()])
def test_pipe_control_flow_identity_after_owned_cleanup(tmp_path, monkeypatch, exception):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, 'print("{}",flush=True)'))
        def fail(*args): raise exception
        monkeypatch.setattr(transport, "_parse", fail)
        try:
            await transport.connect()
            with pytest.raises(type(exception)) as caught:
                await anext(transport.read_messages())
            assert caught.value is exception
            assert transport.cleanup_complete and transport._pump_task is None
        finally: await transport.close()
    run(case())


def test_cancel_during_close_wait_still_finishes_cleanup(tmp_path, monkeypatch):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path))
        await transport.connect()
        original = transport._win.active
        once = []
        def active(job):
            if not once:
                once.append(True)
                asyncio.current_task().cancel("cleanup cancel")
                return 1
            return original(job)
        monkeypatch.setattr(transport._win, "active", active)
        with pytest.raises(asyncio.CancelledError): await transport.close()
        assert transport.cleanup_complete and not transport._ops
        await transport.close()
    run(case())


def test_unlisted_inheritable_handle_not_inherited(tmp_path):
    win = t._Windows()
    sa = win.types.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = True
    event = win.event.CreateEvent(sa, True, False, None)
    source = f'import win32event,json\ntry:\n win32event.SetEvent({int(event)})\n inherited=True\nexcept Exception: inherited=False\nprint(json.dumps({{"inherited":inherited}}),flush=True)'
    async def case():
        transport = t.OwnedTransport(spec(tmp_path, source))
        try:
            await transport.connect()
            stream = transport.read_messages()
            assert await anext(stream) == {"inherited": False}
            assert win.event.WaitForSingleObject(event, 0) == 258
        finally: await transport.close()
    try: run(case())
    finally: event.Close()


@pytest.mark.parametrize("version", [None, "0.2.164"])
def test_missing_wrong_sdk_explicit_actionable_error(monkeypatch, version):
    def installed(name):
        if version is None: raise t.importlib.metadata.PackageNotFoundError(name)
        return version
    monkeypatch.setattr(t.importlib.metadata, "version", installed)
    with pytest.raises(t.TransportError, match="0.2.165 must be supplied by the host"):
        t.require_supported_sdk()


def test_new_host_helpers_not_required_for_import(monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.startswith(("backend.agent", "claude_agent_sdk")):
            raise AssertionError("host dependency imported eagerly")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    namespace = {"__name__":"_old_host_owned"}
    # Dataclasses require a module, so use an ordinary fresh module registration.
    from types import ModuleType
    module = ModuleType(namespace["__name__"])
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(Path(t.__file__).read_text(encoding="utf-8"), t.__file__, "exec"), module.__dict__)
    assert module.OwnedTransport and module.LaunchSpec


def test_handle_close_failure_is_sanitized_incomplete_then_retry(tmp_path):
    async def case():
        transport = t.OwnedTransport(spec(tmp_path))
        await transport.connect()
        original = transport._pipes[0]
        class FailsOnce:
            failed = False
            def Close(self):
                if not self.failed:
                    self.failed = True
                    raise OSError(MARKER)
                original.Close()
        transport._pipes[0] = FailsOnce()
        try:
            with pytest.raises(t.TransportError) as caught: await transport.close()
            assert MARKER not in str(caught.value)
            assert not transport.cleanup_complete
            await transport.close()
            assert transport.cleanup_complete and not transport._pipes
        finally: await transport.close()
    run(case())


def test_control_flow_preserved_when_cleanup_unconfirmed(tmp_path, monkeypatch):
    exception = GeneratorExit()
    original = t._Windows.active
    monkeypatch.setattr(t._Windows, "active", lambda *args: 1)
    def fail(*args): raise exception
    monkeypatch.setattr(t._Windows, "assign", fail)
    async def case():
        transport = t.OwnedTransport(replace(spec(tmp_path), limits=t.Limits(cleanup=.03)))
        try:
            with pytest.raises(GeneratorExit) as caught: await transport.connect()
            assert caught.value is exception and not transport.cleanup_complete
        finally:
            monkeypatch.setattr(t._Windows, "active", original)
            await transport.close()
        assert transport.cleanup_complete
    run(case())


@pytest.mark.parametrize("mode", ["success", "cancel", "close"])
def test_overlapping_connect_has_one_resource_owner(tmp_path, monkeypatch, mode):
    saved = []
    original = t._Windows.spawn_suspended
    marker = tmp_path / "overlap-executed"
    def capture(win, launch, children):
        handles = original(win, launch, children)
        saved.append((win, handles))
        if mode == "cancel": asyncio.current_task().cancel("startup cancellation")
        return handles
    monkeypatch.setattr(t._Windows, "spawn_suspended", capture)
    async def case():
        obj = t.OwnedTransport(spec(tmp_path, f'import time\nopen({str(marker)!r},"w").close()\ntime.sleep(30)'))
        try:
            calls = [obj.connect(), obj.connect()]
            if mode == "close": calls.append(obj.close())
            outcomes = await asyncio.gather(*calls, return_exceptions=True)
            if mode == "success":
                assert outcomes[0] is None and obj.is_ready()
            else:
                assert isinstance(outcomes[0], asyncio.CancelledError if mode == "cancel" else t.TransportError)
                assert not marker.exists()
            assert isinstance(outcomes[1], t.TransportError)
            assert len(saved) == 1
            await obj.close()
            assert obj.cleanup_complete and obj._pump_task is None
            for win, handles in saved:
                for handle in handles[:2]:
                    with pytest.raises(win.handles.error) as caught:
                        win.handles.GetHandleInformation(handle)
                    assert caught.value.winerror == 6  # ERROR_INVALID_HANDLE
        finally:
            await obj.close()
            # Clean exact recorded handles even on the pre-fix leak reproduction.
            for win, handles in saved:
                for handle in handles[:2]:
                    try: win.api.CloseHandle(handle)
                    except OSError: pass
    run(case())


def test_repeated_overlapping_connect_no_handle_growth(tmp_path, monkeypatch):
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    def count():
        value = ctypes.c_ulong()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value
    async def case():
        warm = t.OwnedTransport(spec(tmp_path))
        await warm.connect(); await warm.close()
        before, tasks = count(), set(asyncio.all_tasks())
        saved = []
        original = t._Windows.spawn_suspended
        def capture(win, launch, children):
            handles = original(win, launch, children)
            saved.append((win, handles))
            return handles
        monkeypatch.setattr(t._Windows, "spawn_suspended", capture)
        for _ in range(6):
            obj = t.OwnedTransport(spec(tmp_path))
            try:
                outcomes = await asyncio.gather(obj.connect(), obj.connect(), return_exceptions=True)
                assert outcomes[0] is None and isinstance(outcomes[1], t.TransportError)
            finally:
                await obj.close()
                current_count = count()
                for win, handles in saved:
                    for handle in handles[:2]:
                        try: win.api.CloseHandle(handle)
                        except OSError: pass
                saved.clear()
            assert current_count == before
        assert count() == before
        assert set(asyncio.all_tasks()) == tasks
    run(case())
