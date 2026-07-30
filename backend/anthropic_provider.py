"""Anthropic Messages API provider."""

from __future__ import annotations

from typing import Any, AsyncIterator

from backend.agent.multimodal_content import build_anthropic_user_content
from backend.agent.prompt_cache import PromptCachePayload
from backend.agent.providers.base import (
    ProviderMessage,
    StreamEvent,
    StreamEventKind,
    ToolCallRequest,
)
from backend.agent.providers.cache_utils import (
    anthropic_messages_with_cache,
    anthropic_system_blocks,
    anthropic_tools_with_cache,
    parse_anthropic_usage,
)
from backend.agent.thinking_effort import EFFORT_BUDGET, normalize_thinking_effort

# Back-compat alias for callers that still expect the private name.
_EFFORT_BUDGET = EFFORT_BUDGET


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, *, thinking_effort: str = "off") -> None:
        self._api_key = api_key
        self._model = model
        self._thinking_effort = normalize_thinking_effort(thinking_effort)

    def _client(self):
        import anthropic

        return anthropic.Anthropic(api_key=self._api_key)

    def _to_anthropic_messages(self, messages: list[ProviderMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
                continue
            if m.role == "assistant" and m.tool_calls:
                blocks: list[dict[str, Any]] = []
                # Prefer signed thinking blocks from the live turn (required by Anthropic
                # when continuing a tool-use loop with extended thinking).
                if m.thinking_blocks:
                    blocks.extend(m.thinking_blocks)
                elif m.thinking and self._thinking_effort != "off":
                    blocks.append({"type": "thinking", "thinking": m.thinking})
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
                continue
            if m.role == "user" and m.attachments:
                out.append({"role": "user", "content": build_anthropic_user_content(m.content, m.attachments)})
                continue
            if m.role == "assistant":
                if m.thinking_blocks:
                    blocks = list(m.thinking_blocks)
                    if m.content:
                        blocks.append({"type": "text", "text": m.content})
                    out.append({"role": "assistant", "content": blocks})
                    continue
                if m.thinking and self._thinking_effort != "off":
                    blocks = [{"type": "thinking", "thinking": m.thinking}]
                    if m.content:
                        blocks.append({"type": "text", "text": m.content})
                    out.append({"role": "assistant", "content": blocks})
                    continue
            out.append({"role": m.role, "content": m.content})
        return out

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[ProviderMessage],
        tools: list[dict[str, Any]],
        cancel_event: Any | None = None,
        cache: PromptCachePayload | None = None,
    ) -> AsyncIterator[StreamEvent]:
        client = self._client()
        anthropic_tools = anthropic_tools_with_cache(tools, cache)
        system_payload = anthropic_system_blocks(cache, fallback_system=system)
        anthropic_messages = anthropic_messages_with_cache(self._to_anthropic_messages(messages), cache)
        collected_text = ""
        tool_calls: list[ToolCallRequest] = []
        usage: dict[str, int] = {}

        budget = _EFFORT_BUDGET.get(self._thinking_effort, 0)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "system": system_payload,
            "messages": anthropic_messages,
            "tools": anthropic_tools if anthropic_tools else None,
        }
        if budget > 0:
            # Extended thinking: max_tokens must be greater than budget_tokens.
            kwargs["max_tokens"] = max(8192, budget + 4096)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        else:
            kwargs["max_tokens"] = 8192

        with client.messages.stream(**kwargs) as stream:
            cancelled = False
            for event in stream:
                if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                    cancelled = True
                    break
                if event.type == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", "") or ""
                    if dtype == "text_delta":
                        chunk = delta.text
                        collected_text += chunk
                        yield StreamEvent(kind=StreamEventKind.TEXT_DELTA, text=chunk)
                    elif dtype == "thinking_delta":
                        chunk = getattr(delta, "thinking", None) or getattr(delta, "text", "") or ""
                        if chunk:
                            yield StreamEvent(kind=StreamEventKind.THINKING, text=chunk)
                elif event.type == "content_block_start":
                    block = event.content_block
                    if getattr(block, "type", "") == "tool_use":
                        tool_calls.append(
                            ToolCallRequest(
                                id=block.id,
                                name=block.name,
                                arguments=dict(block.input or {}),
                            )
                        )

            if cancelled:
                return

            final = stream.get_final_message()
            usage = parse_anthropic_usage(final.usage)
            stop = final.stop_reason or ""
            rebuilt_tools: list[ToolCallRequest] = []
            thinking_blocks: list[dict[str, Any]] = []
            for block in final.content:
                btype = getattr(block, "type", "")
                if btype == "tool_use":
                    rebuilt_tools.append(
                        ToolCallRequest(
                            id=block.id,
                            name=block.name,
                            arguments=dict(block.input or {}),
                        )
                    )
                elif btype == "thinking":
                    entry: dict[str, Any] = {
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", "") or "",
                    }
                    sig = getattr(block, "signature", None)
                    if sig:
                        entry["signature"] = sig
                    thinking_blocks.append(entry)
                elif btype == "redacted_thinking":
                    entry = {"type": "redacted_thinking"}
                    data = getattr(block, "data", None)
                    if data is not None:
                        entry["data"] = data
                    thinking_blocks.append(entry)
            if rebuilt_tools:
                yield StreamEvent(
                    kind=StreamEventKind.TOOL_CALLS,
                    tool_calls=rebuilt_tools,
                    usage=usage,
                    thinking_blocks=thinking_blocks,
                )
            yield StreamEvent(
                kind=StreamEventKind.DONE,
                text=collected_text,
                stop_reason=stop,
                usage=usage,
                thinking_blocks=thinking_blocks,
            )

    async def test_connection(self) -> tuple[bool, str]:
        try:
            client = self._client()
            msg = client.messages.create(
                model=self._model,
                max_tokens=16,
                messages=[{"role": "user", "content": "ping"}],
            )
            _ = msg.content
            return True, "Anthropic OK"
        except Exception as e:
            return False, str(e)
