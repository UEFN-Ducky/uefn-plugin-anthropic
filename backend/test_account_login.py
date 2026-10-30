"""Account 2 login / status / logout from Settings (no real CLI, terminal or network)."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

# Collected first, this file can find this plugin's own `backend` package in place of
# the app's; drop it so `backend.agent...` resolves to the app (as test_anthropic_followup does).
_pkg = sys.modules.get("backend")
if _pkg is not None and Path(getattr(_pkg, "__file__", "") or ".").resolve().parent == Path(__file__).resolve().parent:
    for _k in [k for k in sys.modules if k == "backend" or k.startswith("backend.")]:
        del sys.modules[_k]

import account_rpc
import accounts
import claude_auth

_URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "secondary_config_dir", lambda: tmp_path / "claude-account-2")
    # Account 2's folder links account 1's: keep that off this PC's real ~/.claude.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account-1"))
    monkeypatch.setattr(claude_auth, "_PENDING_PATH", tmp_path / "auth_pending.json")
    monkeypatch.setattr(account_rpc, "_cli_path", lambda: "")
    monkeypatch.setattr(claude_auth, "resolve_claude_bin", lambda *_a, **_k: "claude.exe")
    monkeypatch.setattr(account_rpc.time, "sleep", lambda _s: None)
    yield tmp_path


def _statuses(monkeypatch, one: dict, two: dict) -> list[int]:
    calls: list[int] = []

    def fake(_cli: str = "", *, account: int = 1) -> dict:
        calls.append(account)
        return dict(two if account == 2 else one)

    monkeypatch.setattr(claude_auth, "claude_auth_status", fake)
    return calls


class _Session:
    def __init__(self, tail: str = "", alive: bool = True) -> None:
        self.tail, self.alive = tail, alive

    def is_alive(self) -> bool:
        return self.alive

    def read_output_tail(self, _n: int) -> str:
        return self.tail


class _Mgr:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.spawns: list[dict] = []
        self.killed: list[str] = []

    def spawn(self, **kw):
        self.spawns.append(kw)
        return {"ok": True, "session_id": "t2"}

    def get_session(self, sid: str):
        return self.session if sid == "t2" else None

    def kill(self, sid: str, *, push_close: bool = True):
        self.killed.append(sid)


def _wire(monkeypatch, mgr: _Mgr) -> None:
    monkeypatch.setattr(account_rpc, "_terminal_manager", lambda: mgr)
    monkeypatch.setattr(claude_auth, "_kill_login_session", lambda sid: sid and mgr.kill(sid))


def test_account_env_only_for_account_2(tmp_path):
    assert accounts.account_env(1) == {}
    env = accounts.account_env(2)
    assert env == {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-account-2")}
    assert (tmp_path / "claude-account-2").is_dir()


def test_status_passes_account_env(monkeypatch, tmp_path):
    seen: list[dict] = []

    def run(argv, **kw):
        seen.append(kw["env"])
        return types.SimpleNamespace(stdout='{"loggedIn": true, "email": "a@x.com"}', stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert claude_auth.claude_auth_status("", account=2)["email"] == "a@x.com"
    claude_auth.claude_auth_status("")
    assert seen[0]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-account-2")
    assert seen[1].get("CLAUDE_CONFIG_DIR") != str(tmp_path / "claude-account-2")


def test_status_detail(monkeypatch):
    _statuses(monkeypatch, {"loggedIn": True, "email": "one@x.com"},
              {"loggedIn": True, "email": "two@x.com", "subscriptionType": "max"})
    assert account_rpc.account2_status() == {"ok": True, "connected": True, "detail": "Account 2: two@x.com · Max"}


def test_status_not_logged_in(monkeypatch):
    _statuses(monkeypatch, {"loggedIn": True}, {"loggedIn": False})
    res = account_rpc.account2_status()
    assert res == {"ok": True, "connected": False, "detail": "Account 2 isn't logged in."}


def test_status_same_email_warns(monkeypatch):
    _statuses(monkeypatch, {"loggedIn": True, "email": "Me@x.com"}, {"loggedIn": True, "email": "me@x.com"})
    res = account_rpc.account2_status()
    assert res["connected"] is True and res["ok"] is False
    assert "same Claude account" in res["detail"]


def test_login_success_closes_tab(monkeypatch, tmp_path):
    mgr = _Mgr(_Session(tail=f"Open this URL:\r\n{_URL}\r\n"))
    _wire(monkeypatch, mgr)
    polls = iter([False, True])
    monkeypatch.setattr(claude_auth, "is_claude_logged_in", lambda *_a, account=1: account == 2 and next(polls))
    _statuses(monkeypatch, {"loggedIn": True, "email": "one@x.com"},
              {"loggedIn": True, "email": "two@x.com", "subscriptionType": "pro"})

    res = account_rpc.account2_login()

    assert res == {"ok": True, "connected": True, "detail": "Account 2: two@x.com · Pro"}
    spawn = mgr.spawns[0]
    assert spawn["title"] == "Claude Login (account 2)"
    assert spawn["conv_id"] == claude_auth.ACCOUNT2_LOGIN_CONV
    assert spawn["command"] == ["claude.exe", "auth", "login"]
    assert spawn["env_extra"] == {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-account-2")}
    assert "BROWSER" not in spawn["env_extra"]
    assert mgr.killed == ["t2"]
    assert claude_auth.get_pending_auth(claude_auth.ACCOUNT2_LOGIN_CONV) is None


def test_login_deadline_leaves_tab_open(monkeypatch):
    mgr = _Mgr(_Session(tail=f"Open this URL:\r\n{_URL}\r\n"))
    _wire(monkeypatch, mgr)
    monkeypatch.setattr(claude_auth, "is_claude_logged_in", lambda *_a, **_k: False)
    monkeypatch.setattr(account_rpc, "LOGIN_WAIT_S", 0.0)
    claude_auth.set_pending_auth(claude_auth.SETTINGS_CONV, {"terminal_session_id": "acct1"})

    res = account_rpc.account2_login()

    assert res["ok"] is True and res["connected"] is False
    assert "Claude Login (account 2)" in res["detail"] and _URL in res["detail"]
    assert mgr.killed == []
    assert claude_auth.get_pending_auth(claude_auth.ACCOUNT2_LOGIN_CONV)["auth_url"] == _URL
    # Account 1's pending login is never touched.
    assert claude_auth.get_pending_auth(claude_auth.SETTINGS_CONV) == {"terminal_session_id": "acct1"}


def test_login_tab_closed_is_an_error(monkeypatch):
    mgr = _Mgr(_Session(alive=False))
    _wire(monkeypatch, mgr)
    monkeypatch.setattr(claude_auth, "is_claude_logged_in", lambda *_a, **_k: False)
    res = account_rpc.account2_login()
    assert res["ok"] is False and "closed" in res["error"]


def test_login_kills_previous_account2_tab(monkeypatch):
    mgr = _Mgr(_Session())
    _wire(monkeypatch, mgr)
    monkeypatch.setattr(claude_auth, "is_claude_logged_in", lambda *_a, **_k: False)
    monkeypatch.setattr(account_rpc, "LOGIN_WAIT_S", 0.0)
    claude_auth.set_pending_auth(claude_auth.ACCOUNT2_LOGIN_CONV, {"terminal_session_id": "old"})
    account_rpc.account2_login()
    assert mgr.killed == ["old"]


def test_login_again_keeps_waiting_on_open_tab(monkeypatch):
    mgr = _Mgr(_Session())
    _wire(monkeypatch, mgr)
    monkeypatch.setattr(claude_auth, "is_claude_logged_in", lambda *_a, **_k: False)
    monkeypatch.setattr(account_rpc, "LOGIN_WAIT_S", 0.0)
    claude_auth.set_pending_auth(claude_auth.ACCOUNT2_LOGIN_CONV, {"terminal_session_id": "t2", "auth_url": _URL})

    res = account_rpc.account2_login()

    assert mgr.spawns == [] and mgr.killed == []
    assert _URL in res["detail"]


def test_login_when_already_logged_in_opens_nothing(monkeypatch):
    mgr = _Mgr(_Session())
    _wire(monkeypatch, mgr)
    _statuses(monkeypatch, {"loggedIn": True, "email": "a@x.com"}, {"loggedIn": True, "email": "b@x.com"})

    res = account_rpc.account2_login()

    assert res["connected"] is True and mgr.spawns == []


def test_login_without_cli(monkeypatch):
    monkeypatch.setattr(claude_auth, "resolve_claude_bin", lambda *_a, **_k: "")
    res = account_rpc.account2_login()
    assert res["ok"] is False and res["error"]


def _fake_logout(monkeypatch, logged_in_after: bool = False) -> list[dict]:
    seen: list[dict] = []

    def run(argv, **kw):
        seen.append({"argv": argv, "env": kw["env"]})
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(claude_auth, "is_claude_logged_in", lambda *_a, **_k: logged_in_after)
    return seen


def test_logout_uses_account2_env_and_keeps_account1_pending(monkeypatch, tmp_path):
    seen = _fake_logout(monkeypatch)
    _wire(monkeypatch, _Mgr(_Session()))
    claude_auth.set_pending_auth(claude_auth.SETTINGS_CONV, {"deferred_prompt": "hi"})
    claude_auth.set_pending_auth(claude_auth.ACCOUNT2_LOGIN_CONV, {"terminal_session_id": "t2"})

    res = account_rpc.account2_logout()

    assert res == {"ok": True, "connected": False, "detail": "Logged out of account 2."}
    assert seen[0]["argv"] == ["claude.exe", "auth", "logout"]
    assert seen[0]["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-account-2")
    assert claude_auth.get_pending_auth(claude_auth.SETTINGS_CONV) == {"deferred_prompt": "hi"}
    assert claude_auth.get_pending_auth(claude_auth.ACCOUNT2_LOGIN_CONV) is None


def test_account1_logout_unchanged(monkeypatch, tmp_path):
    seen = _fake_logout(monkeypatch)
    claude_auth.set_pending_auth(claude_auth.SETTINGS_CONV, {"deferred_prompt": "hi"})
    res = claude_auth.claude_logout("")
    assert res == {"ok": True, "logged_in": False, "message": "Logged out of Claude Code."}
    assert "claude-account-2" not in seen[0]["env"].get("CLAUDE_CONFIG_DIR", "")
    assert not (tmp_path / "auth_pending.json").exists()


def test_account1_login_tab_unchanged():
    mgr = _Mgr(_Session())
    claude_auth.spawn_login_session(mgr, ".", claude_auth.SETTINGS_CONV, "claude.exe")
    spawn = mgr.spawns[0]
    assert spawn["title"] == "Claude Login"
    assert spawn["env_extra"] == {"BROWSER": claude_auth.LOGIN_NO_BROWSER}


def test_register_account_rpcs():
    names: list[str] = []
    api = types.SimpleNamespace(register_panel_rpc=lambda name, fn: names.append(name))
    account_rpc.register_account_rpcs(api)
    assert names == ["account2_login", "account2_status", "account2_logout", "set_claude_account"]


def test_set_claude_account_keeps_the_other_prefs(monkeypatch):
    from frontend.ui_web import plugin_host_api

    saved = {"anthropic": {"accountFailover": False}}
    monkeypatch.setattr(plugin_host_api, "prefs_plugin_get", lambda pid: dict(saved.get(pid, {})))
    monkeypatch.setattr(plugin_host_api, "prefs_plugin_set", lambda pid, prefs: saved.__setitem__(pid, prefs))
    res = account_rpc.set_claude_account(account="2")
    assert res["ok"] and res["value"] == "2"
    assert saved["anthropic"] == {"accountFailover": False, "claudeAccount": "2"}
    assert accounts.preferred_account() == 2
    account_rpc.set_claude_account(account="junk")
    assert accounts.preferred_account() == 1
