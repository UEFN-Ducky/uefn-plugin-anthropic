"""Anthropic model records report image/video/audio limits only when present."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parents[1]
_APP = None
for _p in ROOT.parents:
    for _name in ("UEFN-Ducky-Release", "UEFN-Ducky-video", "UEFN-Ducky"):
        _app = _p / _name / "ducky_app"
        if (_app / "backend" / "agent").is_dir():
            _APP = str(_app)
            break
    else:
        continue
    break


def _load():
    # The plugin dir is itself a ``backend`` package; let the host's win while importing.
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "backend" or k.startswith("backend.")}
    saved_path = list(sys.path)
    sys.path[:] = [_APP] + [p for p in sys.path if os.path.abspath(p) != str(ROOT)]
    try:
        return _load_module()
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if k == "backend" or k.startswith("backend.")]:
            del sys.modules[k]
        sys.modules.update(saved)


def _load_module():
    pkg = types.ModuleType("anthropic_gw")
    pkg.__path__ = [str(ROOT / "backend")]
    sys.modules["anthropic_gw"] = pkg
    prov = types.ModuleType("anthropic_gw.anthropic_provider")  # heavy; only BUDGET is needed
    prov.BUDGET = {"low": 1024, "medium": 4096, "high": 16000}
    sys.modules["anthropic_gw.anthropic_provider"] = prov
    spec = importlib.util.spec_from_file_location("anthropic_gw.model_fetch", ROOT / "backend" / "model_fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _info(caps):
    return _load()._anthropic_info_from_item({"id": "claude-x", "capabilities": caps})


def test_media_fields_from_capabilities():
    info = _info(
        {
            "image_input": {"supported": True, "max_images": 20},
            "video_input": {"supported": True},
            "audio_input": {"supported": False},
        }
    )
    assert (info.max_images, info.supports_video, info.supports_audio) == (20, True, False)


def test_max_images_key_fallbacks():
    assert _info({"image_input": {"supported": True, "max_count": 5}}).max_images == 5
    assert _info({"image_input": {"supported": True, "max_images_per_request": 8}}).max_images == 8


def test_media_fields_unknown_when_absent():
    info = _info({"image_input": {"supported": True}})
    assert (info.max_images, info.supports_video, info.supports_audio) == (None, None, None)
