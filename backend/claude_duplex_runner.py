"""Unintegrated, public-SDK pre-prompt gate; no adapter or app imports.

Pinned contract: claude-agent-sdk==0.2.165, published on PyPI 2026-10-08.
https://github.com/anthropics/claude-agent-sdk-python/tree/v0.2.165
See src/claude_agent_sdk/client.py (connect, get_mcp_status, query,
receive_response, disconnect) and types.py (McpServerStatus/ClaudeAgentOptions).

Callers construct public ClaudeAgentOptions: preserve model/system prompt,
resume, cwd/add_dirs, env, settings_sources, strict_mcp_config, hooks and
permission callbacks/rules. This module never changes those options. Catalog
presence does not certify effective managed/permission policy or future health.
Failed ResultMessage objects never reach callbacks; failures use TurnResult's
fixed error. Successful answer/tool messages remain available for translation.
The public SDK has no output-drained barrier or per-query result correlation.
Receiver scheduling is NOT wire ownership: even with a running receiver an
unsolicited result delayed until after query is indistinguishable. Integration
requires an ordered transport boundary; this module alone cannot certify it.
The default production route therefore refuses before constructing a client.
Explicit binding injection exercises test doubles only; it is not an integration
override or evidence that a real SDK session has established response ownership.

Deadlines are cooperative AnyIO deadlines. The pinned SDK's shielded close
(subprocess_cli.py:962-1057) can delay cancellation ~20 seconds and only reaps
its direct child. No public API certifies Windows descendant cleanup. A returned
cleanup_complete means disconnect returned, NOT that a process tree was reaped.
No background thread, private protocol, reconnect or task replay is used here.
Packaging and adapter integration are deliberately separate work.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import import_module, metadata
import math
from typing import Any, Callable

SDK_VERSION = "0.2.165"
# backend/tools/panel/ducky_panel.py and permission_prompt.py define these.
DISCOVERY_TOOLS = frozenset({"ducky_get_tools", "ducky_call_tool"})
PERMISSION_TOOL = "ducky_permission_prompt"
UNAVAILABLE = (
    "Ducky tools unavailable: this integration requires claude-agent-sdk==0.2.165. "
    "Ask the Ducky maintainer to package the supported SDK, then retry."
)
READINESS_ERROR = "Ducky tools unavailable: check the required Ducky MCP connection and discovery tools, then retry."
TIMEOUT_ERROR = "Ducky tools unavailable: startup timed out. Check the Ducky MCP connection, then retry."
TURN_ERROR = "Claude turn failed after submission. Check the chat before retrying; the task may have run."
CLEANUP_ERROR = "Claude session cleanup was not confirmed. Check the running session before retrying."
ORDERING_UNAVAILABLE = (
    "Ducky tools unavailable: Claude response ownership cannot be established. "
    "Ask the Ducky maintainer for the supported session integration before retrying."
)


@dataclass(frozen=True)
class TurnResult:
    status: str
    upstream_session_id: str
    error: str = ""
    submitted: bool = False
    cleanup_complete: bool = True


@dataclass(frozen=True)
class SDKBinding:
    """Test-double seam, not a production response-ownership certificate."""
    version: str
    client_factory: Callable[..., Any]
    result_type: type


def _load_sdk() -> SDKBinding:
    if metadata.version("claude-agent-sdk") != SDK_VERSION:
        raise ValueError("Unsupported SDK")
    sdk = import_module("claude_agent_sdk")
    return SDKBinding(SDK_VERSION, sdk.ClaudeSDKClient, sdk.ResultMessage)


def _catalog_ready(response: Any, required: frozenset[str]) -> bool:
    """Pending returns False; malformed/terminal/missing catalog fails closed."""
    if not isinstance(response, dict) or not isinstance(response.get("mcpServers"), list):
        raise ValueError
    servers = response["mcpServers"]
    if not all(isinstance(server, dict) for server in servers):
        raise ValueError
    matches = [server for server in servers if server.get("name") == "uefn"]
    if len(matches) != 1:
        raise ValueError
    server = matches[0]
    if server.get("status") == "pending":
        return False
    if server.get("status") != "connected" or not isinstance(server.get("tools"), list):
        raise ValueError
    names = set()
    for tool in server["tools"]:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ValueError
        names.add(tool["name"])
    # Accept only exact server-local names or this exact server's CLI prefix.
    # Never strip arbitrary mcp/server prefixes or use substring matching.
    if not all(name in names or "mcp__uefn__" + name in names for name in required):
        raise ValueError
    return True


async def run_turn_async(
    *, options: Any, prompt: str, upstream_session_id: str = "",
    cancel: Any = None, on_message: Callable[..., Any] | None = None,
    startup_timeout: float = 30, turn_timeout: float = 600,
    cleanup_timeout: float = 25, poll_interval: float = 0.05,
    require_permission_tool: bool = True, binding: SDKBinding | None = None,
) -> TurnResult:
    """Default route fails closed; injected test doubles exercise the turn protocol.

    on_message is an async, cancellation-cooperative consumer. Exceptions and
    timeouts after query begins return submitted=True: never automatically retry.
    Threading.Event cancellation returns cancelled; task/control-flow cancellation
    propagates after cleanup. Use this entry point when already in an event loop.
    """
    result = TurnResult("error", upstream_session_id, READINESS_ERROR)
    if cancel is not None and cancel.is_set():
        return replace(result, status="cancelled", error="Cancelled")
    try:
        import anyio
        sdk = binding if binding is not None else _load_sdk()
        if sdk.version != SDK_VERSION:
            return replace(result, error=UNAVAILABLE)
    except Exception:
        return replace(result, error=UNAVAILABLE)
    if binding is None:
        # Pinned public APIs lack an ordered drain/correlation contract. Do not
        # turn scheduling observations or a caller Boolean into certification.
        # Keep the test seam isolated until a supported integration exists.
        return replace(result, error=ORDERING_UNAVAILABLE)
    if not all(math.isfinite(value) and value > 0 for value in
               (startup_timeout, turn_timeout, cleanup_timeout, poll_interval)):
        return result
    # Resume is a process option, not query's wire-session routing parameter.
    if (getattr(options, "resume", None) or "") != upstream_session_id:
        return result
    required = DISCOVERY_TOOLS | ({PERMISSION_TOOL} if require_permission_tool else set())
    client = None
    submitted = False
    cancelled = False
    control_error = None
    try:
        client = sdk.client_factory(options=options)
        with anyio.CancelScope() as operation:
            async with anyio.create_task_group() as tasks:
                async def watch_cancel():
                    nonlocal cancelled
                    while True:
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            operation.cancel()
                            return
                        await anyio.sleep(poll_interval)

                tasks.start_soon(watch_cancel)
                try:
                    # SDK connect starts its own reader/control handlers. Public
                    # receive_response drains messages during status and query.
                    response_done = anyio.Event()
                    response_ok = False
                    response_session = upstream_session_id
                    response_exception = None
                    receiver_started = False

                    async def receive():
                        nonlocal response_ok, response_session, response_exception, receiver_started
                        receiver_started = True
                        try:
                            async for message in client.receive_response():
                                if not submitted:
                                    # Don't forward unsolicited startup/config data.
                                    if isinstance(message, sdk.result_type):
                                        return
                                    continue
                                if isinstance(message, sdk.result_type):
                                    response_ok = not message.is_error
                                    if not response_ok:
                                        # Vendor failure fields may contain private
                                        # diagnostics. Classify before any callback.
                                        return
                                    if response_ok and isinstance(message.session_id, str):
                                        response_session = message.session_id
                                if on_message is not None:
                                    await on_message(message)
                        except Exception:
                            response_ok = False
                        except BaseException as exc:
                            # Relay control-flow exceptions from the consumer task
                            # to the owner rather than converting them into errors.
                            response_exception = exc
                        finally:
                            response_done.set()

                    # One monotonic budget includes connect and every status poll.
                    startup_deadline = anyio.current_time() + startup_timeout
                    with anyio.fail_after(startup_timeout):
                        await client.connect(None)
                        tasks.start_soon(receive)
                        while not _catalog_ready(await client.get_mcp_status(), required):
                            if response_done.is_set():
                                if response_exception is not None:
                                    raise response_exception
                                raise ValueError
                            await anyio.sleep(poll_interval)
                        # Fail closed if a synchronous status response outran
                        # receiver startup. Do not sleep/yield to manufacture a
                        # purported SDK drain barrier; none exists publicly.
                        if not receiver_started or response_done.is_set():
                            if response_exception is not None:
                                raise response_exception
                            raise ValueError
                    # Vendor shields can defer cancellation past fail_after.
                    # Even when they eventually return, never submit late work.
                    if anyio.current_time() >= startup_deadline:
                        raise TimeoutError
                    with anyio.fail_after(turn_timeout):
                        # Check at the last possible point before the only user frame.
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                        else:
                            submitted = True
                            await client.query(prompt)
                            await response_done.wait()
                            if response_exception is not None:
                                raise response_exception
                            if response_ok:
                                result = TurnResult("success", response_session, submitted=True)
                            else:
                                result = replace(result, error=TURN_ERROR, submitted=True)
                except TimeoutError:
                    result = replace(result, error=TURN_ERROR if submitted else TIMEOUT_ERROR)
                except Exception:
                    result = replace(result, error=TURN_ERROR if submitted else READINESS_ERROR)
                finally:
                    tasks.cancel_scope.cancel()
        if cancelled or (cancel is not None and cancel.is_set()):
            result = replace(result, status="cancelled", error="Cancelled",
                             upstream_session_id=upstream_session_id)
    except Exception:
        result = replace(result, error=TURN_ERROR if submitted else READINESS_ERROR)
    except BaseException as exc:
        # AnyIO wraps a lone control-flow failure on task-group exit. Unwrap
        # only singleton groups; preserve genuinely concurrent failures.
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        control_error = exc
    finally:
        if client is not None:
            complete = False
            try:
                cleanup_deadline = anyio.current_time() + cleanup_timeout
                with anyio.move_on_after(cleanup_timeout, shield=True) as cleanup:
                    await client.disconnect()
                    complete = not cleanup.cancel_called and anyio.current_time() < cleanup_deadline
            except BaseException as exc:
                if control_error is not None:
                    control_error = BaseExceptionGroup("Turn and cleanup failed", [control_error, exc])
                elif not isinstance(exc, Exception):
                    control_error = exc
            if not complete:
                result = replace(result, status="error", error=CLEANUP_ERROR,
                                 upstream_session_id=upstream_session_id, cleanup_complete=False)
    if control_error is not None:
        raise control_error
    if result.cleanup_complete and cancel is not None and cancel.is_set():
        result = replace(result, status="cancelled", error="Cancelled",
                         upstream_session_id=upstream_session_id)
    return replace(result, submitted=submitted)


def run_turn(**kwargs: Any) -> TurnResult:
    """Synchronous bridge; refuses active event loops, never starts a worker thread."""
    import functools
    try:
        import anyio
        return anyio.run(functools.partial(run_turn_async, **kwargs))
    except RuntimeError:
        return TurnResult("error", kwargs.get("upstream_session_id", ""),
                          "Ducky tools unavailable: use the asynchronous runner from an active event loop.")
    except ImportError:
        return TurnResult("error", kwargs.get("upstream_session_id", ""), UNAVAILABLE)
