"""Pure-function checks for the chat-driven Claude login (no terminal, no CLI)."""

from __future__ import annotations

from claude_auth import _rejection_line, extract_auth_url, looks_like_auth_code

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


if __name__ == "__main__":
    test_url_is_clean_of_ansi()
    test_code_hash_state_is_a_code()
    test_words_and_sentences_are_not_codes()
    test_rejection_is_claudes_line_or_empty()
    print("ok")
