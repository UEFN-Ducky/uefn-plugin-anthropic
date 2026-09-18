from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for k in list(sys.modules):
    if k == "backend" or k.startswith("backend."):
        del sys.modules[k]
for p in _here.parents:
    cand = p / "UEFN-Ducky-Release" / "ducky_app"
    if (cand / "backend" / "agent").is_dir():
        sys.path.insert(0, str(_here))
        sys.path.insert(0, str(cand))
        break

from model_fetch import anthropic_supports_thinking, anthropic_thinking_menu, canonical_model_id, parse_active_model_ids

_HTML = """
<table><tbody>
<tr><td><button aria-label="Copy model ID claude-fable-5-1">claude-fable-5-1</button></td><td>Active</td></tr>
<tr><td><button aria-label="Copy model ID claude-opus-4-1-20250805">x</button></td><td>Retired</td></tr>
<tr><td><button aria-label="Copy model ID claude-haiku-4-5-20251001">x</button></td><td>Active</td></tr>
</tbody></table>
"""


def test_parse_skips_retired_and_strips_date():
    ids = parse_active_model_ids(_HTML)
    assert ids == ["claude-fable-5-1", "claude-haiku-4-5"]


def test_canonical_drops_snapshot_date():
    assert canonical_model_id("claude-opus-4-5-20251101") == "claude-opus-4-5"
    assert canonical_model_id("claude-fable-5-1") == "claude-fable-5-1"


def test_thinking_flag():
    assert anthropic_supports_thinking("claude-sonnet-4-5")
    assert anthropic_supports_thinking("claude-3-7-sonnet-latest")
    assert not anthropic_supports_thinking("claude-3-5-sonnet-latest")
    assert not anthropic_supports_thinking("claude-3-opus-20240229")
    menu = anthropic_thinking_menu("claude-sonnet-4-5")
    assert menu and menu["levels"][0]["id"] == "off"
    assert menu["levels"][1]["thinking_tokens"] == 2048
    assert anthropic_thinking_menu("claude-3-5-sonnet-latest") is None
