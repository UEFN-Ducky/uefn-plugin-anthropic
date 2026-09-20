from pathlib import Path
import json

def test_plugin_declares_complete():
    data = json.loads((Path(__file__).resolve().parent.parent / "plugin.json").read_text(encoding="utf-8"))
    ids = {n["id"] for n in data["contributes"]["automations"]["nodes"]}
    assert "anthropic.complete" in ids
    assert "brainrot" not in Path(__file__).with_name("graph_nodes.py").read_text(encoding="utf-8")
