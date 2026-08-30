from __future__ import annotations

from pathlib import Path

src = Path(__file__).with_name("claude_code_adapter.py").read_text(encoding="utf-8")


def test_followup_passes_resume_flag():
    assert "argv.extend([\"--resume\", session_id])" in src
    assert "resume=True" in src


def test_first_turn_only_resumes_when_id_set():
    assert "if session_id:" in src
    assert "argv.extend([\"--resume\", session_id])" in src
