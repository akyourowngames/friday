import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import ares.channels.telegram as telegram_module
from ares.channels.store import ChannelStore
from ares.channels.telegram import TelegramChannel, _split_message, run_telegram_channel
from ares.channels.audio import EnglishAudioTranscriber, EnglishTranscript
from ares.models import AppConfig, TelegramConfig


class FakeConversationStore:
    def __init__(self):
        self.messages = []
        self.started = 0
        self.ended = []

    def start_conversation(self):
        self.started += 1
        return self.started

    def end_conversation(self, conversation_id):
        self.ended.append(conversation_id)

    def get_messages(self, conversation_id):
        return [message for message in self.messages if message["conversation_id"] == conversation_id]

    def add_message(self, conversation_id, role, content, tool_calls=None):
        self.messages.append(
            {
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
            }
        )


class FakeAgent:
    def __init__(self, config):
        self.config = config
        self.prompts = []

    async def run_stream(self, prompt, conversation_history):
        self.prompts.append((prompt, conversation_history))
        yield "[tool_start:web_search]"
        yield '[tool:web_search:{"query":"ares"}]'
        yield "Here is the answer."


class FakeTelegramAPI:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.actions = []
        self.documents = []
        self.downloads = []

    async def send_message(self, chat_id, text, reply_to_message_id=None):
        self.messages.append((chat_id, text, reply_to_message_id))
        return {"message_id": len(self.messages)}

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return {"message_id": message_id}

    async def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    async def send_document(self, chat_id, path, caption="", reply_to_message_id=None):
        self.documents.append((chat_id, Path(path), caption, reply_to_message_id))
        return {"message_id": 99}

    async def download_file(self, file_id, destination, max_bytes):
        self.downloads.append((file_id, Path(destination), max_bytes))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("Telegram attachment", encoding="utf-8")
        return destination


class FakePollingTelegramAPI(FakeTelegramAPI):
    def __init__(self, update):
        super().__init__()
        self.update = update
        self.returned_update = False
        self.hold = asyncio.Event()

    async def delete_webhook(self):
        return True

    async def get_me(self):
        return {"username": "ares_test_bot"}

    async def get_updates(self, *, offset, timeout):
        if not self.returned_update:
            self.returned_update = True
            return [self.update]
        await self.hold.wait()
        return []


class FakeAudioTranscriber:
    def __init__(self, text="What is on my calendar today?"):
        self.text = text
        self.calls = []

    async def transcribe_to_english(self, path, *, duration_seconds=0):
        self.calls.append((Path(path), duration_seconds))
        return EnglishTranscript(text=self.text, backend="whisper")


@pytest.fixture
def telegram_channel(tmp_path):
    config = AppConfig(
        data_dir=str(tmp_path),
        telegram=TelegramConfig(
            enabled=True,
            bot_token="test-token",
            allowed_chat_ids=[123],
            show_tool_progress=True,
        ),
    )
    store = FakeConversationStore()
    api = FakeTelegramAPI()
    state = ChannelStore(tmp_path / "ares.db")
    channel = TelegramChannel(
        config=config,
        agent=FakeAgent(config),
        conversation_store=store,
        api=api,
        state_store=state,
    )
    yield channel, store, api, state
    state.close()


@pytest.mark.asyncio
async def test_authorized_message_uses_persistent_conversation_and_reports_tool_activity(telegram_channel):
    channel, conversations, api, state = telegram_channel

    await channel._handle_update(
        {
            "update_id": 10,
            "message": {"message_id": 5, "chat": {"id": 123, "type": "private"}, "text": "research Ares"},
        }
    )

    assert state.get_conversation_id("telegram", 123) == 1
    assert [message["role"] for message in conversations.messages] == ["user", "assistant"]
    assert conversations.messages[0]["content"] == "research Ares"
    assert conversations.messages[1]["content"] == "Here is the answer."
    assert any("Using tool: web search" in edit[2] for edit in api.edits)
    assert any(message[1] == "Here is the answer." for message in api.messages)


@pytest.mark.asyncio
async def test_unauthorized_start_never_runs_agent(telegram_channel):
    channel, _conversations, api, _state = telegram_channel

    await channel._handle_update(
        {
            "update_id": 11,
            "message": {"message_id": 6, "chat": {"id": 999, "type": "private"}, "text": "/start"},
        }
    )

    assert not channel.agent.prompts
    assert api.messages == [
        (999, "Ares is running, but this chat is not authorized. Chat ID: 999. On the PC, add it to telegram.allowed_chat_ids, then restart Ares.", 6)
    ]


@pytest.mark.asyncio
async def test_help_exposes_marketplace_commands_to_an_authorized_chat(telegram_channel):
    channel, _conversations, api, _state = telegram_channel

    await channel._handle_update(
        {
            "update_id": 111,
            "message": {"message_id": 61, "chat": {"id": 123, "type": "private"}, "text": "/help"},
        }
    )

    response = api.messages[-1][1]
    assert "/skills [list|search|info|install]" in response
    assert "/mcp [list|search|info|add|test|refresh]" in response
    assert "/confirm <code>" in response


@pytest.mark.asyncio
async def test_mcp_status_command_does_not_route_through_the_agent(telegram_channel):
    channel, _conversations, api, _state = telegram_channel

    await channel._handle_update(
        {
            "update_id": 112,
            "message": {"message_id": 62, "chat": {"id": 123, "type": "private"}, "text": "/mcp status"},
        }
    )

    assert "No MCP servers are active" in api.messages[-1][1]
    assert not channel.agent.prompts


@pytest.mark.asyncio
async def test_skills_search_command_uses_registry_and_returns_inspectable_results(telegram_channel, monkeypatch):
    class FakeSkillRegistry:
        last_errors = {}

        def __init__(self, _registries):
            pass

        async def search(self, query):
            assert query == "research briefs"
            return [
                SimpleNamespace(
                    reference="@ares/research-brief",
                    name="Research Brief",
                    slug="research-brief",
                    version="1.2.0",
                    registry="clawhub",
                    description="Creates source-backed research briefs.",
                    score=0.9,
                )
            ]

    monkeypatch.setattr(telegram_module, "SkillRegistryClient", FakeSkillRegistry)
    channel, conversations, api, _state = telegram_channel

    await channel._handle_update(
        {
            "update_id": 113,
            "message": {"message_id": 63, "chat": {"id": 123, "type": "private"}, "text": "/skills search research briefs"},
        }
    )

    response = api.messages[-1][1]
    assert "@ares/research-brief" in response
    assert "/skills info 1" in response
    assert [message["role"] for message in conversations.messages] == ["user", "assistant"]
    assert conversations.messages[-1]["content"] == response


@pytest.mark.asyncio
async def test_marketplace_result_is_available_to_the_next_natural_language_turn(telegram_channel):
    channel, conversations, _api, state = telegram_channel
    channel._active_marketplace_requests[123] = "/mcp info example/weather"
    await channel._send_marketplace_message(
        123,
        "MCP · Weather\n\nWhat it does: Reads forecast data from a weather API.",
        reply_to=64,
    )
    channel._active_marketplace_requests.pop(123, None)

    conversation_id = state.get_conversation_id("telegram", 123)
    assert conversation_id is not None
    history = channel._conversation_history(conversation_id)
    assert history[-1]["content"].startswith("MCP · Weather")

    await channel._handle_update(
        {
            "update_id": 114,
            "message": {"message_id": 65, "chat": {"id": 123, "type": "private"}, "text": "what does it do?"},
        }
    )

    _prompt, agent_history = channel.agent.prompts[-1]
    assert any("Reads forecast data" in item["content"] for item in agent_history)
    assert [message["role"] for message in conversations.messages][-2:] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_file_command_only_sends_existing_regular_file(telegram_channel, tmp_path):
    channel, _conversations, api, _state = telegram_channel
    report = tmp_path / "report.txt"
    report.write_text("ready", encoding="utf-8")

    await channel._handle_update(
        {
            "update_id": 12,
            "message": {
                "message_id": 7,
                "chat": {"id": 123, "type": "private"},
                "text": f"/file {report}",
            },
        }
    )

    assert api.documents == [(123, report.resolve(), "Ares file: report.txt", 7)]
    assert (123, "upload_document") in api.actions


@pytest.mark.asyncio
async def test_document_attachment_is_downloaded_and_added_as_untrusted_context(telegram_channel):
    channel, _conversations, api, _state = telegram_channel

    await channel._handle_update(
        {
            "update_id": 13,
            "message": {
                "message_id": 8,
                "chat": {"id": 123, "type": "private"},
                "caption": "summarize this",
                "document": {
                    "file_id": "file-1",
                    "file_name": "notes.txt",
                    "mime_type": "text/plain",
                    "file_size": 20,
                },
            },
        }
    )

    prompt, _history = channel.agent.prompts[0]
    assert "Telegram attachment" in prompt
    assert "untrusted file contents" in prompt
    assert api.downloads[0][0] == "file-1"


@pytest.mark.asyncio
async def test_voice_note_is_transcribed_to_english_before_running_ares(telegram_channel):
    channel, conversations, api, _state = telegram_channel
    transcriber = FakeAudioTranscriber()
    channel.audio_transcriber = transcriber

    await channel._handle_update(
        {
            "update_id": 15,
            "message": {
                "message_id": 11,
                "chat": {"id": 123, "type": "private"},
                "voice": {"file_id": "voice-1", "file_size": 120, "duration": 8},
            },
        }
    )

    prompt, _history = channel.agent.prompts[0]
    assert "What is on my calendar today?" in prompt
    assert "Reply only in English" in prompt
    assert transcriber.calls[0][1] == 8
    assert conversations.messages[0]["content"].startswith("Voice note (English transcript):")
    assert any(message[1].startswith("Transcript (English):") for message in api.messages)


@pytest.mark.asyncio
async def test_audio_transcriber_auto_falls_back_to_whisper_when_sarvam_fails(monkeypatch):
    from ares.models import TelegramConfig

    config = TelegramConfig(audio_stt_backend="auto")
    transcriber = EnglishAudioTranscriber(lambda: config)
    calls = []

    async def sarvam_failure(_path):
        calls.append("sarvam")
        raise RuntimeError("temporary provider outage")

    async def whisper_success(_path, _model):
        calls.append("whisper")
        return EnglishTranscript(text="English fallback transcript", backend="whisper")

    monkeypatch.setenv("SARVAM_API_KEY", "configured")
    monkeypatch.setattr(transcriber, "_transcribe_with_sarvam", sarvam_failure)
    monkeypatch.setattr(transcriber, "_transcribe_with_whisper", whisper_success)

    result = await transcriber.transcribe_to_english("voice.ogg", duration_seconds=10)

    assert result.text == "English fallback transcript"
    assert calls == ["sarvam", "whisper"]


@pytest.mark.asyncio
async def test_new_command_closes_old_channel_session(telegram_channel):
    channel, conversations, api, state = telegram_channel
    state.set_conversation_id("telegram", 123, 4)

    await channel._handle_update(
        {
            "update_id": 14,
            "message": {"message_id": 9, "chat": {"id": 123, "type": "private"}, "text": "/new"},
        }
    )

    assert conversations.ended == [4]
    assert state.get_conversation_id("telegram", 123) == 1
    assert api.messages[-1][1] == "Started a new Ares session for this chat."


@pytest.mark.asyncio
async def test_polling_persists_next_offset_after_a_processed_update(telegram_channel):
    channel, _conversations, _api, state = telegram_channel
    update = {
        "update_id": 25,
        "message": {"message_id": 10, "chat": {"id": 123, "type": "private"}, "text": "hello"},
    }
    channel.api = FakePollingTelegramAPI(update)

    await channel.start()
    for _ in range(50):
        if channel.agent.prompts:
            break
        await asyncio.sleep(0.01)
    await channel.stop()

    assert channel.agent.prompts
    assert state.get_offset("telegram") == 26


def test_channel_store_never_moves_provider_cursor_backwards(tmp_path):
    state = ChannelStore(tmp_path / "ares.db")
    state.advance_offset("telegram", 100)
    state.advance_offset("telegram", 50)
    assert state.get_offset("telegram") == 100
    state.close()


def test_long_telegram_messages_split_without_losing_text():
    text = "alpha " * 1200
    chunks = _split_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "".join(f"{chunk} " for chunk in chunks).replace("  ", " ").strip() == text.strip()


@pytest.mark.asyncio
async def test_headless_channel_explains_setup_when_disabled(tmp_path, monkeypatch, capsys):
    from ares import config as config_module

    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "config.json")

    await run_telegram_channel()

    assert "python -m ares --telegram-setup" in capsys.readouterr().out
