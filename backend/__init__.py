"""Anthropic gateway — LLM provider + Claude Code via host registries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_CLAUDE_FAMILIES = frozenset({"sonnet", "opus", "haiku", "fable"})
_ANTHROPIC_FAMILY_FALLBACK = {
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5-1",
}

_INSTALL_HELP = (
    "Needs the Claude Code CLI (`claude` in PowerShell) — not the Claude Desktop chat app. "
    "Install in Windows PowerShell (not Git Bash): irm https://claude.ai/install.ps1 | iex "
    "Then add %USERPROFILE%\\.local\\bin to your user PATH, open a new PowerShell, run claude --version, "
    "restart Ducky, and click Detect."
)


def _fetch_models(api_key: str, **_kw: Any) -> Any:
    from .model_fetch import fetch_models

    return fetch_models(api_key)


def _anthropic_id_for_family(family: str) -> str:
    fam = (family or "sonnet").strip().lower()
    try:
        from backend.agent.model_fetch import fetch_models
        from backend.agent.secrets import get_key, has_key

        if has_key("anthropic"):
            rows = fetch_models("anthropic", get_key("anthropic") or "")
            ids = [(item.id if hasattr(item, "id") else str(item)).strip() for item in rows]
            hits = [i for i in ids if fam in i.lower()]
            if hits:
                hits.sort(reverse=True)
                return hits[0]
    except Exception:
        pass
    return _ANTHROPIC_FAMILY_FALLBACK.get(fam, _ANTHROPIC_FAMILY_FALLBACK["sonnet"])


def _resolve_api_fallback(model_id: str) -> tuple[str, str] | None:
    from backend.agent.secrets import has_key

    if not has_key("anthropic"):
        return None
    mid = (model_id or "").strip() or "sonnet"
    fam = mid if mid.lower() in _CLAUDE_FAMILIES else "sonnet"
    if mid.lower() not in _CLAUDE_FAMILIES and mid.startswith("claude"):
        return "anthropic", mid
    return "anthropic", _anthropic_id_for_family(fam)


def _complete_one_shot(*, model: str, system: str, user: str) -> str:
    import os
    import subprocess

    from .claude_auth import resolve_claude_bin

    binary = resolve_claude_bin("")
    if not binary:
        raise ValueError(
            "Claude Code CLI not found. Install `claude` or add an Anthropic API key."
        )
    from backend.agent.coding_agents.mcp_inject import write_prompt_file

    sys_path = write_prompt_file(system, conv_id="oneshot-sys") if system.strip() else None
    argv = [
        binary,
        "-p",
        "--output-format",
        "text",
        "--model",
        model or "sonnet",
    ]
    if sys_path is not None:
        argv.extend(["--append-system-prompt-file", str(sys_path)])
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 180,
        "encoding": "utf-8",
        "errors": "replace",
        "input": user,
    }
    if os.name == "nt":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(argv, **run_kwargs)
    finally:
        if sys_path is not None:
            try:
                sys_path.unlink(missing_ok=True)
            except OSError:
                pass
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise ValueError(err or out or "Claude Code completion failed")
    if not out:
        raise ValueError("Claude Code returned empty response")
    return out


def _thinking_env(thinking_effort: str) -> dict[str, str]:
    from backend.agent.thinking_effort import EFFORT_BUDGET, normalize_thinking_effort

    budget = EFFORT_BUDGET.get(normalize_thinking_effort(thinking_effort), 0)
    return {"MAX_THINKING_TOKENS": str(budget)} if budget > 0 else {}


def _skills_dir() -> str:
    return str(Path.home() / ".claude" / "skills")


def _before_launch(
    *,
    conv: Any,
    user_text: str,
    cli_path: str,
    cwd: str,
    push: Any,
    run_id: str,
    agent_id: str,
    emit_assistant: Any,
    **_kw: Any,
) -> dict[str, Any] | None:
    from .claude_auth import (
        clear_pending_auth,
        continue_claude_login,
        get_pending_auth,
        is_claude_logged_in,
        start_claude_login,
    )

    pending = get_pending_auth(conv.id)
    if pending:
        outcome = continue_claude_login(conv_id=conv.id, user_text=user_text, cli_path=cli_path)
        if outcome.get("restart"):
            clear_pending_auth(conv.id)
            deferred = str(outcome.get("deferred_prompt") or user_text)
            started = start_claude_login(
                conv_id=conv.id,
                cwd=cwd,
                cli_path=cli_path,
                deferred_prompt=deferred,
                push=push,
            )
            return emit_assistant(
                conv,
                agent_id=agent_id,
                reply=str(started.get("message") or outcome.get("message") or ""),
                push=push,
                run_id=run_id,
                ok=True,
                terminal_session_id=str(started.get("terminal_session_id") or ""),
                status="needs_login",
            )
        if outcome.get("logged_in") and outcome.get("deferred_prompt") is not None:
            return {"__run_prompt__": str(outcome.get("deferred_prompt") or "")}
        return emit_assistant(
            conv,
            agent_id=agent_id,
            reply=str(outcome.get("message") or outcome.get("error") or "Login still pending."),
            push=push,
            run_id=run_id,
            ok=bool(outcome.get("ok")),
            error=str(outcome.get("error") or ""),
            terminal_session_id=str(
                outcome.get("terminal_session_id") or pending.get("terminal_session_id") or ""
            ),
            status="needs_login"
            if outcome.get("needs_login")
            else ("done" if outcome.get("ok") else "error"),
        )

    if is_claude_logged_in(cli_path):
        return None

    started = start_claude_login(
        conv_id=conv.id,
        cwd=cwd,
        cli_path=cli_path,
        deferred_prompt=user_text,
        push=push,
    )
    if started.get("logged_in"):
        return None
    return emit_assistant(
        conv,
        agent_id=agent_id,
        reply=str(started.get("message") or started.get("error") or "Claude login required."),
        push=push,
        run_id=run_id,
        ok=bool(started.get("ok")),
        error=str(started.get("error") or ""),
        terminal_session_id=str(started.get("terminal_session_id") or ""),
        status="needs_login"
        if started.get("needs_login")
        else ("done" if started.get("ok") else "error"),
    )


def _on_needs_login(
    *,
    conv: Any,
    user_text: str,
    cli_path: str,
    cwd: str,
    push: Any,
    run_id: str,
    agent_id: str,
    reply: str,
    result: Any,
    emit_assistant: Any,
    **_kw: Any,
) -> dict[str, Any]:
    from .claude_auth import start_claude_login

    started = start_claude_login(
        conv_id=conv.id,
        cwd=cwd,
        cli_path=cli_path,
        deferred_prompt=user_text,
        push=push,
    )
    return emit_assistant(
        conv,
        agent_id=agent_id,
        reply=str(started.get("message") or reply),
        push=push,
        run_id=run_id,
        ok=True,
        terminal_session_id=str(
            started.get("terminal_session_id") or getattr(result, "terminal_session_id", "") or ""
        ),
        status="needs_login",
        blocks=getattr(result, "blocks", None),
    )


def register(api) -> None:
    from .anthropic_provider import AnthropicProvider
    from .claude_code_adapter import ClaudeCodeAdapter

    from .model_fetch import clear_model_cache

    api.register_llm_provider(
        "anthropic",
        factory=lambda api_key, model, **kw: AnthropicProvider(api_key, model, **kw),
        fetch_models=_fetch_models,
        test_key_model="claude-haiku-4-5-20251001",
        tool_schema="anthropic",
        clear_model_cache=clear_model_cache,
        cache_mode="cached",
        cost_mode="inclusive_input",
        shows_thinking_effort=True,
    )
    api.register_coding_agent(
        "claude_code",
        factory=lambda: ClaudeCodeAdapter(),
        complete_one_shot=_complete_one_shot,
        resolve_api_fallback=_resolve_api_fallback,
        aliases=["claude", "claudecode", "claude_code_cli"],
        skills_dir=_skills_dir,
        native_skills=True,
        thinking_env=_thinking_env,
        before_launch=_before_launch,
        on_needs_login=_on_needs_login,
        settings_defaults={
            "enabled": True,
            "cli_path": "",
            "default_args": "",
            "permission_mode": "acceptEdits",
        },
        install_help=_INSTALL_HELP,
        token_provider="anthropic",
        login_status_ok="logged in",
        shows_thinking_effort=True,
    )
    api.register_ide_hookup("claude", label="Claude")
    api.log("Anthropic gateway contribution active (Providers + Claude Code + IDE)")
