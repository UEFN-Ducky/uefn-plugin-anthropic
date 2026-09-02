from __future__ import annotations

from model_fetch import canonical_model_id, parse_active_model_ids

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
