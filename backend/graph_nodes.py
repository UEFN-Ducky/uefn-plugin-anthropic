"""Anthropic Automations + Pipelines tiles. No BrainRot imports."""

from __future__ import annotations

from typing import Any

NODES = ("anthropic.complete",)


def handle_complete(ctx: dict[str, Any]) -> dict[str, Any]:
    cfg = ctx.get("config") if isinstance(ctx.get("config"), dict) else {}
    payload = ctx.get("payload") if isinstance(ctx.get("payload"), dict) else {}
    prompt = str(cfg.get("prompt") or payload.get("prompt") or payload.get("text") or "")
    model = str(cfg.get("model") or payload.get("model") or "")
    from backend.automations.llm_complete import complete_prompt

    return complete_prompt("anthropic", prompt, model)


def register_nodes(api: Any) -> None:
    if not hasattr(api, "register_pipeline_node"):
        return
    api.register_pipeline_node("anthropic.complete", handle_complete)
