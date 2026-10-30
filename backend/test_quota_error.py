"""API-key mode: which Anthropic errors move a turn to the next key."""

from __future__ import annotations

import pytest

from anthropic_provider import is_quota_error


@pytest.mark.parametrize("text", [
    "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error'}}",
    "Error code: 400 - Your credit balance is too low to access the Anthropic API.",
    "insufficient_quota",
])
def test_quota_errors_switch_key(text):
    assert is_quota_error(text)


@pytest.mark.parametrize("text", [
    "Error code: 529 - overloaded_error",
    "Error code: 401 - invalid x-api-key",
    "prompt is too long",
    "",
])
def test_other_errors_do_not(text):
    assert not is_quota_error(text)
