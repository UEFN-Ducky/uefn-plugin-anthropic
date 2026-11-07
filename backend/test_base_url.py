from __future__ import annotations

import sys
import types
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

import model_fetch
from anthropic_provider import AnthropicProvider


def test_api_root_takes_a_proxy_with_or_without_v1():
    assert model_fetch.api_root("") == "https://api.anthropic.com"
    assert model_fetch.api_root("https://proxy.example/v1/") == "https://proxy.example"
    assert model_fetch.api_root("https://proxy.example/anthropic") == "https://proxy.example/anthropic"


def test_client_and_model_list_use_the_base_url(monkeypatch):
    made: list = []
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda **kw: made.append(kw)))
    AnthropicProvider("sk", "m")._client()
    AnthropicProvider("sk", "m", base_url="https://proxy.example/v1")._client()
    assert made == [{"api_key": "sk"}, {"api_key": "sk", "base_url": "https://proxy.example"}]

    asked: list = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [], "has_more": False}

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(get=lambda url, **_k: asked.append(url) or Resp()))
    monkeypatch.setattr(model_fetch, "_pricing_catalog", lambda: {})
    model_fetch.fetch_models("sk", base_url="https://proxy.example/v1")
    model_fetch.fetch_models("sk")
    assert asked == ["https://proxy.example/v1/models", "https://api.anthropic.com/v1/models"]
