"""Pure, UNUSED Windows launch profile for claude-agent-sdk 0.2.165.

Public contract: anthropics/claude-agent-sdk-python at
f7529348747235da34af3ed202251228ff31db08, types.py and client.py.
That release's bundled CLI source specifies 2.1.294; this is not runtime
version validation, packaging policy or an installer. No SDK/app imports.

Custom Transport does not apply ClaudeAgentOptions. A future owner must use
the explicit argv/env AND materialize the public option descriptors below.
The system-prompt preset retains normal Claude defaults; append-file is a
public extra_args descriptor, not a fictitious system_prompt file-append API.
None setting_sources retains normal CLI sources; () disables them. CLI
external settings hooks remain enabled. No SDK skills auto-configuration.

Caller owns prompt/config artifacts, their identity, contents and lifetime.
A frozen path/snapshot is NOT proof they match or cannot change before CLI
reads them. The caller must establish that separately. Identity here is data,
not authorization. add_dirs is already resolved by the caller (including
captures and additional projects); this module neither discovers nor reads it.

No user frame, client, readiness decision or result attribution is produced.
Fresh human-origin inference, resumed historical results and unlabelled UI
frames remain integration gates. Keep the production runner's refusal intact.
The app-owned shared daemon must remain outside disposable agent ownership.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import ntpath

SDK_VERSION = "0.2.165"
BUNDLED_CLI_VERSION = "2.1.294"
_PERMISSIONS = ("acceptEdits", "bypassPermissions", "default", "plan")
_FIELDS = frozenset({"binary", "cwd", "model", "prompt_file", "system_prompt_file",
    "mcp_config_file", "mcp_config", "inherited_env", "env_overlay", "identity",
    "session_id", "permission_mode", "permission_prompt_available", "add_dirs",
    "image_paths", "setting_sources", "settings", "extra_args"})


class ProfileError(ValueError):
    """Structured, fixed diagnostic; never contains supplied values or keys."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(
            "Ducky tools unavailable: unsupported Claude SDK profile; use a supported explicit launch profile."
            if code == "unsupported" else
            "Ducky tools unavailable: invalid Claude SDK profile; check the supplied launch configuration."
        )


@dataclass(frozen=True)
class UserInput:
    """Later owner reads prompt_file and appends suffix once, never on argv."""
    prompt_file: str = field(repr=False)
    attachment_suffix: str = field(repr=False)


@dataclass(frozen=True)
class PublicOptions:
    """Immutable descriptors of public ClaudeAgentOptions, not an SDK object.

    Tuple maps/lists must be materialized into fresh dicts/lists by a future
    client factory. No mutable SDK object is shared across launches. env is
    the complete effective snapshot for the owned transport; passing it as
    options.env alone does NOT prevent SDK default transport inheritance.
    """
    model: str
    resume: str | None = field(repr=False)
    cwd: str = field(repr=False)
    system_prompt: tuple[tuple[str, str], ...]
    extra_args: tuple[tuple[str, str], ...] = field(repr=False)
    mcp_servers: str = field(repr=False)
    strict_mcp_config: bool
    allowed_tools: tuple[str, ...]
    permission_mode: str
    permission_prompt_tool_name: str | None
    add_dirs: tuple[str, ...] = field(repr=False)
    setting_sources: tuple[str, ...] | None
    settings: str | None = field(repr=False)
    env: tuple[tuple[str, str], ...] = field(repr=False)
    tools: None = None
    skills: None = None
    include_partial_messages: bool = True
    continue_conversation: bool = False
    fork_session: bool = False


@dataclass(frozen=True)
class Profile:
    argv: tuple[str, ...] = field(repr=False)
    env: tuple[tuple[str, str], ...] = field(repr=False)
    cwd: str = field(repr=False)
    options: PublicOptions
    user_input: UserInput = field(repr=False)
    identity: tuple[tuple[str, str], ...] = field(repr=False)
    config_snapshot: str = field(repr=False)
    required_tools: tuple[str, ...]
    required_server: str = "uefn"
    sdk_version: str = SDK_VERSION


def _text(value, *, empty=False):
    if type(value) is not str or "\0" in value or (not empty and not value.strip()):
        raise ProfileError("invalid")
    return value


def _path(value):
    value = _text(value)
    # Owned Windows transport needs a fully qualified path, not drive-relative
    # or rooted-on-current-drive paths. No resolve/stat/home/environment access.
    drive, tail = ntpath.splitdrive(value)
    if not drive or not ntpath.isabs(value) or not tail.startswith(("/", "\\")):
        raise ProfileError("invalid")
    return value


def _strings(value, *, paths=False):
    if type(value) not in (list, tuple):
        raise ProfileError("invalid")
    return tuple((_path if paths else _text)(item) for item in value)


def _pairs(value, *, environment=False):
    if type(value) is not dict:
        raise ProfileError("invalid")
    pairs, seen = [], set()
    for key, item in value.items():
        _text(key)
        _text(item, empty=True)
        canonical = key.casefold() if environment else key
        if canonical in seen or (environment and "=" in key):
            raise ProfileError("invalid")
        seen.add(canonical)
        pairs.append((key, item))
    return tuple(pairs)


def _json(value, depth=0):
    """Plain JSON only: no callbacks, custom encoders, cycles or magic methods."""
    if depth > 32:
        raise ProfileError("invalid")
    if type(value) in (str, int, bool, type(None)):
        if type(value) is str:
            _text(value, empty=True)
        return value
    if type(value) is list:
        return [_json(item, depth + 1) for item in value]
    if type(value) is dict:
        return {_text(key): _json(item, depth + 1) for key, item in value.items()}
    raise ProfileError("unsupported")


def _config(value):
    if type(value) is not dict or set(value) != {"mcpServers"}:
        raise ProfileError("invalid")
    servers = value["mcpServers"]
    if type(servers) is not dict or "uefn" not in servers:
        raise ProfileError("invalid")
    for name, server in servers.items():
        _text(name)
        if type(server) is not dict:
            raise ProfileError("invalid")
        kind = server.get("type", "stdio" if "command" in server else "http")
        if type(kind) is not str or kind not in ("stdio", "http", "sse", "streamable-http", "ws"):
            raise ProfileError("unsupported")
        if kind == "stdio":
            _text(server.get("command"))
            _strings(server.get("args", ()))
            if "url" in server:
                raise ProfileError("invalid")
        else:
            # CLI owns interpolation/URL semantics; do not read environment or
            # duplicate its URL parser. Values still must be nonempty strings.
            _text(server.get("url"))
            if "command" in server or "args" in server:
                raise ProfileError("invalid")
        for key in ("env", "headers"):
            if key in server:
                _pairs(server[key], environment=(key == "env"))
    return json.dumps(_json(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _extras(value):
    tokens = _strings(value)
    mapped = []
    seen = set()
    i = 0
    while i < len(tokens):
        flag, sep, val = tokens[i].partition("=")
        if flag not in ("--max-turns", "--max-budget-usd"):
            raise ProfileError("unsupported")
        if flag in seen:
            raise ProfileError("invalid")
        seen.add(flag)
        if not sep:
            i += 1
            if i == len(tokens):
                raise ProfileError("invalid")
            val = tokens[i]
        if flag == "--max-turns":
            if not val.isascii() or not val.isdecimal() or len(val) > 9 or int(val) < 1:
                raise ProfileError("invalid")
        else:
            try:
                budget = Decimal(val)
            except InvalidOperation:
                raise ProfileError("invalid") from None
            if not budget.is_finite() or budget <= 0:
                raise ProfileError("invalid")
        mapped.append((flag[2:], val))
        i += 1
    return tuple(mapped)


def build_profile(inputs: dict) -> Profile:
    """Map explicit input data; reject unsupported fields without silent drops.

    Required: binary/cwd/model/prompt_file/mcp_config_file/mcp_config,
    inherited_env/env_overlay/identity. Optional fields are named in _FIELDS.
    Only tokenized max-turns/max-budget-usd extras are supported. Every other
    legacy extra is a structured rejection for this unused mapper, not a
    behavioral change to the existing adapter. Native permission_mode=plan
    remains a vendor preference; this mapper does not implement Ducky modes.
    """
    if type(inputs) is not dict:
        raise ProfileError("invalid")
    if any(type(key) is not str or key not in _FIELDS for key in inputs):
        raise ProfileError("unsupported")
    binary = _path(inputs.get("binary"))
    if ntpath.splitext(binary)[1].lower() != ".exe":
        raise ProfileError("unsupported")
    cwd = _path(inputs.get("cwd"))
    model = _text(inputs.get("model"))
    if model.strip().lower() == "default" or model.startswith("-"):
        raise ProfileError("invalid")
    prompt_file = _path(inputs.get("prompt_file"))
    append_file = _text(inputs.get("system_prompt_file", ""), empty=True)
    if append_file:
        _path(append_file)
    config_file = _path(inputs.get("mcp_config_file"))
    config_snapshot = _config(inputs.get("mcp_config"))
    session = _text(inputs.get("session_id", ""), empty=True)
    if session.startswith("-"):
        raise ProfileError("invalid")
    permission = inputs.get("permission_mode", "acceptEdits")
    if type(permission) is not str or permission not in _PERMISSIONS:
        raise ProfileError("unsupported")
    hook = inputs.get("permission_prompt_available", False)
    if type(hook) is not bool:
        raise ProfileError("invalid")
    dirs = _strings(inputs.get("add_dirs", ()), paths=True)
    images = _strings(inputs.get("image_paths", ()), paths=True)
    sources = inputs.get("setting_sources")
    if sources is not None:
        sources = _strings(sources)
        if len(set(sources)) != len(sources) or any(v not in ("user", "project", "local") for v in sources):
            raise ProfileError("unsupported")
    settings = inputs.get("settings")
    if settings is not None:
        settings = _text(settings)
    identity = _pairs(inputs.get("identity"))
    inherited = _pairs(inputs.get("inherited_env"), environment=True)
    overlay = _pairs(inputs.get("env_overlay"), environment=True)
    # Windows keys are case-insensitive. Overlay wins deterministically; forbid
    # duplicate aliases within each explicit source, filter nested-CLI marker.
    effective = {key.casefold(): (key, value) for key, value in inherited}
    effective["claude_code_entrypoint"] = ("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    effective.update({key.casefold(): (key, value) for key, value in overlay})
    effective.pop("claudecode", None)
    effective["claude_agent_sdk_version"] = ("CLAUDE_AGENT_SDK_VERSION", SDK_VERSION)
    env = tuple(effective.values())
    extras = _extras(inputs.get("extra_args", ()))
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--mcp-config", config_file, "--strict-mcp-config", "--allowedTools", "mcp__uefn"]
    permission_tool = "mcp__uefn__ducky_permission_prompt" if hook else None
    if permission_tool:
        argv += ["--permission-prompt-tool", permission_tool]
    argv += ["--permission-mode", permission]
    for directory in dirs:
        argv += ["--add-dir", directory]
    if session:
        argv += ["--resume", session]
    option_extras = ()
    if append_file:
        argv += ["--append-system-prompt-file", append_file]
        option_extras = (("append-system-prompt-file", append_file),)
    argv += ["--model", model, "--input-format", "stream-json"]
    if sources is not None:
        argv += ["--setting-sources=" + ",".join(sources)]
    if settings is not None:
        argv += ["--settings", settings]
    for flag, value in extras:
        argv += ["--" + flag, value]
    suffix = ""
    if images:
        listed = "\n".join(f"- {path}" for path in images)
        suffix = ("\n\nThe user attached image file(s) with this message. View them by reading these "
                  f"absolute paths with your Read tool (it renders images):\n{listed}")
    options = PublicOptions(model, session or None, cwd, (("type", "preset"), ("preset", "claude_code")),
        option_extras + extras, config_file, True, ("mcp__uefn",), permission, permission_tool,
        dirs, sources, settings, env)
    required = ("ducky_get_tools", "ducky_call_tool") + (("ducky_permission_prompt",) if hook else ())
    return Profile(tuple(argv), env, cwd, options, UserInput(prompt_file, suffix), identity,
                   config_snapshot, required)
