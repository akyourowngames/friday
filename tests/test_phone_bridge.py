import json

from ares.models import AppConfig, PhoneConfig
from ares.memory_extractor import MemoryExtractor
from ares.tools.executor import ToolExecutor


class Store:
    def __init__(self):
        self.facts = []
    def store(self, fact_text, **kwargs):
        self.facts.append((fact_text, kwargs))
        return len(self.facts)


class LLM:
    def __init__(self):
        self.seen = ""
    async def chat(self, messages, tools=None):
        self.seen = messages[0]["content"]
        return {"content": "[]"}


def test_phone_config_defaults():
    cfg = AppConfig()
    assert isinstance(cfg.phone, PhoneConfig)
    assert cfg.phone.enabled is False
    assert cfg.phone.store_notification_content is False


def test_phone_call_requires_confirm():
    executor = ToolExecutor(Store(), config=AppConfig(phone=PhoneConfig(enabled=True)))
    payload = json.loads(executor.execute("phone_call_number", {"number": "+15555550123"}))
    assert payload["confirm_required"] is True
    assert payload["dialed"] is False


def test_memory_extractor_filters_private_phone_tool_output():
    llm = LLM()
    extractor = MemoryExtractor(llm, Store(), config=AppConfig())
    history = [
        {"role": "user", "content": "check notifications"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "phone_get_notifications"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Bank code 123456"},
        {"role": "user", "content": "I prefer tea"},
    ]
    extractor.extract_and_store(history)
    assert "Bank code" not in llm.seen
    assert "I prefer tea" in llm.seen


def test_memory_extractor_can_keep_phone_output_when_enabled():
    llm = LLM()
    cfg = AppConfig(phone=PhoneConfig(store_notification_content=True))
    extractor = MemoryExtractor(llm, Store(), config=cfg)
    history = [
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "phone_get_notifications"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Bank code 123456"},
        {"role": "user", "content": "remember I like guarded tests"},
    ]
    extractor.extract_and_store(history)
    # Tool output is retained in the filtered history when enabled, even though only user text is summarized today.
    assert extractor._filter_private_phone_tool_output(history) == history


def test_phone_launch_app_requires_package():
    """phone_launch_app requires a package name."""
    executor = ToolExecutor(Store(), config=AppConfig(phone=PhoneConfig(enabled=True)))
    result = executor.execute("phone_launch_app", {})
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "package" in payload["error"].lower()


def test_open_url_requires_url():
    """phone_open_url requires a URL."""
    executor = ToolExecutor(Store(), config=AppConfig(phone=PhoneConfig(enabled=True)))
    result = executor.execute("phone_open_url", {})
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "url" in payload["error"].lower()
