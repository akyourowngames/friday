"""Focused regression coverage for OpenCode Zen and NVIDIA NIM routing."""

import asyncio

from ares.llm import (
    LLMClient,
    PROVIDER_BASE_URLS,
    activate_provider_config,
    configured_provider_api_key,
    provider_for_model,
    resolve_provider_base_url,
)
from ares.models import AppConfig


def test_registered_models_select_their_actual_transport_provider():
    assert provider_for_model("mimo-v2.5-free") == "opencode"
    assert provider_for_model("deepseek-ai/deepseek-v4-flash") == "nim"
    assert provider_for_model("custom/local-model") is None


def test_provider_switch_preserves_keys_and_never_reuses_the_nim_url_for_opencode(monkeypatch):
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    config = AppConfig(
        provider="nvidia",  # legacy spelling remains accepted
        model="deepseek-ai/deepseek-v4-flash",
        api_key="nvapi-test-key",
        api_base_url=PROVIDER_BASE_URLS["nim"],
    )

    activate_provider_config(config, "opencode")

    assert config.provider == "opencode"
    assert config.api_base_url == PROVIDER_BASE_URLS["opencode"]
    assert config.api_key == ""
    assert config.provider_api_keys == {"nim": "nvapi-test-key"}
    assert resolve_provider_base_url("opencode", PROVIDER_BASE_URLS["nim"]) == PROVIDER_BASE_URLS["opencode"]

    activate_provider_config(config, "nim")

    assert config.provider == "nim"
    assert config.api_key == "nvapi-test-key"
    assert configured_provider_api_key(config, "nim") == "nvapi-test-key"


def test_llm_client_uses_the_configured_provider_endpoint_and_matching_key_only(monkeypatch):
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    config = AppConfig(
        provider="nvidia",
        model="deepseek-v4-flash-free",
        api_base_url=PROVIDER_BASE_URLS["nim"],  # stale legacy endpoint
        api_key="nvapi-test-key",
    )

    client = LLMClient(config=config)
    try:
        assert client.provider == "opencode"
        assert client.base_url == PROVIDER_BASE_URLS["opencode"]
        assert client.api_key == ""
    finally:
        asyncio.run(client.close())
