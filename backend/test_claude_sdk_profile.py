"""Offline profile contracts; no SDK/app imports or vendor execution."""
from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

HERE = Path(__file__).resolve().parent


def load_mapper():
    spec = importlib.util.spec_from_file_location("_profile_under_test", HERE / "claude_sdk_profile.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    return module


@pytest.fixture
def m():
    return load_mapper()


@pytest.fixture
def inputs():
    return dict(binary="C:/synthetic/claude.exe", cwd="C:/synthetic/project", model="sonnet",
                prompt_file="C:/synthetic/user.txt", system_prompt_file="C:/synthetic/system.txt",
                mcp_config_file="C:/synthetic/mcp.json",
                mcp_config={"mcpServers": {"uefn": {"command": "mock-bridge", "args": []}}},
                inherited_env={"Path": "C:/synthetic/bin", "CLAUDECODE": "1"},
                env_overlay={"DUCKY_RUN_ID": "synthetic-run"},
                identity={"run_id": "synthetic-run", "conv_id": "synthetic-chat"})


def accepted_functions():
    """Execute actual pure adapter functions, never its imports or launch path."""
    tree = ast.parse((HERE / "claude_code_adapter.py").read_text(encoding="utf-8"))
    names = {"build_claude_argv", "_image_prompt_suffix"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    constants = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in {"_PERMISSION_MODES", "_PERMISSION_PROMPT_TOOL"}
        for t in n.targets)]
    scope = {"shlex": shlex, "_core_has_permission_prompt": lambda: True}
    exec(compile(ast.Module(body=constants + nodes, type_ignores=[]), "accepted-adapter-pure-functions", "exec"), scope)
    return scope


@pytest.mark.parametrize("session", ["", "saved-session"])
@pytest.mark.parametrize("model", ["sonnet", "claude-opus-4-6"])
@pytest.mark.parametrize("permission", ["default", "acceptEdits", "bypassPermissions", "plan"])
@pytest.mark.parametrize("hook", [False, True])
def test_accepted_argv_parity(m, inputs, session, model, permission, hook):
    dirs = ["C:/synthetic/image folder", "C:/synthetic/captures", "D:/other-project"]
    inputs.update(session_id=session, model=model, permission_mode=permission,
                   permission_prompt_available=hook, add_dirs=dirs)
    profile = m.build_profile(inputs)
    old = accepted_functions()
    old["_core_has_permission_prompt"] = lambda: hook
    argv = old["build_claude_argv"](binary=inputs["binary"], prompt="", system_prompt="",
        system_prompt_file=inputs["system_prompt_file"], prompt_via_stdin=True,
        model=model, mcp_config_path=inputs["mcp_config_file"], extra_args="",
        session_id=session, permission_mode=permission, image_dirs=dirs)
    assert profile.argv == tuple(argv + ["--input-format", "stream-json"])
    assert profile.cwd == inputs["cwd"]
    assert profile.options.resume == (session or None)
    assert profile.options.model == model
    assert profile.options.allowed_tools == ("mcp__uefn",)
    assert profile.options.tools is None
    assert profile.options.permission_mode == permission
    assert profile.options.permission_prompt_tool_name == ("mcp__uefn__ducky_permission_prompt" if hook else None)
    assert profile.required_tools == (("ducky_get_tools", "ducky_call_tool", "ducky_permission_prompt") if hook
                                      else ("ducky_get_tools", "ducky_call_tool"))


@pytest.mark.parametrize("session", ["", "saved-session"])
@pytest.mark.parametrize("append", ["", "C:/synthetic/a long directory/system prompt.txt"])
def test_prompt_artifacts_append_preset_and_attachments(m, inputs, session, append):
    images = ["C:/synthetic/a.png", "D:/synthetic/b c.jpg"]
    inputs.update(session_id=session, system_prompt_file=append, image_paths=images)
    profile = m.build_profile(inputs)
    assert profile.user_input.prompt_file == inputs["prompt_file"]
    assert profile.user_input.attachment_suffix == accepted_functions()["_image_prompt_suffix"](images)
    assert profile.options.system_prompt == (("type", "preset"), ("preset", "claude_code"))
    assert ("--append-system-prompt-file" in profile.argv) == bool(append)
    assert "--system-prompt" not in profile.argv
    assert inputs["prompt_file"] not in profile.argv
    assert profile.options.extra_args == ((("append-system-prompt-file", append),) if append else ())


@pytest.mark.parametrize("sources,token", [(None,None),([],"--setting-sources="),
    (["user","project","local"],"--setting-sources=user,project,local"),(["project"],"--setting-sources=project")])
def test_settings_defaults_and_external_hooks(m, inputs, sources, token):
    inputs.update(setting_sources=sources, settings="C:/synthetic/external-hooks.json")
    p = m.build_profile(inputs)
    assert p.options.setting_sources == (None if sources is None else tuple(sources))
    assert ([v for v in p.argv if v.startswith("--setting-sources=")] == ([] if token is None else [token]))
    assert p.options.settings == inputs["settings"]
    assert p.argv[p.argv.index("--settings")+1] == inputs["settings"]
    assert p.options.skills is None


def test_environment_snapshot_policy_and_no_mutation(m, inputs):
    inputs["env_overlay"].update(path="D:/overlay", CLAUDECODE="remove-me", CLAUDE_CODE_ENTRYPOINT="custom-host",
                                  CLAUDE_AGENT_SDK_VERSION="incorrect", TOKEN="synthetic-secret")
    before = json.dumps(inputs, sort_keys=True)
    p = m.build_profile(inputs)
    env = dict(p.env)
    assert env["path"] == "D:/overlay" and "Path" not in env
    assert "CLAUDECODE" not in env
    assert env["CLAUDE_CODE_ENTRYPOINT"] == "custom-host"
    assert env["CLAUDE_AGENT_SDK_VERSION"] == "0.2.165"
    assert p.options.env == p.env
    assert json.dumps(inputs, sort_keys=True) == before
    inputs["env_overlay"]["TOKEN"] = "changed"
    inputs["mcp_config"]["mcpServers"]["uefn"]["args"].append("changed")
    assert dict(p.env)["TOKEN"] == "synthetic-secret"
    assert "changed" not in p.config_snapshot
    assert "synthetic-secret" not in repr(p)


def test_deeply_immutable_outputs(m, inputs):
    p = m.build_profile(inputs)
    def immutable(value):
        if dataclasses.is_dataclass(value):
            for f in dataclasses.fields(value):
                immutable(getattr(value,f.name))
        elif isinstance(value,tuple):
            for item in value:
                immutable(item)
        else:
            assert type(value) in (str,int,bool,type(None))
    immutable(p)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.cwd = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.options.model = "changed"


@pytest.mark.parametrize("tokens,expected", [(["--max-turns","3"],["--max-turns","3"]),
    (["--max-turns=3"],["--max-turns","3"]),(["--max-budget-usd=2.50"],["--max-budget-usd","2.50"])])
def test_safe_explicit_extra_args(m, inputs, tokens, expected):
    inputs["extra_args"] = tokens
    p=m.build_profile(inputs)
    assert list(p.argv[-len(expected):]) == expected


@pytest.mark.parametrize("flag", ["--input-format","--output-format","--mcp-config","--strict-mcp-config",
    "--resume","--continue","--fork-session","--session-id","--permission-mode","--allowedTools",
    "--allowed-tools","--disallowedTools","--tools","--permission-prompt-tool","--system-prompt",
    "--append-system-prompt-file","--replay-user-messages","--include-partial-messages","--settings",
    "--setting-sources","--agents","--model","--sdk-url","-p","--unknown"])
@pytest.mark.parametrize("equals", [False,True])
def test_colliding_and_unknown_extras_are_sanitized(m, inputs, flag, equals):
    inputs["extra_args"] = [flag+"=SYNTHETIC_SECRET"] if equals else [flag,"SYNTHETIC_SECRET"]
    with pytest.raises(m.ProfileError) as caught:
        m.build_profile(inputs)
    assert caught.value.code == "unsupported"
    assert "SYNTHETIC_SECRET" not in str(caught.value)


@pytest.mark.parametrize("field,value", [
    ("model",""),("model","default"),("model",42),("cwd","relative"),("binary","C:/x/claude.cmd"),
    ("prompt_file","relative"),("mcp_config_file",""),("system_prompt_file",None),("permission_mode","auto"),
    ("permission_prompt_available",1),("setting_sources",["team"]),("setting_sources",["user","user"]),
    ("env_overlay",{"bad=name":"x"}),("inherited_env",{"PATH":"a","path":"b"}),
    ("env_overlay",{"key":42}),("identity",{"run_id":lambda:None}),("image_paths",[1]),
    ("add_dirs",["relative"]),("session_id",True),("extra_args","--max-turns 2"),
    ("extra_args",["--max-turns","0"]),("extra_args",["--max-turns","3","--max-turns=4"]),
    ("extra_args",["--max-budget-usd=NaN"]),("extra_args",["--max-turns"]),
    ("mcp_config",{}),("mcp_config",{"mcpServers":{}}),
    ("mcp_config",{"mcpServers":{"uefn":{"command":"x","args":7}}}),
    ("mcp_config",{"mcpServers":{"uefn":{"type":"http","url":7}}}),
    ("mcp_config",{"mcpServers":{"uefn":{"command":"x","env":{"key":None}}}}),
])
def test_malformed_inputs_fail_without_values(m, inputs, field, value):
    inputs[field]=value
    with pytest.raises(m.ProfileError) as caught:
        m.build_profile(inputs)
    assert str(caught.value).startswith("Ducky tools unavailable:")
    assert caught.value.code in ("invalid","unsupported")


@pytest.mark.parametrize("field", ["hooks","can_use_tool","stderr","session_store","agents","skills",
    "system_prompt","prompt","continue_conversation","fork_session","mode","unknown_feature"])
def test_unsupported_initialization_is_explicit(m, inputs, field):
    inputs[field] = {"private": "SYNTHETIC_SECRET", "callback":lambda:None}
    with pytest.raises(m.ProfileError, match="unsupported") as caught:
        m.build_profile(inputs)
    assert "SYNTHETIC_SECRET" not in str(caught.value)


def test_compound_inprocess_mcp_rejected(m, inputs):
    inputs["mcp_config"]["mcpServers"]["other"]={"type":"sdk","instance":object()}
    with pytest.raises(m.ProfileError):
        m.build_profile(inputs)


@pytest.mark.parametrize("server", [{"command":"node","args":["bridge.js"]},
    {"type":"http","url":"${URL}/mcp"},{"type":"streamable-http","url":"${URL:-https://example.invalid}/mcp"},
    {"type":"sse","url":"https://example.invalid/sse"},{"type":"ws","url":"wss://example.invalid/socket"}])
def test_vendor_config_not_expanded_or_url_restricted(m, inputs, server):
    inputs["mcp_config"]={"mcpServers":{"uefn":server}}
    p=m.build_profile(inputs)
    assert json.loads(p.config_snapshot)==inputs["mcp_config"]


def test_mapping_has_no_environment_files_process_sdk_or_app_access(m, inputs, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure mapping crossed an IO boundary")
    original=builtins.__import__
    def guard(name,*args,**kwargs):
        if name.startswith(("claude_agent_sdk","backend","frontend","subprocess")):
            forbidden()
        return original(name,*args,**kwargs)
    with monkeypatch.context() as guarded:
        guarded.setattr(builtins,"__import__",guard)
        guarded.setattr(builtins,"open",forbidden)
        guarded.setattr(Path,"open",forbidden)
        guarded.setattr(os,"getenv",forbidden)
        guarded.setattr(os,"environ",None)
        p=m.build_profile(inputs)
    assert p.options.strict_mcp_config


def test_import_contains_no_runtime_io_or_sdk_dependencies(monkeypatch):
    source=(HERE / "claude_sdk_profile.py").read_text(encoding="utf-8")
    code=compile(source,"isolated-mapper-import","exec")
    namespace={"__name__":"_profile_import_probe"}
    import types
    module=types.ModuleType(namespace["__name__"])
    monkeypatch.setitem(sys.modules,module.__name__,module)
    original=builtins.__import__
    def guard(name,*args,**kwargs):
        assert not name.startswith(("claude_agent_sdk", "backend", "frontend", "subprocess")), name
        return original(name,*args,**kwargs)
    def forbidden(*args,**kwargs):
        raise AssertionError("import IO")
    with monkeypatch.context() as guarded:
        guarded.setattr(builtins,"__import__",guard)
        guarded.setattr(builtins,"open",forbidden)
        guarded.setattr(os,"environ",None)
        exec(code,module.__dict__)


def test_control_flow_is_not_suppressed(m, inputs):
    class Broken(dict):
        def items(self):
            raise KeyboardInterrupt("synthetic control")
    # Non-plain mappings are rejected without invoking user-defined behavior.
    inputs["env_overlay"]=Broken()
    with pytest.raises(m.ProfileError):
        m.build_profile(inputs)


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_control_flow_identity_propagates(m, inputs, monkeypatch, kind):
    signal = kind("synthetic control")
    def interrupted(_value):
        raise signal
    monkeypatch.setattr(m, "_config", interrupted)
    with pytest.raises(kind) as caught:
        m.build_profile(inputs)
    assert caught.value is signal
