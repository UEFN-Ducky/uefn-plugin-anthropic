"""Pure-function checks for the chat-driven Claude login (no terminal, no CLI)."""

from __future__ import annotations

from claude_auth import (
    SETTINGS_LOGIN_HREF,
    _logout_argv,
    _rejection_line,
    extract_auth_url,
    looks_like_auth_code,
    settings_login_message,
    spawn_login_session,
    submit_claude_login_code,
    tail_says_logged_in,
)

_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&state=gcqp3x991kcfSlwLT5qn_-XtWcbXGLjnk_9Y45Wi-zQ"
)
# What `claude auth login` really prints: OSC-8 hyperlink + blue color codes.
_TERMINAL = f"Open this URL:\r\n\x1b]8;;{_URL}\x07\x1b[94m{_URL}\x1b[39m\x1b]8;;\x07\r\nPaste code here if prompted >"


def test_url_is_clean_of_ansi():
    assert extract_auth_url(_TERMINAL) == _URL
    assert extract_auth_url("nothing here") == ""


def test_code_hash_state_is_a_code():
    assert looks_like_auth_code("svVbMkUV0dJFzHV9KX_M3Ln9jPd5dTsL#gcqp3x991kcfSlwLT5qn")
    assert looks_like_auth_code("`abcdefghij#state`")
    assert looks_like_auth_code("abc123#https://claude.com/cai?code=true")


def test_words_and_sentences_are_not_codes():
    for s in ("done", "DONE", "ok", "continue", "short", "paste the code here", "a b"):
        assert not looks_like_auth_code(s), s


def test_rejection_is_claudes_line_or_empty():
    assert _rejection_line("\x1b[31mError: Invalid authorization code\x1b[39m\r\n") == (
        "Error: Invalid authorization code"
    )
    assert _rejection_line("Exchanging code…\r\n") == ""
    assert _rejection_line("") == ""


def test_logout_argv():
    assert _logout_argv(r"C:\claude.exe") == [r"C:\claude.exe", "auth", "logout"]


def test_chat_points_at_settings_not_codes():
    msg = settings_login_message()
    assert SETTINGS_LOGIN_HREF in msg
    assert "Do not paste a code" in msg
    assert "Settings → LLMs → Anthropic" in msg
    assert "sign-in link" in msg
    assert "box for the code" in msg
    assert "terminal" not in msg.lower()


def test_tail_logged_in():
    assert tail_says_logged_in("Logged in as  you@anthropic.com")
    assert not tail_says_logged_in("Not logged in")
    assert not tail_says_logged_in("Paste code here")


def test_spawn_login_skips_hidden_on_old_manager():
    class Old:
        def spawn(self, shell, cwd, title, push_open=False, conv_id=""):
            return {"ok": True, "via": "old"}

    class New:
        def spawn(self, shell, cwd, title, push_open=False, hidden=False, conv_id=""):
            return {"ok": True, "via": "new", "hidden": hidden}

    assert spawn_login_session(New(), ".", "__settings__") == {
        "ok": True,
        "via": "new",
        "hidden": True,
    }
    assert spawn_login_session(Old(), ".", "__settings__")["via"] == "old"


def test_submit_rejects_junk_without_a_session():
    bad = submit_claude_login_code(code="not a code", conv_id="__test_no_session__")
    assert bad["ok"] is False
    missing = submit_claude_login_code(
        code="svVbMkUV0dJFzHV9KX_M3Ln9jPd5dTsL#gcqp3x991kcfSlwLT5qn",
        conv_id="__test_no_session__",
    )
    assert missing["ok"] is False
    assert "Press Log in" in str(missing.get("error") or "")


if __name__ == "__main__":
    test_url_is_clean_of_ansi()
    test_code_hash_state_is_a_code()
    test_words_and_sentences_are_not_codes()
    test_rejection_is_claudes_line_or_empty()
    test_logout_argv()
    test_chat_points_at_settings_not_codes()
    test_tail_logged_in()
    test_spawn_login_skips_hidden_on_old_manager()
    test_submit_rejects_junk_without_a_session()
    print("ok")
