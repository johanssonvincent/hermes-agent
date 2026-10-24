"""Explicit Codex context variants honor published maxima after default-window changes."""

import base64
import json
from unittest.mock import MagicMock

import pytest

from agent import model_metadata as mm


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6.1-sol-900k", "openai/gpt-6.1-sol-900k"])
@pytest.mark.parametrize(
    "advertised,catalog_max,extended",
    [
        (500_000, 872_000, 872_000),
        (500_000, 1_050_000, 900_000),
        (372_000, 600_000, 600_000),
        (200_000, 200_000, 200_000),
    ],
)
def test_catalog_max_controls_only_explicit_variant(
    model, advertised, catalog_max, extended, monkeypatch, tmp_path
):
    cache_path = tmp_path / "context_length_cache.yaml"
    monkeypatch.setattr(mm, "_get_context_cache_path", lambda: cache_path)
    monkeypatch.setattr(mm, "_codex_oauth_context_cache", {})
    monkeypatch.setattr(mm, "_codex_oauth_max_context_cache", {})
    base_url = "https://chatgpt.com/backend-api/codex"
    mm.save_context_length(model, base_url, advertised)

    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "context-test"}).encode()).rstrip(b"=").decode()
    token = f"{header}.{payload}.test-signature"
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "models": [{
            "slug": "gpt-6.1-sol",
            "context_window": advertised,
            "max_context_window": catalog_max,
        }]
    }
    get = MagicMock(return_value=response)
    monkeypatch.setattr(mm.model_metadata_http, "get", get)
    expected = extended if model.endswith("-900k") else advertised

    assert mm.get_model_context_length(
        model=model, base_url=base_url, api_key=token, provider="openai-codex"
    ) == expected
    assert mm.get_cached_context_length(model, base_url) == expected
    assert mm.get_model_context_length(
        model=model, base_url=base_url, api_key=token, provider="openai-codex"
    ) == expected
    get.assert_called_once()
