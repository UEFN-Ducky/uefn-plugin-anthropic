"""Unused Windows process transport; no model/result/readiness certification.

Implements claude-agent-sdk 0.2.165's exported Transport method signatures.
The SDK is optional at import. Windows implementation needs CPython's Windows
stdlib and the host's existing pywin32; neither is installed by this module.

LaunchSpec is an explicit raw argv/env/cwd boundary, NOT a ClaudeAgentOptions
mapper. Integration must build/review option parity, exclude in-process SDK MCP,
session_store, can_use_tool/hooks/stderr callbacks from a bounded profile, and
preserve CLI permission_prompt_tool/settings hooks. No SDK options are silently
accepted here. All descendants must be disposable; host services must already
be external and their bootstrap must never be included in argv/MCP config.
The ownership enum records that obligation, not proof of daemon readiness.

CreateProcess(CREATE_SUSPENDED), HANDLE_LIST, AssignProcessToJobObject, then
ResumeThread prevent execution outside the owned Job. No breakaway flags.
Microsoft contracts: /windows/win32/procthread/process-creation-flags,
/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute,
/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject,
/windows/win32/api/ioapiset/nf-ioapiset-cancelio (learn.microsoft.com).

Deadlines bound cooperative polling, not stalled kernel/Python calls. Incomplete
I/O retains its buffer/OVERLAPPED and handles for retrying close; never free live
OVERLAPPED memory. No unjoinable threads, SDK-private handles or process-name
kills. Ingress/write counters describe local observations, NOT causal ownership.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

SDK_VERSION = "0.2.165"
SDK_UNAVAILABLE = "Ducky tools unavailable: the supported Claude SDK 0.2.165 must be supplied by the host before transport integration."
UNAVAILABLE = "Ducky tools unavailable: owned transport requires Windows and the supported host runtime."
INVALID = "Ducky tools unavailable: invalid owned-process launch specification."
FAILED = "Ducky tools unavailable: owned transport failed; check the supported session setup."
INCOMPLETE = "Ducky tools unavailable: owned process cleanup could not be confirmed."


class TransportError(RuntimeError):
    pass


class Ownership(Enum):
    DISPOSABLE_TREE_EXTERNAL_HOST_SERVICES = "disposable-tree-external-host-services"


@dataclass(frozen=True)
class Limits:
    startup: float = 5.0
    write: float = 5.0
    read: float = 30.0
    cleanup: float = 5.0
    frame_bytes: int = 1024 * 1024
    ordinary_frames: int = 64
    control_frames: int = 16


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cwd: str
    ownership: Ownership
    limits: Limits = Limits()

    def validate(self):
        if (type(self.argv) is not tuple or not self.argv or
                any(not isinstance(v, str) or not v or "\0" in v for v in self.argv) or
                not Path(self.argv[0]).is_absolute() or
                not isinstance(self.cwd, str) or "\0" in self.cwd or
                not Path(self.cwd).is_absolute() or
                self.ownership is not Ownership.DISPOSABLE_TREE_EXTERNAL_HOST_SERVICES or
                type(self.env) is not tuple or not isinstance(self.limits, Limits)):
            raise TransportError(INVALID)
        keys = set()
        for pair in self.env:
            if (type(pair) is not tuple or len(pair) != 2 or
                    any(not isinstance(x, str) or "\0" in x for x in pair) or
                    not pair[0] or "=" in pair[0] or pair[0].casefold() in keys):
                raise TransportError(INVALID)
            keys.add(pair[0].casefold())
        for value in (self.limits.startup, self.limits.write, self.limits.read, self.limits.cleanup):
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 3600:
                raise TransportError(INVALID)
        for value, maximum in ((self.limits.frame_bytes, 4 * 1024 * 1024),
                               (self.limits.ordinary_frames, 1024), (self.limits.control_frames, 128)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise TransportError(INVALID)


def sdk_available():
    """No SDK import/installation, and no app-base dependency."""
    try:
        return importlib.metadata.version("claude-agent-sdk") == SDK_VERSION
    except importlib.metadata.PackageNotFoundError:
        return False


def require_supported_sdk():
    """Lazy public-contract check for future integration, never an installer.

    Pinned public exports/client seam:
    https://github.com/anthropics/claude-agent-sdk-python/tree/v0.2.165/src/claude_agent_sdk
    The standalone process primitive remains testable without that dependency.
    This does not map options or construct an SDK client.
    """
    try:
        if not sdk_available():
            raise TransportError(SDK_UNAVAILABLE)
        from claude_agent_sdk import Transport
        expected = {"connect", "write", "read_messages", "close", "is_ready", "end_input"}
        if not expected <= set(dir(Transport)):
            raise TransportError(SDK_UNAVAILABLE)
        return Transport
    except Exception:
        raise TransportError(SDK_UNAVAILABLE) from None


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise TransportError(UNAVAILABLE)
        try:
            import _winapi
            import win32api
            import win32event
            import win32file
            import win32job
            import win32pipe
            import win32process
            import pywintypes
        except ImportError:
            raise TransportError(UNAVAILABLE) from None
        self.api, self.file, self.job = _winapi, win32file, win32job
        self.event, self.pipe = win32event, win32pipe
        self.process, self.types, self.handles = win32process, pywintypes, win32api

    def make_job(self):
        # This host's pywin32 wrapper rejects None for the name. An empty
        # string can name an object; use the native NULL contract explicitly.
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateJobObjectW
        create.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        create.restype = ctypes.c_void_p
        raw = create(None, None)
        if not raw:
            raise TransportError(FAILED)
        job = self.types.HANDLE(raw)
        try:
            info = self.job.QueryInformationJobObject(job, self.job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] = self.job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self.job.SetInformationJobObject(job, self.job.JobObjectExtendedLimitInformation, info)
            return job
        except BaseException:
            job.Close()
            raise

    def pipe_pair(self, read):
        name = r"\\.\pipe\ducky-owned-" + uuid.uuid4().hex
        parent = self.pipe.CreateNamedPipe(name,
            (1 if read else 2) | 0x40000000 | 0x00080000,  # overlapped, first instance
            8, 1, 65536, 65536, 0, None)  # byte pipe; reject remote clients
        try:
            sa = self.types.SECURITY_ATTRIBUTES()
            sa.bInheritHandle = True
            child = self.file.CreateFile(name, 0x40000000 if read else 0x80000000,
                                         0, sa, 3, 0, None)
            return parent, child
        except BaseException:
            parent.Close()
            raise

    def spawn_suspended(self, spec, children):
        si = subprocess.STARTUPINFO()
        si.dwFlags = subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        si.hStdInput, si.hStdOutput, si.hStdError = map(int, children)
        si.lpAttributeList = {"handle_list": list(map(int, children))}
        return self.api.CreateProcess(spec.argv[0], subprocess.list2cmdline(spec.argv),
            None, None, True, 0x4 | 0x08000000 | 0x00080000,
            dict(spec.env), spec.cwd, si)

    def assign(self, job, process):
        self.job.AssignProcessToJobObject(job, process)

    def active(self, job):
        return self.job.QueryInformationJobObject(job, self.job.JobObjectBasicAccountingInformation)["ActiveProcesses"]


class _Operation:
    """Retains pywin32's I/O buffer until completion, including after cancellation."""
    def __init__(self, win, handle, data=None):
        self.win, self.handle = win, handle
        self.ov = win.types.OVERLAPPED()
        self.ov.hEvent = win.event.CreateEvent(None, True, False, None)
        self.done, self.count, self.error = False, 0, 0
        try:
            if data is None:
                self.buffer = win.file.AllocateReadBuffer(16384)
                win.file.ReadFile(handle, self.buffer, self.ov)
            else:
                self.buffer = data
                win.file.WriteFile(handle, data, self.ov)
        except win.types.error as exc:
            if exc.winerror != 997:  # ERROR_IO_PENDING
                self.done, self.error = True, exc.winerror
        except BaseException:
            self.ov.hEvent.Close()
            raise

    def poll(self):
        if not self.done:
            try:
                self.count = self.win.file.GetOverlappedResult(self.handle, self.ov, False)
                self.done = True
            except self.win.types.error as exc:
                if exc.winerror != 996:  # ERROR_IO_INCOMPLETE
                    self.done, self.error = True, exc.winerror
        return self.done

    def cancel(self):
        if not self.done:
            try:
                # All I/O is issued on this one asyncio thread. CancelIo cancels
                # this thread's operations for this owned handle; completion
                # must still be observed before freeing OVERLAPPED/buffers.
                self.win.file.CancelIo(self.handle)
            except self.win.types.error:
                pass  # poll completion; absence is never considered completion

    def dispose(self):
        if not self.poll():
            raise TransportError(INCOMPLETE)
        self.ov.hEvent.Close()


class OwnedTransport:
    """Public SDK Transport-shaped primitive; argv mapping/integration is absent.

    One asyncio loop owns it. A caller must await close (also after exceptions).
    No arbitrary SDK options/callbacks are accepted. The SDK's custom transport
    seam is duck typed; future integration must verify the exact pinned ABC.
    """
    def __init__(self, spec: LaunchSpec):
        self.spec = spec
        self._win = None
        self._job = self._process = self._thread = None
        self._pipes = []
        self._children = []
        self._ops = []
        self._owned_wait_handles = []
        self._owner_thread = threading.get_ident()
        self._loop = None
        self._pump_task = None
        self._closing = False
        self._connected = False
        self._error = False
        self._control_failure = None
        self._eof = False
        self._ordinary, self._control = deque(), deque()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self.cleanup_complete = True
        self.ingress_count = 0
        self.write_attempts = 0
        self.written_bytes = 0
        self.partial_write_possible = False
        self.stderr_bytes = 0  # fixed diagnostic only; never retain raw stderr
        self.pid = None

    @property
    def stderr_diagnostic(self):
        return "Owned process emitted diagnostic output." if self.stderr_bytes else ""

    def is_ready(self):
        return self._connected and not self._closing and not self._error

    async def connect(self):
        self._check_thread()
        if self._connected or self._closing:
            raise TransportError(FAILED)
        started = time.monotonic()
        self._loop = asyncio.get_running_loop()
        try:
            self.spec.validate()
            self._win = _Windows()
            self.cleanup_complete = False
            self._job = self._win.make_job()
            for read in (False, True, True):
                parent, child = self._win.pipe_pair(read)
                self._pipes.append(parent)
                self._children.append(child)
            self._process, self._thread, self.pid, _ = self._win.spawn_suspended(self.spec, self._children)
            self._win.assign(self._job, self._process)
            self._close_handles(self._children)
            if time.monotonic() - started >= self.spec.limits.startup:
                raise TransportError(FAILED)
            # First cancellation checkpoint while the child is still suspended.
            await asyncio.sleep(0)
            if time.monotonic() - started >= self.spec.limits.startup:
                raise TransportError(FAILED)
            self._win.process.ResumeThread(self._thread)
            self._win.api.CloseHandle(self._thread)
            self._thread = None
            self._connected = True
            self._pump_task = asyncio.create_task(self._pump(), name="claude-owned-pipes")
        except BaseException as exc:
            await self._after_failure(exc)
            if not isinstance(exc, Exception):
                raise
            reason = str(exc) if isinstance(exc, TransportError) and str(exc) in (INVALID, UNAVAILABLE) else FAILED
            raise TransportError(reason) from None

    def _new_op(self, handle, data=None):
        op = _Operation(self._win, handle, data)
        self._ops.append(op)
        return op

    def _check_thread(self):
        if (threading.get_ident() != self._owner_thread or
                (self._loop is not None and asyncio.get_running_loop() is not self._loop)):
            raise TransportError(FAILED)

    @staticmethod
    def _close_handles(handles):
        while handles:
            if handles[-1] is not None:
                handles[-1].Close()
            handles.pop()

    async def _after_failure(self, original):
        try:
            await self.close()
        except BaseException as cleanup:
            if not isinstance(original, Exception) and isinstance(cleanup, Exception):
                # cleanup_complete remains False; retain resource ownership for
                # retry, without replacing the original control-flow object.
                raise original from None
            if (not isinstance(original, Exception) and cleanup is not original
                    and not isinstance(cleanup, Exception)):
                raise BaseExceptionGroup("Concurrent owned transport cancellation", [original, cleanup]) from None
            raise

    def _release(self, op):
        if op in self._ops:
            op.dispose()
            self._ops.remove(op)

    def _parse(self, data):
        try:
            def invalid_constant(value):
                raise ValueError
            obj = json.loads(data.decode("utf-8"), parse_constant=invalid_constant)
            if not isinstance(obj, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise TransportError(FAILED) from None
        self.ingress_count += 1
        queue = self._control if obj.get("type") in ("control_request", "control_response", "control_cancel_request") else self._ordinary
        limit = self.spec.limits.control_frames if queue is self._control else self.spec.limits.ordinary_frames
        if len(queue) >= limit:
            # Bounded fail-closed overload, never block a reader behind a full
            # ordinary queue while waiting for a later control frame.
            raise TransportError(FAILED)
        queue.append(obj)

    async def _pump(self):
        pending = {1: None, 2: None}
        buffer = bytearray()
        try:
            while pending and not self._closing:
                for index in list(pending):
                    op = pending[index]
                    if op is None:
                        op = pending[index] = self._new_op(self._pipes[index])
                    if not op.poll():
                        continue
                    raw = bytes(op.buffer[:op.count])
                    error = op.error
                    self._release(op)
                    pending[index] = None
                    if error in (109, 232) or (not error and not raw):
                        del pending[index]
                        if index == 1:
                            if buffer:
                                raise TransportError(FAILED)
                            self._eof = True
                        continue
                    if error:
                        raise TransportError(FAILED)
                    if index == 2:
                        self.stderr_bytes = min(2**63 - 1, self.stderr_bytes + len(raw))
                        continue
                    buffer.extend(raw)
                    while b"\n" in buffer:
                        pos = buffer.index(10)
                        if pos > self.spec.limits.frame_bytes:
                            raise TransportError(FAILED)
                        frame = bytes(buffer[:pos])
                        del buffer[:pos + 1]
                        self._parse(frame)
                    if len(buffer) > self.spec.limits.frame_bytes:
                        raise TransportError(FAILED)
                await asyncio.sleep(.002)
        except Exception:
            self._error = True
            self._terminate()
        except BaseException as exc:
            self._control_failure = exc
            self._error = True
            self._terminate()
        finally:
            for op in pending.values():
                if op is not None:
                    op.cancel()  # close retains/polls pending buffers

    def _terminate(self):
        if self._job is not None:
            try:
                self._win.job.TerminateJobObject(self._job, 1)
            except Exception:
                pass
        # Also owns a still-suspended process if assignment itself failed.
        if self._process is not None:
            try:
                self._win.api.TerminateProcess(self._process, 1)
            except Exception:
                pass

    async def write(self, data):
        self._check_thread()
        deadline = time.monotonic() + self.spec.limits.write
        attempted = False
        try:
            if not isinstance(data, str) or len(data) > self.spec.limits.frame_bytes:
                raise TransportError(FAILED)
            payload = data.encode("utf-8")
            if not payload or len(payload) > self.spec.limits.frame_bytes:
                raise TransportError(FAILED)
            async with asyncio.timeout(self.spec.limits.write):
                async with self._write_lock:
                    if not self.is_ready() or self._pipes[0] is None:
                        raise TransportError(FAILED)
                    self.write_attempts += 1
                    self.partial_write_possible = attempted = True
                    op = self._new_op(self._pipes[0], payload)
                    while not op.poll():
                        if time.monotonic() >= deadline or not self.is_ready():
                            raise TransportError(FAILED)
                        await asyncio.sleep(.002)
                    self.written_bytes += op.count
                    error, count = op.error, op.count
                    self._release(op)
                    if error or count != len(payload):
                        raise TransportError(FAILED)
        except BaseException as exc:
            # Even a partial frame is never replayed. This counter is evidence
            # of uncertainty, not proof of provider/model execution.
            self.partial_write_possible |= attempted
            await self._after_failure(exc)
            if not isinstance(exc, Exception):
                raise
            raise TransportError(FAILED) from None

    async def read_messages(self):
        self._check_thread()
        deadline = time.monotonic() + self.spec.limits.read
        try:
            while True:
                if self._error:
                    raise TransportError(FAILED)
                if self._control or self._ordinary:
                    yield (self._control if self._control else self._ordinary).popleft()
                    deadline = time.monotonic() + self.spec.limits.read
                elif self._eof or self._closing:
                    return
                elif time.monotonic() >= deadline:
                    raise TransportError(FAILED)
                else:
                    await asyncio.sleep(.002)
        except BaseException as exc:
            await self._after_failure(exc)
            if not isinstance(exc, Exception):
                raise
            raise TransportError(FAILED) from None

    async def end_input(self):
        self._check_thread()
        try:
            async with asyncio.timeout(self.spec.limits.write):
                async with self._write_lock:
                    if self._pipes and self._pipes[0] is not None:
                        self._pipes[0].Close()
                        self._pipes[0] = None
        except BaseException as exc:
            await self._after_failure(exc)
            if not isinstance(exc, Exception):
                raise
            raise TransportError(FAILED) from None

    async def close(self):
        try:
            await self._close_resources()
        except Exception:
            self.cleanup_complete = False
            raise TransportError(INCOMPLETE) from None
        if self._control_failure is not None:
            failure, self._control_failure = self._control_failure, None
            raise failure

    async def _close_resources(self):
        self._check_thread()
        # No SDK shield assumptions. Suppress task cancellation only while
        # finishing bounded owned cleanup, then re-raise the identical object.
        if self.cleanup_complete and self._job is None and not self._pipes:
            self._closing = True
            return
        cancelled = None
        deadline = time.monotonic() + self.spec.limits.cleanup
        while self._close_lock.locked():
            if time.monotonic() >= deadline:
                raise TransportError(INCOMPLETE)
            try:
                await asyncio.sleep(.002)
            except asyncio.CancelledError as exc:
                cancelled = exc
        async with self._close_lock:
            self._closing = True
            self._connected = False
            # This is an owned Job membership query, NOT tree enumeration for
            # containment. Keep handles to currently active members so process
            # termination signaling is observed as well as active Job count.
            if self._job is not None and not self._owned_wait_handles:
                try:
                    pids = self._win.job.QueryInformationJobObject(
                        self._job, self._win.job.JobObjectBasicProcessIdList)
                    for pid in pids:
                        try:
                            handle = self._win.handles.OpenProcess(0x100000 | 0x1000, False, pid)
                        except self._win.types.error:
                            continue  # process may already have exited
                        if self._win.job.IsProcessInJob(handle, self._job):
                            self._owned_wait_handles.append(handle)
                        else:
                            handle.Close()  # PID reused: never own the replacement
                except Exception:
                    # Active count below still must succeed; no fallback launch.
                    pass
            self._terminate()
            for op in self._ops:
                op.cancel()
            while True:
                pending = False
                for op in list(self._ops):
                    if op.poll():
                        self._release(op)
                    else:
                        pending = True
                pump_done = self._pump_task is None or self._pump_task.done()
                try:
                    active = self._win.active(self._job) if self._job is not None else 0
                    process_done = self._process is None or self._win.api.WaitForSingleObject(self._process, 0) == 0
                except Exception:
                    active, process_done = 1, False
                members_done = all(self._win.event.WaitForSingleObject(h, 0) == 0
                                   for h in self._owned_wait_handles)
                if not pending and pump_done and not active and process_done and members_done:
                    resources_done = True
                    break
                if time.monotonic() >= deadline:
                    self.cleanup_complete = False
                    resources_done = False
                    break
                try:
                    await asyncio.sleep(.002)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            if resources_done:
                if self._pump_task is not None:
                    # Retrieve any unexpected task failure; never leave it unobserved.
                    try:
                        self._pump_task.result()
                    except Exception:
                        self._error = True
                    except BaseException as exc:
                        self._control_failure = exc
                    self._pump_task = None
                self._close_handles(self._children)
                self._close_handles(self._pipes)
                self._close_handles(self._owned_wait_handles)
                for attr in ("_thread", "_process"):
                    handle = getattr(self, attr)
                    if handle is not None:
                        self._win.api.CloseHandle(handle)
                        setattr(self, attr, None)
                if self._job is not None:
                    self._job.Close()
                    self._job = None
                self._ordinary.clear()
                self._control.clear()
                self.cleanup_complete = True
        if cancelled is not None:
            raise cancelled
        if not self.cleanup_complete:
            raise TransportError(INCOMPLETE)
