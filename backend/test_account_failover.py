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
acc = importlib.import_module(pkg.__name__ + ".accounts")
from backend.agent.coding_agents.proc_exec import ProcResult

FUTURE = 2_000_000_000.0


@pytest.fixture(autouse=True)
def accounts(tmp_path, monkeypatch):
    """Both account folders and the state file live in tmp; no network, no CLI."""
    monkeypatch.delenv("DUCKY_CLAUDE_FAILOVER", raising=False)
    dirs = {1: tmp_path / "acc1", 2: tmp_path / "acc2"}
    for d in dirs.values():
        d.mkdir()
    ns = types.SimpleNamespace(dirs=dirs, windows={1: None, 2: None}, enabled=True, logged_in=True,
                               state=tmp_path / "state.json", preferred=1)
    ns.login = lambda n=2: (dirs[n] / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": f"tok{n}"}}), encoding="utf-8")
    creds = lambda n: [dirs[int(n)] / ".credentials.json", dirs[int(n)] / "credentials.json"]
    monkeypatch.setattr(fo, "state_path", lambda: ns.state)
    monkeypatch.setattr(fo, "account_config_dir", lambda n: dirs[int(n)])
    monkeypatch.setattr(fo, "account_credentials_paths", creds)
    monkeypatch.setattr(fo, "failover_enabled", lambda: ns.enabled)
    monkeypatch.setattr(fo, "preferred_account", lambda: ns.preferred)
    monkeypatch.setattr(fo, "account_env", lambda n: {})
    monkeypatch.setattr(fo, "_windows", lambda n, max_age=60.0: ns.windows[n])
    monkeypatch.setattr(fo, "logged_in", lambda n, cli_path="": n == 1 or ns.logged_in)
    monkeypatch.setattr(u, "account_credentials_paths", creds)
    monkeypatch.setattr(a, "account_env", lambda n: {} if n == 1 else {"CLAUDE_CONFIG_DIR": str(dirs[2])})
    fo._CHECKED.clear()
    fo._LOGIN.clear()
    return ns


def _row(wid: str, used: float, resets_at: float | None = FUTURE) -> dict:
    row = {"id": wid, "label": wid, "used": used, "limit": 100.0}
    if resets_at:
        row["resets_at"] = resets_at
    return row


# ── pick_account ────────────────────────────────────────────────────────


def test_pick_stays_on_account_1_without_account_2(accounts):
    accounts.windows[1] = [_row("hourly", 100)]
    assert fo.pick_account() == 1
    assert not accounts.state.exists()


def test_pick_stays_on_account_1_when_disabled(accounts):
    accounts.login()
    accounts.enabled = False
    accounts.windows[1] = [_row("hourly", 100)]
    assert fo.pick_account() == 1


def test_pick_moves_to_account_2_when_account_1_is_full(accounts):
    accounts.login()
    accounts.windows[1] = [_row("hourly", 40), _row("weekly", 100, FUTURE)]
    accounts.windows[2] = [_row("hourly", 10)]
    assert fo.pick_account() == 2
    assert fo.exhausted_until(1) == FUTURE
    # Saved state answers next time, without a usage check.
    accounts.windows[1] = None
    assert fo.pick_account() == 2


def test_pick_needs_account_2_logged_in(accounts):
    accounts.login()
    accounts.logged_in = False
    accounts.windows[1] = [_row("hourly", 100)]
    assert fo.pick_account() == 1


def test_pick_both_full_takes_the_earliest_reset(accounts):
    accounts.login()
    accounts.windows[1] = [_row("hourly", 100, FUTURE + 500)]
    accounts.windows[2] = [_row("weekly", 100, FUTURE + 100)]
    assert fo.pick_account() == 2
    fo.mark_exhausted(2, FUTURE + 900)
    assert fo.pick_account() == 1


def test_pick_unknown_usage_keeps_account_1(accounts):
    accounts.login()
    accounts.windows[1] = None
    assert fo.pick_account() == 1


def test_pick_goes_back_to_account_1_once_its_reset_passed(accounts):
    accounts.login()
    fo.mark_exhausted(1, time.time() + 60)
    assert fo.pick_account() == 2
    state = json.loads(accounts.state.read_text(encoding="utf-8"))
    state["accounts"]["1"]["exhausted_until"] = time.time() - 1
    accounts.state.write_text(json.dumps(state), encoding="utf-8")
    # Cached bars from before the reset still say 100%: their reset time has passed.
    accounts.windows[1] = [_row("hourly", 100, time.time() - 5)]
    assert fo.pick_account() == 1


def test_pick_never_raises_and_corrupt_state_is_ignored(accounts, monkeypatch):
    accounts.login()
    accounts.state.write_text("{not json", encoding="utf-8")
    accounts.windows[1] = [_row("hourly", 20)]
    assert fo.pick_account() == 1
    monkeypatch.setattr(fo, "account_full", Mock(side_effect=RuntimeError("boom")))
    assert fo.pick_account() == 1


def test_picked_account_2_runs_first_and_falls_back_to_account_1(accounts):
    accounts.preferred = 2
    assert fo.pick_account() == 1  # no login for account 2 yet
    accounts.login()
    accounts.windows[2] = [_row("hourly", 10)]
    assert fo.pick_account() == 2 and fo.current_account() == 2
    accounts.windows[2] = [_row("weekly", 100, FUTURE)]
    fo._CHECKED.clear()
    assert fo.pick_account() == 1
    fo.set_active(1)
    assert fo.current_account() == 1


def test_picked_account_2_without_switching_stays_on_2(accounts):
    accounts.login()
    accounts.preferred, accounts.enabled = 2, False
    accounts.windows[2] = [_row("hourly", 100)]
    assert fo.in_play() and fo.pick_account() == 2


def test_meter_account_follows_saved_state(accounts):
    assert fo.current_account() == 1
    accounts.login()
    fo.mark_exhausted(1, time.time() + 600)
    fo.set_active(2)
    assert fo.current_account() == 2
    fo.set_active(1)
    assert fo.current_account() == 1


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


def test_limit_hit_without_reset_uses_usage_then_fallback(accounts):
    accounts.windows[1] = [_row("weekly", 100, FUTURE)]
    assert fo.note_limit_hit(1) == FUTURE
    accounts.windows[2] = None
    until = fo.note_limit_hit(2)
    assert abs(until - (time.time() + fo.FALLBACK_WAIT_S)) < 5


# ── sessions ────────────────────────────────────────────────────────────


def test_carry_session_copies_when_missing_or_older(accounts):
    src = accounts.dirs[1] / "projects" / "C--proj"
    src.mkdir(parents=True)
    (src / "sid-1.jsonl").write_text("one\n", encoding="utf-8")
    assert fo.carry_session("sid-1", 1, 2)
    dst = accounts.dirs[2] / "projects" / "C--proj" / "sid-1.jsonl"
    assert dst.read_text(encoding="utf-8") == "one\n"
    # Account 2 continued the chat: going back copies its newer transcript.
    dst.write_text("one\ntwo\n", encoding="utf-8")
    os.utime(dst, (time.time() + 10, time.time() + 10))
    assert fo.carry_session("sid-1", 2, 1)
    assert (src / "sid-1.jsonl").read_text(encoding="utf-8") == "one\ntwo\n"
    # An up-to-date copy is left alone.
    assert fo.carry_session("sid-1", 1, 2)
    assert dst.read_text(encoding="utf-8") == "one\ntwo\n"


def test_replay_carries_the_transcript_from_before_the_failed_turn(accounts):
    src = accounts.dirs[1] / "projects" / "C--proj"
    src.mkdir(parents=True)
    (src / "sid-1.jsonl").write_text("before\n", encoding="utf-8")
    before = fo.session_snapshot("sid-1", 1)
    (src / "sid-1.jsonl").write_text("before\nprompt\nlimit\n", encoding="utf-8")
    assert fo.carry_session("sid-1", 1, 2, upto=before)
    assert (accounts.dirs[2] / "projects" / "C--proj" / "sid-1.jsonl").read_text(encoding="utf-8") == "before\n"


def test_tests_stay_off_the_real_account_2(accounts, monkeypatch):
    accounts.login()
    monkeypatch.setenv("DUCKY_CLAUDE_FAILOVER", "off")
    assert not fo.in_play() and fo.pick_account() == 1


def test_carry_session_rejects_missing_and_bad_ids(accounts):
    assert not fo.carry_session("nope", 1, 2)
    assert not fo.carry_session("", 1, 2)
    assert not fo.carry_session("../evil", 1, 2)


# ── usage per account ───────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


def _reset_usage():
    u._LAST_WINDOWS, u._BLOCKED_UNTIL = [], 0.0
    u._ACCOUNT2_LAST, u._ACCOUNT2_BLOCKED_UNTIL = [], 0.0
    u._FETCHED_AT.clear()


def test_account_2_reads_only_its_own_credentials(accounts, monkeypatch):
    import httpx

    _reset_usage()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, **k: seen.append(headers["Authorization"]) or _Resp(
        200, {"five_hour": {"utilization": 1, "resets_at": "2033-05-18T03:33:20+00:00"}}))
    assert u.account_windows(2) is None and not seen
    accounts.login(2)
    rows = u.account_windows(2)
    assert seen == ["Bearer tok2"]
    assert rows[0]["used"] == 100 and rows[0]["resets_at"] == FUTURE
    u.account_windows(2)
    assert len(seen) == 1  # reused for a minute


def test_429_blocks_are_per_account(accounts, monkeypatch):
    import httpx

    _reset_usage()
    accounts.login(2)
    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok1"}))
    calls: list[str] = []

    def fake_get(url, headers=None, **k):
        calls.append(headers["Authorization"])
        if headers["Authorization"] == "Bearer tok1":
            return _Resp(429, headers={"retry-after": "600"})
        return _Resp(200, {"seven_day": {"utilization": 0.5, "resets_at": FUTURE}})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert u.account_windows(1, max_age=0) is None
    assert u._BLOCKED_UNTIL > time.time() and u._ACCOUNT2_BLOCKED_UNTIL == 0.0
    assert u.account_windows(2, max_age=0)[0]["used"] == 50
    u.account_windows(1, max_age=0)
    assert calls == ["Bearer tok1", "Bearer tok2"]


def test_refreshed_account_2_token_is_saved_to_its_file(accounts, monkeypatch):
    import httpx

    _reset_usage()
    path = accounts.dirs[2] / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old", "refreshToken": "r", "expiresAt": 1}}),
                    encoding="utf-8")
    monkeypatch.setattr(u, "_refresh_claude_oauth", lambda oauth: {**oauth, "accessToken": "new", "expiresAt": 9e12})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"five_hour": {"utilization": 0.1}}))
    assert u.account_windows(2, max_age=0)
    assert json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "new"


def test_meter_labels_account_2(accounts, monkeypatch):
    import httpx

    _reset_usage()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"five_hour": {"utilization": 0.2}}))
    monkeypatch.setattr(fo, "current_account", lambda: 2)
    accounts.login(2)
    out = u.fetch_usage("")
    assert out["windows"][0]["label"] == "Account 2 · 5-hour limit"
    assert out["switch"]["value"] == "1" and out["switch"]["rpc"] == "set_claude_account"
    assert [o["id"] for o in out["switch"]["options"]] == ["1", "2"]
    monkeypatch.setattr(fo, "current_account", lambda: 1)
    monkeypatch.setattr(u, "_load_claude_oauth", lambda: (None, {}, {"accessToken": "tok1"}))
    assert u.fetch_usage("")["windows"][0]["label"] == "Account 1 · 5-hour limit"
    (accounts.dirs[2] / ".credentials.json").unlink()
    out = u.fetch_usage("")
    assert out["windows"][0]["label"] == "5-hour limit" and "switch" not in out


def test_share_config_links_account_1_folders_and_copies_its_files(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    (one / "skills" / "verse").mkdir(parents=True)
    (one / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    (two / "agents").mkdir(parents=True)  # a real folder account 2 already has stays its own
    (one / "agents").mkdir()
    acc.share_config(one, two)
    assert (two / "skills" / "verse").is_dir()
    assert (one / "projects").is_dir() and (two / "projects").is_dir()
    (two / "projects" / "C--p").mkdir()
    assert (one / "projects" / "C--p").is_dir()  # same folder: sessions are shared
    assert not os.path.samefile(one / "agents", two / "agents")
    assert (two / "settings.json").read_text(encoding="utf-8") == '{"hooks": {}}'
    assert not (two / ".credentials.json").exists()
    acc.share_config(one, two)  # again: nothing breaks


def test_replay_on_shared_projects_drops_the_failed_attempt(accounts, tmp_path):
    shared = tmp_path / "shared" / "C--proj"
    shared.mkdir(parents=True)
    acc._link_dir(tmp_path / "shared", accounts.dirs[1] / "projects")
    acc._link_dir(tmp_path / "shared", accounts.dirs[2] / "projects")
    (shared / "sid-1.jsonl").write_text("before\n", encoding="utf-8")
    before = fo.session_snapshot("sid-1", 1)
    (shared / "sid-1.jsonl").write_text("before\nprompt\nlimit\n", encoding="utf-8")
    assert fo.carry_session("sid-1", 1, 2, upto=before)
    assert (shared / "sid-1.jsonl").read_text(encoding="utf-8") == "before\n"
    assert fo.carry_session("sid-1", 2, 1)
    assert (shared / "sid-1.jsonl").read_text(encoding="utf-8") == "before\n"


# ── adapter ─────────────────────────────────────────────────────────────


@pytest.fixture
def launch(tmp_path, monkeypatch):
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
                  env={"MOCK_ENV": "kept"}, push=events.append)

    def run(session=""):
        return a.ClaudeCodeAdapter().launch(**dict(kwargs, session_id=session))

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


def test_limit_error_retries_once_on_account_2(accounts, launch):
    accounts.login()
    accounts.windows[1] = [_row("hourly", 50)]
    accounts.windows[2] = [_row("hourly", 10)]
    session = accounts.dirs[1] / "projects" / "C--proj"
    session.mkdir(parents=True)
    (session / "prior.jsonl").write_text("{}\n", encoding="utf-8")
    launch.script[:] = [limit_turn, ok_turn]
    result = launch.run("prior")
    assert result.ok and result.reply_text == "answer"
    first, second = launch.calls
    assert "CLAUDE_CONFIG_DIR" not in first["env_extra"]
    assert second["env_extra"]["CLAUDE_CONFIG_DIR"] == str(accounts.dirs[2])
    assert second["env_extra"]["MOCK_ENV"] == "kept"
    assert second["argv"][second["argv"].index("--resume") + 1] == "prior"
    assert (accounts.dirs[2] / "projects" / "C--proj" / "prior.jsonl").is_file()
    assert fo.exhausted_until(1) == FUTURE
    assert any("Account 1 is at its limit — continuing on account 2" in str(e.get("text")) for e in launch.events)
    # The next message starts on account 2 straight away.
    launch.script[:] = [ok_turn]
    assert launch.run("sid-1").ok
    assert launch.calls[2]["env_extra"]["CLAUDE_CONFIG_DIR"] == str(accounts.dirs[2])


def test_limit_on_both_accounts_retries_only_once(accounts, launch):
    accounts.login()
    accounts.windows[1] = [_row("hourly", 50)]
    accounts.windows[2] = [_row("hourly", 10)]
    launch.script[:] = [limit_turn, limit_turn]
    result = launch.run()
    assert not result.ok and len(launch.calls) == 2
    assert "usage limit reached" in result.error


def test_no_retry_after_streamed_work(accounts, launch):
    accounts.login()
    accounts.windows[1] = [_row("hourly", 50)]
    accounts.windows[2] = [_row("hourly", 10)]
    launch.script[:] = [lambda kw: limit_turn(kw, work=True)]
    result = launch.run()
    assert not result.ok and len(launch.calls) == 1
    assert "Your next message will use account 2." in result.error
    assert fo.exhausted_until(1) == FUTURE


def test_model_prose_about_limits_is_not_a_limit(accounts, launch):
    accounts.login()
    accounts.windows[1] = [_row("hourly", 50)]

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
    assert fo.exhausted_until(1) == 0.0
    assert "next message" not in (result.error or "")


def test_picked_account_2_says_so_once(accounts, launch):
    accounts.login()
    accounts.preferred = 2
    accounts.windows[2] = [_row("hourly", 10)]
    launch.script[:] = [ok_turn, ok_turn]
    assert launch.run().ok and launch.run("sid-1").ok
    assert all(c["env_extra"]["CLAUDE_CONFIG_DIR"] == str(accounts.dirs[2]) for c in launch.calls)
    texts = [str(e.get("text")) for e in launch.events]
    assert texts.count("Running on account 2.") == 1
    assert not any("at its limit" in t for t in texts)


def test_account_1_env_unchanged_without_account_2(accounts, launch):
    launch.script[:] = [limit_turn]
    result = launch.run()
    assert not result.ok and len(launch.calls) == 1
    assert launch.calls[0]["env_extra"] == a.claude_env({"MOCK_ENV": "kept"})
    assert "next message" not in (result.error or "")
    assert not accounts.state.exists()
    assert not any("account 2" in str(e.get("text")) for e in launch.events)
