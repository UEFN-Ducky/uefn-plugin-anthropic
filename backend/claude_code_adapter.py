"""Claude Code CLI adapter — subprocess + stream-json + session resume.

Every turn runs ``claude -p --output-format stream-json``. This plugin owns
``--resume``: it captures ``session_id`` from init/result events and passes
it back on later turns. Core only stores the id.
"""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Callable

from backend.agent.coding_agents.base import (
    CodingAgentCapabilities,
    CodingAgentInfo,
    CodingAgentLaunchResult,
)
from .claude_auth import is_claude_logged_in, resolve_claude_bin
from backend.agent.coding_agents.cli_shared import finalize_cli_turn, truncate_tool_result
from backend.agent.coding_agents.proc_exec import run_streaming_process
from backend.agent.coding_agents.settings_helpers import coding_agent_cfg

_CLAUDE_CODE_INSTALL_PS = "irm https://claude.ai/install.ps1 | iex"
_CLAUDE_CODE_PATH_HINT = r"%USERPROFILE%\.local\bin"

_PERMISSION_MODES = ("acceptEdits", "bypassPermissions", "default", "plan")


# ── Model menu ───────────────────────────────────────────────────────────────
# `claude --model` accepts family aliases (opus/sonnet/…) *or* concrete ids.
# Live list: /v1/models when an API key is set, else Anthropic's public
# deprecations table (no key). No hardcoded version pins.
_DEFAULT_CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_FAMILY_ORDER = ("opus", "sonnet", "haiku", "fable")
_MODELS_TTL_S = 3600.0
_CACHE_SOURCE = "live"

_models_lock = threading.Lock()
_models_cache: list[dict[str, str]] | None = None
_models_cache_at = 0.0
_models_refreshing = False


def _family_of(model_id: str) -> str:
    """First alphabetic name token of a Claude model id — its CLI alias.

    ``claude-opus-4-8`` → ``opus``; ``claude-3-5-sonnet-20241022`` → ``sonnet``;
    ``claude-fable-5`` → ``fable``. Returns "" for ids with no family word
    (e.g. ``claude-2-1``), which the caller drops.
    """
    for tok in (model_id or "").lower().split("-"):
        if tok.isalpha() and tok not in ("claude", "latest"):
            return tok
    return ""


def _order_families(families: set[str]) -> tuple[str, ...]:
    known = [f for f in _FAMILY_ORDER if f in families]
    extra = sorted(f for f in families if f not in _FAMILY_ORDER)
    return tuple(known + extra)


def _display_name_for(model_id: str, display_name: str = "") -> str:
    name = (display_name or "").strip()
    if name:
        return name
    mid = (model_id or "").strip()
    # claude-opus-4-8 → Claude Opus 4.8
    parts = mid.split("-")
    if len(parts) >= 3 and parts[0] == "claude":
        fam = parts[1].title()
        ver = parts[2:]
        if len(ver) >= 2 and ver[0].isdigit() and ver[1].isdigit() and len(ver[1]) <= 2:
            return f"Claude {fam} {ver[0]}.{ver[1]}"
        if ver and ver[0].isdigit():
            return f"Claude {fam} {ver[0]}"
        return f"Claude {fam}"
    return mid


def _is_chat_model_id(model_id: str) -> bool:
    """Drop non-chat Anthropic catalog rows (embeddings, etc.)."""
    mid = (model_id or "").lower()
    if not mid.startswith("claude"):
        return False
    if any(x in mid for x in ("embed", "moderat", "rerank", "tts", "whisper")):
        return False
    return bool(_family_of(mid))


def _row(model_id: str, display_name: str = "") -> dict[str, str]:
    mid = (model_id or "").strip()
    return {"id": mid, "name": _display_name_for(mid, display_name), "provider": "Claude Code"}


def _fetch_api_model_rows() -> list[dict[str, str]]:
    """Concrete rows from /v1/models when an Anthropic API key is set."""
    try:
        from backend.agent.secrets import get_key, has_key

        if not has_key("anthropic"):
            return []
        from backend.agent.model_fetch import fetch_models
        from .model_fetch import canonical_model_id

        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for m in fetch_models("anthropic", get_key("anthropic") or ""):
            mid = canonical_model_id(str(getattr(m, "id", "") or ""))
            if not mid or mid in seen or not _is_chat_model_id(mid):
                continue
            seen.add(mid)
            dn = str(getattr(m, "display_name", "") or getattr(m, "name", "") or "")
            rows.append(_row(mid, dn))
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows
    except Exception:
        return []


def _fetch_docs_model_rows() -> list[dict[str, str]]:
    """OAuth / no-key path — scrape Anthropic's public active-model table."""
    try:
        from .model_fetch import fetch_public_active_model_ids

        rows = [_row(mid) for mid in fetch_public_active_model_ids() if _is_chat_model_id(mid)]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows
    except Exception:
        return []


def _fetch_catalog_model_rows() -> list[dict[str, str]]:
    return _fetch_api_model_rows() or _fetch_docs_model_rows()


def _models_cache_path() -> Path | None:
    try:
        from frontend.settings import default_app_data_dir

        return default_app_data_dir() / "coding_agents" / "claude_code_models.json"
    except Exception:
        return None


def _read_models_disk_cache(*, allow_stale: bool = False) -> list[dict[str, str]] | None:
    path = _models_cache_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # Ignore pre-live caches that still unioned a hardcoded fallback list.
    if str(data.get("source") or "") != _CACHE_SOURCE:
        return None
    if not allow_stale and time.time() - float(data.get("fetched_at") or 0) > _MODELS_TTL_S:
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    rows = [
        {
            "id": str(m.get("id") or "").strip(),
            "name": str(m.get("name") or m.get("id") or "").strip(),
            "provider": "Claude Code",
        }
        for m in models
        if isinstance(m, dict) and str(m.get("id") or "").strip()
    ]
    return rows or None


def _write_models_disk_cache(models: list[dict[str, str]]) -> None:
    path = _models_cache_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"fetched_at": time.time(), "source": _CACHE_SOURCE, "models": models},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _refresh_models_async() -> None:
    """Refresh concrete model list off the hot detect() path (single flight)."""
    global _models_refreshing
    with _models_lock:
        if _models_refreshing:
            return
        _models_refreshing = True

    def _worker() -> None:
        global _models_cache, _models_cache_at, _models_refreshing
        rows = _fetch_catalog_model_rows()
        with _models_lock:
            if rows:
                _models_cache = rows
                _models_cache_at = time.time()
                _write_models_disk_cache(rows)
            _models_refreshing = False

    threading.Thread(target=_worker, name="claude-code-models", daemon=True).start()


def claude_code_families() -> tuple[str, ...]:
    """Family aliases for the '(latest)' shortcuts at the top of the menu."""
    rows = claude_code_specific_rows()
    families = {fam for r in rows if (fam := _family_of(r["id"]))}
    return _order_families(families) if families else _DEFAULT_CLAUDE_FAMILIES


def _family_alias_rows() -> list[dict[str, str]]:
    # ponytail: CLI family aliases only when live fetch+cache are empty. Not version pins.
    return [_row(fam, f"Claude {fam.title()}") for fam in _DEFAULT_CLAUDE_FAMILIES]


def claude_code_specific_rows() -> list[dict[str, str]]:
    """Concrete model ids for the picker (cached; never blocks detect())."""
    global _models_cache, _models_cache_at
    now = time.time()
    with _models_lock:
        cache = _models_cache
        fresh = cache is not None and (now - _models_cache_at) < _MODELS_TTL_S
    if not fresh:
        disk = _read_models_disk_cache(allow_stale=False)
        if disk:
            with _models_lock:
                _models_cache = disk
                _models_cache_at = now
            cache = disk
        else:
            stale = _read_models_disk_cache(allow_stale=True)
            if stale and cache is None:
                with _models_lock:
                    _models_cache = stale
                cache = stale
        _refresh_models_async()
    return list(cache) if cache else _family_alias_rows()


def claude_code_model_rows() -> list[dict[str, str]]:
    """Picker rows: concrete model ids only (no family '(latest)' shortcuts)."""
    return claude_code_specific_rows()


# #region agent log
def _dbg_tool_pairing(message: str, data: dict[str, Any]) -> None:
    try:
        with open(r"C:\Users\tas13\Documents\GitHub\UEFN-Ducky\debug-77e3f2.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "sessionId": "77e3f2", "runId": "chat-lag-repro", "hypothesisId": "L-C",
                "location": "backend/agent/coding_agents/claude_code.py",
                "message": message, "data": data, "timestamp": int(time.time() * 1000),
            }) + "\n")
    except Exception:
        pass
# #endregion


def _claude_desktop_installed() -> bool:
    """True when the Claude Desktop chat app is present (not the same as Claude Code CLI)."""
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return False
    return (Path(appdata) / "Claude" / "claude_desktop_config.json").is_file()


def _claude_code_missing_status() -> str:
    if _claude_desktop_installed():
        return (
            "Claude Desktop is installed, but Ducky needs the separate Claude Code CLI "
            f"(`claude` in PowerShell). Install: {_CLAUDE_CODE_INSTALL_PS} — add "
            f"{_CLAUDE_CODE_PATH_HINT} to your user PATH, restart terminals and Ducky."
        )
    return (
        "Claude Code CLI not found — needs the `claude` terminal command (not Claude Desktop). "
        f"Install in Windows PowerShell: {_CLAUDE_CODE_INSTALL_PS}"
    )


def build_claude_argv(
    *,
    binary: str,
    prompt: str,
    system_prompt: str,
    model: str,
    mcp_config_path: str,
    extra_args: str,
    session_id: str,
    permission_mode: str,
    image_dirs: list[str] | None = None,
    system_prompt_file: str = "",
    prompt_via_stdin: bool = False,
) -> list[str]:
    """Argv for one streaming turn (unit-testable, no side effects).

    Prefer ``system_prompt_file`` + ``prompt_via_stdin=True`` so long pastes never
    hit Windows CreateProcess WinError 206 (command line / env too long).
    """
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if mcp_config_path:
        argv.extend(["--mcp-config", mcp_config_path, "--strict-mcp-config"])
        # The injected `uefn` bridge is pre-approved: the user picked this agent.
        argv.extend(["--allowedTools", "mcp__uefn"])
    mode = permission_mode if permission_mode in _PERMISSION_MODES else "acceptEdits"
    argv.extend(["--permission-mode", mode])
    # Claude Code has no --image flag; grant read access to the attachment dirs so
    # its Read tool can open the uploaded images referenced in the prompt.
    for directory in image_dirs or []:
        argv.extend(["--add-dir", directory])
    if session_id:
        argv.extend(["--resume", session_id])
    # The CLI rebuilds its system prompt per run — --resume restores only the
    # message history. Append on EVERY turn, or the persona/skills/no-batching
    # rules silently vanish from turn 2 onward.
    sys_file = (system_prompt_file or "").strip()
    if sys_file:
        argv.extend(["--append-system-prompt-file", sys_file])
    elif system_prompt.strip():
        argv.extend(["--append-system-prompt", system_prompt])
    if model and model not in ("", "default"):
        argv.extend(["--model", model])
    else:
        raise ValueError("Claude Code requires a model alias or id (e.g. sonnet/opus/haiku/fable)")
    extra = (extra_args or "").strip()
    if extra:
        argv.extend(shlex.split(extra, posix=False))
    # Long user prompts must ride stdin (prompt_via_stdin), not argv.
    if not prompt_via_stdin:
        argv.append(prompt)
    return argv


def _image_prompt_suffix(image_paths: list[str]) -> str:
    if not image_paths:
        return ""
    listed = "\n".join(f"- {p}" for p in image_paths)
    return (
        "\n\nThe user attached image file(s) with this message. View them by reading these "
        f"absolute paths with your Read tool (it renders images):\n{listed}"
    )


def _tool_result_to_text(content: Any) -> str:
    """Flatten a tool_result content payload (string or text blocks) for the UI."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        text = "\n".join(parts)
    else:
        text = ""
    return truncate_tool_result(text)


class _StreamState:
    """Accumulates one stream-json turn: deltas, tools, session id, result.

    Tool events follow the panel's pairing contract: a ``tool`` event opens a
    running chip and the VERY NEXT ``tool_done`` event closes it (the UI pairs
    them positionally). Claude can batch several tool_use blocks in one turn,
    so only one chip is shown at a time; the rest queue until theirs resolves.
    """

    def __init__(self, conv_id: str, run_id: str, push: Callable[[dict[str, Any]], None]) -> None:
        self.conv_id = conv_id
        self.run_id = run_id
        self.push = push
        self.session_id = ""
        self.streamed_text: list[str] = []
        self.final_text = ""
        self.is_error = False
        self.error_text = ""
        self.saw_json = False
        self.model = ""
        self.usage: dict[str, Any] = {}
        # Last assistant-step context window (input+cache). result.usage is
        # cumulative across num_turns — do not use that sum as "current context".
        self._last_step_context_tokens = 0
        self._seen_usage_ids: set[str] = set()
        # Ordered thinking/text/tool_call blocks in the embedded agent's persisted
        # format, so the turn's steps survive a panel reload (not just the reply).
        self.blocks: list[dict[str, Any]] = []
        self._seg_text: list[str] = []
        self._seg_thinking: list[str] = []
        self._saw_delta = False
        # Pending tool bookkeeping for the pairing contract.
        self._tools: dict[str, dict[str, Any]] = {}
        self._shown_tool: str | None = None
        self._tool_queue: list[str] = []
        # Delta coalescing: every push becomes a GUI-thread evaluate_js call, and
        # --include-partial-messages emits deltas every few tokens — pushing each
        # one saturates the panel's message pump and freezes clicks. Buffer and
        # flush in chunks instead.
        self._delta_buf: list[str] = []
        self._delta_kind: str = "text_delta"
        self._delta_last_flush = 0.0

    def stdout_empty(self) -> bool:
        return not self.saw_json

    # ── delta coalescing ────────────────────────────────────────────────

    _FLUSH_CHARS = 400
    _FLUSH_SECS = 0.12

    def _queue_delta(self, kind: str, text: str) -> None:
        if kind != self._delta_kind:
            self.flush_stream()
            self._delta_kind = kind
        self._delta_buf.append(text)
        now = time.monotonic()
        if (
            sum(len(t) for t in self._delta_buf) >= self._FLUSH_CHARS
            or now - self._delta_last_flush >= self._FLUSH_SECS
        ):
            self.flush_stream()

    def flush_stream(self) -> None:
        """Push buffered text/thinking as one event (ordering-safe before tools)."""
        if not self._delta_buf:
            self._delta_last_flush = time.monotonic()
            return
        text = "".join(self._delta_buf)
        self._delta_buf = []
        self._delta_last_flush = time.monotonic()
        self._emit({"type": self._delta_kind, "text": text})

    # ── persisted blocks (embedded-agent format) ────────────────────────

    def _flush_segments(self) -> None:
        """Move buffered reasoning/narration into blocks (a tool call follows)."""
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
        self._seg_thinking = []
        text = "".join(self._seg_text).strip()
        if text:
            self.blocks.append({"type": "text", "text": text})
        self._seg_text = []

    def trailing_text(self) -> str:
        """Narration after the last tool call — the turn's final answer text."""
        return "".join(self._seg_text).strip()

    def finalize_blocks(self) -> list[dict[str, Any]]:
        """Blocks to persist. Trailing thinking joins them; trailing text stays
        out — it becomes the message content, like the embedded final step."""
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
            self._seg_thinking = []
        return self.blocks

    # ── tool chip pairing ───────────────────────────────────────────────

    def _push_tool_start(self, tool_id: str) -> None:
        self.flush_stream()
        info = self._tools.get(tool_id) or {}
        name = str(info.get("name") or "tool")
        self._emit(
            {
                "type": "tool",
                "text": f"⚙ {name}",
                "tool": {"name": name, "arguments": info.get("arguments") or {}, "status": "pending"},
            }
        )
        self._shown_tool = tool_id

    def _push_tool_done(self, tool_id: str, *, failed: bool, result_text: str) -> None:
        self.flush_stream()
        info = self._tools.pop(tool_id, None) or {}
        name = str(info.get("name") or "tool")
        args = info.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        started = float(info.get("started_at") or 0.0)
        ms = int((time.monotonic() - started) * 1000) if started else 0
        status = "error" if failed else "success"
        tool_payload: dict[str, Any] = {
            "name": name,
            "arguments": args,
            "status": status,
            "durationMs": ms,
            "result": result_text,
            "hint": "",
        }
        if not failed:
            try:
                from frontend.ui_web.verse_editor.agent_sync import file_edit_meta_for_stream

                file_edit = file_edit_meta_for_stream(name, args, result_text)
                if file_edit:
                    tool_payload["fileEdit"] = file_edit
            except Exception:
                pass
        self.blocks.append(
            {
                "type": "tool_call",
                "id": tool_id,
                "name": name,
                "arguments": args,
                "started": float(info.get("started_wall") or 0.0),
                "duration_ms": ms,
                "result": {"ok": not failed, "data": result_text, "hint": ""},
                "status": status,
                **({"file_edit": tool_payload["fileEdit"]} if "fileEdit" in tool_payload else {}),
            }
        )
        self._emit(
            {
                "type": "tool_done",
                "text": f"⚙ {name} · {status}" + (f" · {ms}ms" if ms else ""),
                "success": not failed,
                "tool": tool_payload,
            }
        )

    def _resolve_tool(self, tool_id: str, *, failed: bool, result_text: str) -> None:
        if tool_id == self._shown_tool:
            self._push_tool_done(tool_id, failed=failed, result_text=result_text)
            self._shown_tool = None
            if self._tool_queue:
                self._push_tool_start(self._tool_queue.pop(0))
            return
        if tool_id in self._tool_queue:
            # Resolved out of order: show its intent right before its result so
            # the positional pairing still lines up.
            self._tool_queue.remove(tool_id)
            self._push_tool_start(tool_id)
            self._shown_tool = None
            self._push_tool_done(tool_id, failed=failed, result_text=result_text)
            if self._tool_queue and self._shown_tool is None:
                self._push_tool_start(self._tool_queue.pop(0))

    def finish_unresolved_tools(self, *, cancelled: bool) -> None:
        """Turn ended with chips still open — close them so nothing spins forever."""
        note = "Cancelled before the tool finished." if cancelled else "Turn ended before the tool reported a result."
        leftovers = ([self._shown_tool] if self._shown_tool else []) + list(self._tool_queue)
        # #region agent log
        if leftovers:
            now = time.monotonic()
            _dbg_tool_pairing("claude turn ended with unresolved tools", {
                "convId": self.conv_id, "cancelled": cancelled, "leftoverCount": len(leftovers),
                "trackedCount": len(self._tools),
                "tools": [
                    {
                        "id": str(tool_id)[:80],
                        "name": str((self._tools.get(tool_id) or {}).get("name") or "tool"),
                        "ageMs": int((now - float((self._tools.get(tool_id) or {}).get("started_at") or now)) * 1000),
                    }
                    for tool_id in leftovers[:30] if tool_id is not None
                ],
            })
        # #endregion
        self._tool_queue = []
        for tool_id in leftovers:
            if tool_id is None or tool_id not in self._tools:
                continue
            if tool_id != self._shown_tool:
                self._push_tool_start(tool_id)
            self._push_tool_done(tool_id, failed=True, result_text=note)
            self._shown_tool = None

    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("conv_id", self.conv_id)
        if self.run_id:
            event.setdefault("run_id", self.run_id)
        self.push(event)

    def on_line(self, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        self.saw_json = True
        sid = str(data.get("session_id") or "")
        if sid:
            self.session_id = sid
        model = str(data.get("model") or "")
        if model:
            self.model = model
        kind = str(data.get("type") or "")
        if kind == "system":
            subtype = str(data.get("subtype") or "")
            if subtype == "init":
                model = str(data.get("model") or self.model or "").strip()
                text = f"Claude Code ready · {model}" if model else "Claude Code ready…"
                self._emit({"type": "status", "text": text})
            return
        if kind == "stream_event":
            self._on_stream_event(data.get("event") or {})
        elif kind == "assistant":
            self._on_assistant(data.get("message") or {})
        elif kind == "user":
            self._on_tool_results(data.get("message") or {})
        elif kind == "result":
            self._on_result(data)

    def _on_stream_event(self, event: dict[str, Any]) -> None:
        if str(event.get("type") or "") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        dtype = str(delta.get("type") or "")
        if dtype == "text_delta":
            text = str(delta.get("text") or "")
            if text:
                self._saw_delta = True
                self.streamed_text.append(text)
                self._seg_text.append(text)
                self._queue_delta("text_delta", text)
        elif dtype == "thinking_delta":
            text = str(delta.get("thinking") or "")
            if text:
                self._saw_delta = True
                self._seg_thinking.append(text)
                self._queue_delta("thinking", text)

    def _on_assistant(self, message: dict[str, Any]) -> None:
        model = str(message.get("model") or "")
        if model:
            self.model = model
        self._ingest_step_usage(message)
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and not self._saw_delta:
                # Old CLI without --include-partial-messages: no deltas arrive,
                # so capture narration from the full message for the blocks.
                txt = str(block.get("text") or "")
                if txt:
                    self._seg_text.append(txt)
            elif btype == "thinking" and not self._saw_delta:
                txt = str(block.get("thinking") or "")
                if txt:
                    self._seg_thinking.append(txt)
            elif btype == "tool_use":
                # This message's narration precedes its tool calls in the stream.
                self._flush_segments()
                tid = str(block.get("id") or "") or f"tool-{len(self._tools)}"
                self._tools[tid] = {
                    "name": str(block.get("name") or "tool"),
                    "arguments": block.get("input") if isinstance(block.get("input"), dict) else {},
                    "started_at": time.monotonic(),
                    "started_wall": time.time(),
                }
                if self._shown_tool is None:
                    self._push_tool_start(tid)
                else:
                    self._tool_queue.append(tid)

    def _on_tool_results(self, message: dict[str, Any]) -> None:
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = str(block.get("tool_use_id") or "")
            if not tid or tid not in self._tools:
                # #region agent log
                _dbg_tool_pairing("claude tool result did not match a tracked tool", {
                    "convId": self.conv_id, "toolId": tid[:80], "hasId": bool(tid),
                    "trackedCount": len(self._tools), "queuedCount": len(self._tool_queue),
                    "shownTool": str(self._shown_tool or "")[:80],
                })
                # #endregion
                continue
            self._resolve_tool(
                tid,
                failed=bool(block.get("is_error")),
                result_text=_tool_result_to_text(block.get("content")),
            )

    @staticmethod
    def _step_window_tokens(usage: dict[str, Any]) -> int:
        """Context window for one API step (fresh input + both cache tiers)."""
        inp = int(usage.get("input_tokens") or 0)
        cache_read = int(
            usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0
        )
        cache_write = int(
            usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens") or 0
        )
        return max(0, inp + cache_read + cache_write)

    def _ingest_step_usage(self, message: dict[str, Any]) -> None:
        """Track last per-step window; skip duplicate ids (parallel tool batches)."""
        usage = message.get("usage")
        if not isinstance(usage, dict) or not usage:
            return
        msg_id = str(message.get("id") or "").strip()
        if msg_id:
            if msg_id in self._seen_usage_ids:
                return
            self._seen_usage_ids.add(msg_id)
        window = self._step_window_tokens(usage)
        if window > 0:
            self._last_step_context_tokens = window

    @staticmethod
    def _context_limit_from_result(data: dict[str, Any]) -> int | None:
        """Pull contextWindow from result.modelUsage / model_usage when present."""
        model_usage = data.get("modelUsage")
        if not isinstance(model_usage, dict):
            model_usage = data.get("model_usage")
        if not isinstance(model_usage, dict) or not model_usage:
            return None
        best = 0
        for entry in model_usage.values():
            if not isinstance(entry, dict):
                continue
            n = int(entry.get("contextWindow") or entry.get("context_window") or 0)
            if n > best:
                best = n
        return best or None

    def _on_result(self, data: dict[str, Any]) -> None:
        self.flush_stream()
        subtype = str(data.get("subtype") or "")
        self.is_error = bool(data.get("is_error")) or subtype.startswith("error")
        text = data.get("result")
        if isinstance(text, str) and text.strip():
            self.final_text = text.strip()
        if self.is_error and not self.error_text:
            self.error_text = self.final_text or subtype or "Claude Code turn failed"
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        inp = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cost = data.get("total_cost_usd")
        # Billing totals stay cumulative from result.usage; context window is the
        # last assistant step (falls back to an anti-double-count estimate).
        if self._last_step_context_tokens > 0:
            context_tokens = self._last_step_context_tokens
        else:
            from frontend.ui_web.token_usage import estimate_context_window_tokens

            context_tokens = estimate_context_window_tokens(
                inp,
                cache_read,
                cache_write,
                num_turns=int(data.get("num_turns") or 0),
            )
        payload: dict[str, Any] = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "context_tokens": context_tokens,
            "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
            "num_turns": int(data.get("num_turns") or 0),
            "model": self.model,
        }
        limit = self._context_limit_from_result(data)
        if limit is not None:
            payload["context_limit"] = limit
        self.usage = payload


class ClaudeCodeAdapter:
    id = "claude_code"
    label = "Claude Code"
    capabilities = CodingAgentCapabilities(
        terminal_agent=True,
        chat_api=False,
        a2a=True,
        mcp_inject=True,
        needs_api_key=False,
        needs_cli=True,
        resume=True,
    )

    def detect(self, settings: Any) -> CodingAgentInfo:
        cfg = coding_agent_cfg(settings, self.id)
        enabled = bool(cfg.get("enabled", True))
        override = str(cfg.get("cli_path") or "")
        path = resolve_claude_bin(override)
        default_args = str(cfg.get("default_args") or "")
        if path:
            logged_in = is_claude_logged_in(override or path)
            status = f"Found: {path}" + (" · logged in" if logged_in else " · not logged in (chat will prompt)")
            available = enabled
        else:
            status = _claude_code_missing_status()
            available = False
        return CodingAgentInfo(
            id=self.id,
            label=self.label,
            enabled=enabled,
            available=available,
            status=status,
            cli_path=path or override,
            default_args=default_args,
            capabilities=self.capabilities,
            models=claude_code_model_rows(),
        )

    def launch(
        self,
        *,
        prompt: str,
        system_prompt: str,
        cwd: str,
        conv_id: str,
        model: str,
        mcp_config_path: str,
        extra_args: str,
        cli_path: str,
        env: dict[str, str],
        push: Any,
        session_id: str = "",
        run_id: str = "",
        cancel: threading.Event | None = None,
        timeout_s: float = 0.0,
        image_paths: list[str] | None = None,
    ) -> CodingAgentLaunchResult:
        model_id = (model or "").strip()
        if not model_id or model_id.lower() == "default":
            return CodingAgentLaunchResult(
                ok=False,
                error=(
                    "No Claude Code model selected. Pick a model (e.g. sonnet, opus, "
                    "haiku, or fable) for this chat or Ducky profile."
                ),
                status="error",
            )
        binary = resolve_claude_bin(cli_path) or "claude"
        from frontend.settings import PanelSettings

        cfg = coding_agent_cfg(PanelSettings.load(), self.id)
        images = list(image_paths or [])
        full_prompt = prompt + _image_prompt_suffix(images)
        image_dirs = sorted({str(Path(p).resolve().parent) for p in images})

        # System prompt + user paste must NOT go on Windows argv/env (WinError 206).
        from backend.agent.coding_agents.mcp_inject import write_prompt_file

        sys_path = (
            write_prompt_file(system_prompt, conv_id=f"{conv_id}-sys")
            if (system_prompt or "").strip()
            else None
        )
        try:
            argv = build_claude_argv(
                binary=binary,
                prompt="",
                system_prompt="",
                system_prompt_file=str(sys_path) if sys_path else "",
                prompt_via_stdin=True,
                model=model,
                mcp_config_path=mcp_config_path,
                extra_args=extra_args,
                session_id=session_id,
                permission_mode=str(cfg.get("permission_mode") or "acceptEdits"),
                image_dirs=image_dirs,
            )
            state = _StreamState(conv_id, run_id, push)
            if session_id:
                push(
                    {
                        "type": "status",
                        "text": "Resumed Claude Code session…",
                        "conv_id": conv_id,
                        "run_id": run_id,
                    }
                )
            else:
                push(
                    {
                        "type": "status",
                        "text": "Starting Claude Code…",
                        "conv_id": conv_id,
                        "run_id": run_id,
                    }
                )
            proc = run_streaming_process(
                argv=argv,
                cwd=cwd,
                env_extra=env,
                conv_id=conv_id,
                on_line=state.on_line,
                timeout_s=timeout_s,
                cancel=cancel,
                stdin_data=full_prompt,
            )
            err_low = (proc.stderr_tail or "").lower()
            if (
                proc.returncode != 0
                and state.stdout_empty()
                and "unknown option" in err_low
                and "append-system-prompt-file" in err_low
                and sys_path is not None
            ):
                # Older CLI: no --append-system-prompt-file — fold system text into stdin.
                argv = build_claude_argv(
                    binary=binary,
                    prompt="",
                    system_prompt="",
                    prompt_via_stdin=True,
                    model=model,
                    mcp_config_path=mcp_config_path,
                    extra_args=extra_args,
                    session_id=session_id,
                    permission_mode=str(cfg.get("permission_mode") or "acceptEdits"),
                    image_dirs=image_dirs,
                )
                state = _StreamState(conv_id, run_id, push)
                stdin_merged = (
                    f"<ducky-system-prompt>\n{system_prompt.strip()}\n"
                    f"</ducky-system-prompt>\n\n{full_prompt}"
                )
                proc = run_streaming_process(
                    argv=argv,
                    cwd=cwd,
                    env_extra=env,
                    conv_id=conv_id,
                    on_line=state.on_line,
                    timeout_s=timeout_s,
                    cancel=cancel,
                    stdin_data=stdin_merged,
                )
                err_low = (proc.stderr_tail or "").lower()
            if (
                proc.returncode != 0
                and state.stdout_empty()
                and "--include-partial-messages" in argv
                and "unknown option" in err_low
            ):
                # Older CLI without partial-message streaming — retry without it.
                argv = [a for a in argv if a != "--include-partial-messages"]
                state = _StreamState(conv_id, run_id, push)
                proc = run_streaming_process(
                    argv=argv,
                    cwd=cwd,
                    env_extra=env,
                    conv_id=conv_id,
                    on_line=state.on_line,
                    timeout_s=timeout_s,
                    cancel=cancel,
                    stdin_data=full_prompt,
                )

            # Never leave a chip spinning: whatever ended this turn, resolve leftovers
            # and push any still-buffered stream text.
            state.flush_stream()
            state.finish_unresolved_tools(cancelled=proc.cancelled)
            blocks = state.finalize_blocks()

            streamed = "".join(state.streamed_text).strip()
            # Text before tool calls lives in blocks; only the trailing segment is
            # the final answer. Falling back to ALL streamed text would duplicate it.
            reply = state.final_text or state.trailing_text() or ("" if blocks else streamed)
            new_session = state.session_id or session_id

            # A stale --resume id makes the CLI exit with "No conversation found".
            return finalize_cli_turn(
                proc=proc,
                reply=reply,
                streamed=bool(streamed) or bool(blocks),
                blocks=blocks,
                session_id=session_id,
                new_session=new_session,
                usage=state.usage,
                agent_label="Claude Code",
                timeout_s=timeout_s,
                error_text=state.error_text,
                stale_session_markers=("no conversation found",),
            )
        finally:
            if sys_path is not None:
                try:
                    sys_path.unlink(missing_ok=True)
                except OSError:
                    pass
