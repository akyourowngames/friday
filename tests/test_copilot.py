import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from ares.integrations.llm import (
    LLMClient,
    activate_provider_config,
    configured_provider_api_key,
    default_model_for_provider,
    provider_for_model,
    resolve_provider_base_url,
)
from ares.models import AppConfig
from ares.integrations import copilot_oauth
from ares.integrations.copilot_oauth import authorization_url


class FakeSession:
    def __init__(self):
        self.handler = None
        self.prompts = []
        self.disconnected = False

    def on(self, handler):
        self.handler = handler
        return lambda: setattr(self, "handler", None)

    async def send_and_wait(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        if self.handler:
            self.handler(SimpleNamespace(
                type="assistant.message_delta",
                data=SimpleNamespace(delta_content="Hello from Copilot"),
            ))
        return SimpleNamespace(data=SimpleNamespace(content="Hello from Copilot"))

    async def disconnect(self):
        self.disconnected = True


class FakeCopilotClient:
    instances = []

    def __init__(self, **options):
        self.options = options
        self.started = False
        self.stopped = False
        self.sessions = []
        self.__class__.instances.append(self)

    async def start(self):
        self.started = True

    async def create_session(self, **kwargs):
        self.session_options = kwargs
        session = FakeSession()
        self.sessions.append(session)
        return session

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_copilot_sdk(monkeypatch):
    FakeCopilotClient.instances.clear()
    module = ModuleType("copilot")
    module.CopilotClient = FakeCopilotClient
    monkeypatch.setitem(sys.modules, "copilot", module)


def test_copilot_provider_is_sdk_backed_and_uses_auto_model():
    config = AppConfig(provider="copilot", model="auto", copilot_github_token="gho_example")

    assert provider_for_model("auto") == "copilot"
    assert default_model_for_provider("copilot") == "auto"
    assert resolve_provider_base_url("copilot", "https://opencode.ai/zen/v1") == ""
    assert configured_provider_api_key(config, "copilot") == "gho_example"


def test_github_oauth_link_includes_the_registered_callback_and_state():
    url = authorization_url("client-id", "http://127.0.0.1:8765/copilot/oauth/callback", "state-value")
    query = parse_qs(urlparse(url).query)

    assert query == {
        "client_id": ["client-id"],
        "redirect_uri": ["http://127.0.0.1:8765/copilot/oauth/callback"],
        "state": ["state-value"],
    }


def test_device_flow_prints_a_code_then_saves_the_authorized_token(monkeypatch):
    calls = []
    seen = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        if url.endswith("/device/code"):
            return Response({
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 60,
                "interval": 1,
            })
        if len([call for call in calls if call[0].endswith("/access_token")]) == 1:
            return Response({"error": "authorization_pending"})
        return Response({"access_token": "gho_saved"})

    monkeypatch.setattr(copilot_oauth.httpx, "post", fake_post)
    monkeypatch.setattr(copilot_oauth.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(copilot_oauth.webbrowser, "open", lambda _url, **_kwargs: True)

    token = copilot_oauth.authorize_github_device_flow(
        client_id="client-id", on_device_code=seen.append
    )

    assert token.access_token == "gho_saved"
    assert seen[0].user_code == "ABCD-EFGH"
    assert seen[0].verification_uri == "https://github.com/login/device"
    assert calls[1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"


@pytest.mark.asyncio
async def test_copilot_adapter_uses_only_explicit_token_and_streams(fake_copilot_sdk):
    config = AppConfig(provider="copilot", model="auto", copilot_github_token="gho_example")
    client = LLMClient(config=config)
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]

    response = await client.chat([{"role": "user", "content": "Read notes.txt"}], tools=tools)
    chunks = [chunk async for chunk in client.chat_stream([{"role": "user", "content": "Say hi"}], tools=tools)]

    instance = FakeCopilotClient.instances[0]
    assert response == {"content": "Hello from Copilot"}
    assert instance.options == {"github_token": "gho_example", "use_logged_in_user": False}
    assert instance.session_options["available_tools"] == []
    assert "<tool_name>{JSON object}</tool_name>" in instance.sessions[0].prompts[0]
    assert chunks == [{"type": "content", "text": "Hello from Copilot"}, {"type": "done"}]
    await client.close()
    assert instance.stopped is True


@pytest.mark.asyncio
async def test_copilot_adapter_surfaces_a_quota_error_without_a_timeout(
    fake_copilot_sdk, monkeypatch
):
    class QuotaSession(FakeSession):
        async def send_and_wait(self, prompt, **_kwargs):
            self.prompts.append(prompt)
            if self.handler:
                self.handler(SimpleNamespace(
                    type="session.error",
                    data=SimpleNamespace(message="You have exceeded your monthly quota"),
                ))
            raise TimeoutError("Timeout after 60s waiting for session.idle")

    async def create_quota_session(self, **kwargs):
        self.session_options = kwargs
        session = QuotaSession()
        self.sessions.append(session)
        return session

    monkeypatch.setattr(FakeCopilotClient, "create_session", create_quota_session)
    client = LLMClient(config=AppConfig(
        provider="copilot", model="auto", copilot_github_token="gho_example"
    ))

    with pytest.raises(Exception, match="quota is exhausted"):
        await client.chat([{"role": "user", "content": "Hello"}])
    with pytest.raises(Exception, match="quota is exhausted"):
        _ = [chunk async for chunk in client.chat_stream([
            {"role": "user", "content": "Hello again"}
        ])]

    await client.close()


def test_switching_to_copilot_does_not_copy_oauth_token_into_legacy_api_key():
    config = AppConfig(api_key="zen-secret", provider="opencode", copilot_github_token="gho_example")

    assert activate_provider_config(config, "copilot") == "copilot"
    assert config.api_key == ""
    assert config.api_base_url == ""
    assert config.provider_api_keys["opencode"] == "zen-secret"
