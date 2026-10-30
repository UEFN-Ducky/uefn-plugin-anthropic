from __future__ import annotations

import importlib
import json
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

# Load this plugin's modules as a package (relative imports) under a private name.
sys.modules.pop("backend", None)
pkg = types.ModuleType("_account_failover_test")
pkg.__path__ = [str(Path(__file__).resolve().parent)]
sys.modules[pkg.__name__] = pkg
a = importlib.import_module(pkg.__name__ + ".claude_code_adapter")
fo = importlib.import_module(pkg.__name__ + ".failover")
u = importlib.import_module(pkg.__name__ + ".usage")
from backend.agent.coding_agents.proc_exec import ProcResult

FUTURE = 2_000_000_000.0
AGENT = "claude_code"


@pytest.fixture(autouse=True)
def accounts(tmp_path, monkeypatch):
    """Core's real account store in tmp, Claude's own folder in tmp; no network, no CLI."""
    monkeypatch.delenv("DUCKY_CLAUDE_FAILOVER", raising=False)
    # Other test files reload the app's `backend`: patch the module failover imports now.
    core = importlib.import_module("backend.agent.accounts")
    own = tmp_path / "own"
    (own / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(own))
    monkeypatch.setattr(core, "_data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(core, "_agent_reg", lambda _agent: {
        "account_env": lambda d: {"CLAUDE_CONFIG_DIR": str(d)},
        "account_shared": {"dirs": ["projects"], "files": [], "primary": str(own)},
    })
    monkeypatch.setattr(core, "_CURRENT", {})
    core.rename_account(AGENT, "default", "Account 1")
    ns = types.SimpleNamespace(core=core, own=own, windows={})
    monkeypatch.setattr(fo, "_windows", lambda account: ns.windows.get(account))

    def add(label):
        new_id = core.add_account(AGENT, label, "login")
        return new_id, core.account_config_dir(AGENT, new_id)

    ns.add = add
    ns.dir = lambda account: fo.config_dir(account)
    return ns


def _row(wid: str, used: float, resets_at: float | None = FUTURE) -> dict:
    row = {"id": wid, "label": wid, "used": used, "limit": 100.0}
    if resets_at:
        row["resets_at"] = resets_at
    return row


# ── core's accounts ─────────────────────────────────────────────────────


def test_one_account_is_not_in_play(accounts):
    assert not fo.in_play()
    accounts.add("Home")
    assert fo.in_play()


def test_default_account_is_claudes_own_folder(accounts):
    home, home_dir = accounts.add("Home")
    assert fo.config_dir("default") == accounts.own
    assert fo.config_dir(home) == home_dir
    assert fo.label(home) == "Home" and fo.label("default") == "Account 1"


def test_tests_stay_off_the_real_accounts(accounts, monkeypatch):
    accounts.add("Home")
    monkeypatch.setenv("DUCKY_CLAUDE_FAILOVER", "off")
    assert not fo.in_play()


def test_limit_without_reset_uses_usage_then_fallback(accounts):
    home, _ = accounts.add("Home")
    accounts.windows["default"] = [_row("weekly", 100, FUTURE)]
    fo.report_limit("default")
    assert accounts.core.exhausted_until(AGENT, "default") == FUTURE
    fo.report_limit(home)
    until = accounts.core.exhausted_until(AGENT, home)
    assert abs(until - (time.time() + fo.FALLBACK_WAIT_S)) < 5


def test_next_account_skips_full_ones_and_none_when_all_full(accounts):
    home, _ = accounts.add("Home")
    work, _ = accounts.add("Work")
    fo.report_limit("default", FUTURE)
    fo.report_limit(home, FUTURE)
    assert fo.next_account("default")[0] == work
    fo.report_limit(work, FUTURE + 100)
    assert fo.next_account(work) == (None, {})  # every account is full: no replay


def test_full_until_ignores_passed_resets():
    assert fo.full_until([_row("hourly", 100, time.time() - 5)]) is None
    assert fo.full_until([_row("hourly", 40), _row("weekly", 100, FUTURE)]) == FUTURE


# ── limit messages ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Claude AI usage limit reached|1750708800",
    "Claude usage limit reached. Your limit will reset at 3pm (Europe/Paris).",
    "5-hour limit reached ∙ resets 3pm",
    "Weekly limit reached · resets Oct 12, 1pm",
    "Opus weekly limit reached ∙ resets Mon 9am",
    "You've hit your limit · resets 3pm (Europe/Paris)",
    "You’ve hit your session limit · resets 1pm (Europe/Berlin)",
    "API Error: You've hit your weekly limit · resets Nov 2 (error type rate_limit, HTTP 429)",
    "You're out of extra usage · resets 5pm",
])
def test_recognises_claude_limit_messages(text):
    assert fo.is_usage_limit_error("", None, text)


@pytest.mark.parametrize("text", [
    "Context limit reached · /compact or /clear to continue",
    "Credit balance is too low",
    "API Error: Usage credits required for 1M context · turn on usage credits",
    "API Error: 529 overloaded_error",
    "Claude Code turn failed",
    "Request rate limit exceeded",
])
def test_ignores_other_errors(text):
    assert not fo.is_usage_limit_error(text)


def test_reset_epoch_from_the_pipe_form():
    assert fo.limit_reset_epoch("x", "Claude AI usage limit reached|1750708800") == 1750708800.0
    assert fo.limit_reset_epoch("Claude AI usage limit reached|1750708800000") == 1750708800.0
    assert fo.limit_reset_epoch("You've hit your limit · resets 3pm") is None


# ── sessions ────────────────────────────────────────────────────────────


def test_carry_session_copies_when_missing_or_older(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    src = one / "projects" / "C--proj"
    src.mkdir(parents=True)
    (src / "sid-1.jsonl").write_text("one\n", encoding="utf-8")
    assert fo.carry_session("sid-1", one, two)
    dst = two / "projects" / "C--proj" / "sid-1.jsonl"
    assert dst.read_text(encoding="utf-8") == "one\n"
    dst.write_text("one\ntwo\n", encoding="utf-8")
    os.utime(dst, (time.time() + 10, time.time() + 10))
    assert fo.carry_session("sid-1", two, one)
    assert (src / "sid-1.jsonl").read_text(encoding="utf-8") == "one\ntwo\n"
    assert fo.carry_session("sid-1", one, two)  # An up-to-date copy is left alone.
    assert dst.read_text(encoding="utf-8") == "one\ntwo\n"


def test_replay_carries_the_transcript_from_before_the_failed_turn(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    src = one / "projects" / "C--proj"
    src.mkdir(parents=True)
    (src / "sid-1.jsonl").write_text("before\n", encoding="utf-8")
    before = fo.session_snapshot("sid-1", one)
    (src / "sid-1.jsonl").write_text("before\nprompt\nlimit\n", encoding="utf-8")
    assert fo.carry_session("sid-1", one, two, upto=before)
    assert (two / "projects" / "C--proj" / "sid-1.jsonl").read_text(encoding="utf-8") == "before\n"


def test_replay_on_shared_projects_drops_the_failed_attempt(accounts):
    home, home_dir = accounts.add("Home")
    accounts.core.coding_agent_account_env(AGENT)  # links the shared projects folder
    accounts.core.set_active_account(AGENT, home)
    accounts.core.coding_agent_account_env(AGENT)
    shared = accounts.own / "projects" / "C--proj"
    shared.mkdir()
    (shared / "sid-1.jsonl").write_text("before\n", encoding="utf-8")
    before = fo.session_snapshot("sid-1", accounts.own)
    (shared / "sid-1.jsonl").write_text("before\nprompt\nlimit\n", encoding="utf-8")
    assert fo.carry_session("sid-1", accounts.own, home_dir, upto=before)
    assert (shared / "sid-1.jsonl").read_text(encoding="utf-8") == "before\n"
    assert (home_dir / "projects" / "C--proj" / "sid-1.jsonl").read_text(encoding="utf-8") == "before\n"


def test_carry_session_rejects_missing_and_bad_ids(tmp_path):
    assert not fo.carry_session("nope", tmp_path / "a", tmp_path / "b")
    assert not fo.carry_session("", tmp_path / "a", tmp_path / "b")
    assert not fo.carry_session("../evil", tmp_path / "a", tmp_path / "b")


# ── usage per account ───────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture
def usage_clean(monkeypatch):
    monkeypatch.setattr(u, "_LAST_WINDOWS", [])
    monkeypatch.setattr(u, "_BLOCKED_UNTIL", 0.0)
    monkeypatch.setattr(u, "_DIR_LAST", {})
    monkeypatch.setattr(u, "_DIR_BLOCKED_UNTIL", {})
    monkeypatch.setattr(u, "_FETCHED_AT", {})


def _login(folder: Path, token: str) -> None:
    (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8")


def test_account_folder_reads_only_its_own_credentials(accounts, usage_clean, monkeypatch):
    import httpx

    _, home_dir = accounts.add("Home")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, **k: seen.append(headers["Authorization"]) or _Resp(
        200, {"five_hour": {"utilization": 1, "resets_at": "2033-05-18T03:33:20+00:00"}}))
    assert u.account_windows(str(home_dir)) is None and not seen
    _login(home_dir, "tok2")
    rows = u.account_windows(str(home_dir))
    assert seen == ["Bearer tok2"]
    assert rows[0]["used"] == 100 and rows[0]["resets_at"] == FUTURE
    u.account_windows(str(home_dir))
    assert len(seen) == 1  # reused for a minute


def test_429_blocks_are_per_account(accounts, usage_clean, monkeypatch):
    import httpx

    _, home_dir = accounts.add("Home")
    _login(home_dir, "tok2")
    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok1"}))
    calls: list[str] = []

    def fake_get(url, headers=None, **k):
        calls.append(headers["Authorization"])
        if headers["Authorization"] == "Bearer tok1":
            return _Resp(429, headers={"retry-after": "600"})
        return _Resp(200, {"seven_day": {"utilization": 0.5, "resets_at": FUTURE}})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert u.account_windows(u.PRIMARY, max_age=0) is None
    assert u._BLOCKED_UNTIL > time.time() and not u._DIR_BLOCKED_UNTIL
    assert u.account_windows(str(home_dir), max_age=0)[0]["used"] == 50
    u.account_windows(u.PRIMARY, max_age=0)
    assert calls == ["Bearer tok1", "Bearer tok2"]


def test_refreshed_token_is_saved_to_the_accounts_file(accounts, usage_clean, monkeypatch):
    import httpx

    _, home_dir = accounts.add("Home")
    path = home_dir / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old", "refreshToken": "r", "expiresAt": 1}}),
                    encoding="utf-8")
    monkeypatch.setattr(u, "_refresh_claude_oauth", lambda oauth: {**oauth, "accessToken": "new", "expiresAt": 9e12})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"five_hour": {"utilization": 0.1}}))
    assert u.account_windows(str(home_dir), max_age=0)
    assert json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "new"


def test_meter_shows_the_running_account_and_sends_no_switch(accounts, usage_clean, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"five_hour": {"utilization": 0.2}}))
    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok1"}))
    assert u.fetch_usage("")["windows"][0]["label"] == "5-hour limit"  # one account: no prefix
    work, work_dir = accounts.add("Work")
    _login(work_dir, "tok3")
    accounts.core.set_active_account(AGENT, work)
    accounts.core.coding_agent_account_env(AGENT)
    out = u.fetch_usage("")
    assert out["windows"][0]["label"] == "Work · 5-hour limit"
    assert "switch" not in out  # core builds the picker's account switch


# ── adapter ─────────────────────────────────────────────────────────────


@pytest.fixture
def launch(tmp_path, monkeypatch, accounts):
    from frontend.settings import PanelSettings

    calls, events, script = [], [], []
    monkeypatch.setattr(a, "resolve_claude_bin", Mock(return_value="mock-claude"))
    monkeypatch.setattr(a, "claude_extra_dirs", lambda *_: [])
    monkeypatch.setattr(a, "_with_chat_captures", lambda *_: [])
    monkeypatch.setattr(a, "coding_agent_cfg", lambda *_: {"permission_mode": "acceptEdits"})
    monkeypatch.setattr(a, "_chat_permission_mode", lambda _conv, configured: configured)
    monkeypatch.setattr(a, "auto_compact_window", lambda: 0)
    monkeypatch.setattr(PanelSettings, "load", lambda: object())
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers":{"uefn":{"command":"mock-only"}}}', encoding="utf-8")

    def process(**kw):
        calls.append(kw)
        return script.pop(0)(kw)

    monkeypatch.setattr(a, "run_streaming_process", process)
    kwargs = dict(prompt="user payload", system_prompt="", cwd=str(tmp_path), conv_id="test",
                  model="sonnet", mcp_config_path=str(config), extra_args="", cli_path="",
                  push=events.append)

    def run(session=""):
        # Core's runner picks the account and adds its env before launch.
        env = {"MOCK_ENV": "kept", **accounts.core.coding_agent_account_env(AGENT)}
        return a.ClaudeCodeAdapter().launch(**dict(kwargs, session_id=session, env=env))

    return types.SimpleNamespace(run=run, calls=calls, events=events, script=script)


def _line(kw, data):
    kw["on_line"](json.dumps(data))


def _init(kw):
    _line(kw, {"type": "system", "subtype": "init", "session_id": "sid-1",
               "mcp_servers": [{"name": "uefn", "status": "connected"}]})


LIMIT = "Claude AI usage limit reached|2000000000"


def limit_turn(kw, *, work=False):
    _init(kw)
    if work:
        _line(kw, {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}], "usage": {"input_tokens": 9}}})
        _line(kw, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    _line(kw, {"type": "assistant", "message": {"id": "m2", "model": "<synthetic>", "content": [
        {"type": "text", "text": LIMIT}], "usage": {"input_tokens": 0, "output_tokens": 0}}})
    _line(kw, {"type": "result", "subtype": "success", "is_error": True, "result": LIMIT, "num_turns": 1,
               "total_cost_usd": 0, "session_id": "sid-1",
               "usage": {"input_tokens": 9 if work else 0, "output_tokens": 0}})
    return ProcResult(returncode=1)


def ok_turn(kw):
    _init(kw)
    _line(kw, {"type": "result", "subtype": "success", "result": "answer", "session_id": "sid-1",
               "usage": {"output_tokens": 3}})
    return ProcResult(returncode=0)


def _texts(launch):
    return [str(e.get("text")) for e in launch.events]


def test_first_two_full_turn_runs_on_the_third(accounts, launch):
    home, _ = accounts.add("Home")
    work, work_dir = accounts.add("Work")
    accounts.core.mark_exhausted(AGENT, "default", FUTURE)
    accounts.core.mark_exhausted(AGENT, home, FUTURE)
    launch.script[:] = [ok_turn]
    assert launch.run().ok
    assert launch.calls[0]["env_extra"]["CLAUDE_CONFIG_DIR"] == str(work_dir)
    assert "Account 1 is at its limit — continuing on Work…" in _texts(launch)


def test_limit_replays_once_on_the_next_account(accounts, launch):
    home, home_dir = accounts.add("Home")
    accounts.add("Work")
    session = accounts.own / "projects" / "C--proj"
    session.mkdir(parents=True)
    (session / "prior.jsonl").write_text("{}\n", encoding="utf-8")
    launch.script[:] = [limit_turn, ok_turn]
    result = launch.run("prior")
    assert result.ok and result.reply_text == "answer"
    first, second = launch.calls
    assert "CLAUDE_CONFIG_DIR" not in first["env_extra"]
    assert second["env_extra"]["CLAUDE_CONFIG_DIR"] == str(home_dir)
    assert second["env_extra"]["MOCK_ENV"] == "kept"
    assert second["argv"][second["argv"].index("--resume") + 1] == "prior"
    assert (home_dir / "projects" / "C--proj" / "prior.jsonl").is_file()
    assert accounts.core.exhausted_until(AGENT, "default") == FUTURE
    assert "Account 1 is at its limit — continuing on Home…" in _texts(launch)
    # The next message starts on Home straight away.
    launch.script[:] = [ok_turn]
    assert launch.run("sid-1").ok
    assert launch.calls[2]["env_extra"]["CLAUDE_CONFIG_DIR"] == str(home_dir)


def test_limit_on_the_replay_too_stops_after_one_replay(accounts, launch):
    accounts.add("Home")
    accounts.add("Work")
    launch.script[:] = [limit_turn, limit_turn]
    result = launch.run()
    assert not result.ok and len(launch.calls) == 2
    assert "usage limit reached" in result.error


def test_no_replay_after_streamed_work(accounts, launch):
    accounts.add("Home")
    launch.script[:] = [lambda kw: limit_turn(kw, work=True)]
    result = launch.run()
    assert not result.ok and len(launch.calls) == 1
    assert "Account 1 is at its limit. Your next message will use Home." in result.error
    assert accounts.core.exhausted_until(AGENT, "default") == FUTURE


def test_model_prose_about_limits_never_marks(accounts, launch):
    accounts.add("Home")

    def failed_turn(kw):
        _init(kw)
        _line(kw, {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet", "content": [
            {"type": "text", "text": "When you've hit your limit, Ducky switches accounts."}],
            "usage": {"input_tokens": 9}}})
        _line(kw, {"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "sid-1",
                   "usage": {"input_tokens": 9}})
        return ProcResult(returncode=1)

    launch.script[:] = [failed_turn]
    result = launch.run()
    assert not result.ok and len(launch.calls) == 1
    assert accounts.core.exhausted_until(AGENT, "default") is None
    assert "next message" not in (result.error or "")


def test_one_account_keeps_the_env_and_marks_nothing(accounts, launch):
    launch.script[:] = [limit_turn]
    result = launch.run()
    assert not result.ok and len(launch.calls) == 1
    assert launch.calls[0]["env_extra"] == a.claude_env({"MOCK_ENV": "kept"})
    assert "next message" not in (result.error or "")
    assert accounts.core.exhausted_until(AGENT, "default") is None
    assert not any("at its limit" in t for t in _texts(launch))
