from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for k in list(sys.modules):
    if k == "backend" or k.startswith("backend."):
        del sys.modules[k]
for p in _here.parents:
    cand = p / "UEFN-Ducky-Release" / "ducky_app"
    if (cand / "backend" / "agent").is_dir():
        sys.path.insert(0, str(_here))
        sys.path.insert(0, str(cand))
        break

from backend.agent.providers.base import ProviderMessage, ToolCallRequest
from anthropic_provider import AnthropicProvider


def test_followup_comes_after_previous_reply():
    msgs = [
        ProviderMessage(role="user", content="first"),
        ProviderMessage(
            role="assistant",
            content="Checking",
            tool_calls=[ToolCallRequest(id="t1", name="ping", arguments={})],
        ),
        ProviderMessage(role="tool", tool_call_id="t1", content='{"ok":true}'),
        ProviderMessage(role="assistant", content="Here is the answer."),
        ProviderMessage(role="user", content="follow up"),
    ]
    out = AnthropicProvider("k", "claude-sonnet-4-0")._to_anthropic_messages(msgs)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[-2]["content"] == "Here is the answer."
    assert out[-1]["content"] == "follow up"


def test_thinking_from_history_has_no_signature_so_it_is_never_sent_back():
    # Oct 30 2026: a chat with extended thinking failed every later turn within 10 s with
    # "messages.N.content.0.thinking.signature: Field required" (400).
    msgs = [
        ProviderMessage(role="user", content="first"),
        ProviderMessage(
            role="assistant", content="Checking", thinking="plan from history",
            tool_calls=[ToolCallRequest(id="t1", name="ping", arguments={})],
        ),
        ProviderMessage(role="tool", tool_call_id="t1", content='{"ok":true}'),
        ProviderMessage(role="assistant", content="Done.", thinking="more history"),
        ProviderMessage(role="user", content="next"),
    ]
    out = AnthropicProvider("k", "claude-sonnet-4-0", thinking_effort="high")._to_anthropic_messages(msgs)
    assert "thinking" not in str([b.get("type") for m in out if isinstance(m["content"], list) for b in m["content"]])
    assert out[1]["content"][0] == {"type": "text", "text": "Checking"}
    assert out[3]["content"] == "Done."


def test_signed_thinking_from_the_live_turn_goes_back_first():
    signed = {"type": "thinking", "thinking": "live", "signature": "sig"}
    msgs = [
        ProviderMessage(role="user", content="go"),
        ProviderMessage(
            role="assistant", content="", thinking="live",
            thinking_blocks=[signed, {"type": "thinking", "thinking": "unsigned"}, {"type": "redacted_thinking", "data": "x"}],
            tool_calls=[ToolCallRequest(id="t1", name="ping", arguments={})],
        ),
        ProviderMessage(role="tool", tool_call_id="t1", content="ok"),
    ]
    out = AnthropicProvider("k", "claude-sonnet-4-0", thinking_effort="high")._to_anthropic_messages(msgs)
    assert [b["type"] for b in out[1]["content"]] == ["thinking", "redacted_thinking", "tool_use"]
    assert out[1]["content"][0]["signature"] == "sig"
