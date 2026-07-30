"""Tests for multi-bot Telegram channel — parallel turns + tool progress."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ares.channels.telegram_multi import (
    MultiTelegramChannel,
    BotInstance,
    _MultiBotProgress,
    _BotDeliveryAdapter,
)
from ares.models import AppConfig, TelegramMultiConfig, TelegramBotConfig
from ares.context.conversations import ConversationStore


@pytest.fixture
def config():
    """Create a test AppConfig with multi-bot Telegram enabled."""
    cfg = AppConfig()
    cfg.telegram_multi = TelegramMultiConfig(
        enabled=True,
        bots=[
            TelegramBotConfig(
                name="Jarvis",
                bot_token="token1",
                mention="@jarvis_bot",
            ),
            TelegramBotConfig(
                name="Friday",
                bot_token="token2",
                mention="@friday_bot",
            ),
        ],
        allowed_chat_ids=[-1001234567890],
        allow_group_chats=True,
        require_mention=True,
        show_tool_progress=True,
    )
    return cfg


@pytest.fixture
def conversation_store():
    """Create a mock conversation store."""
    store = MagicMock(spec=ConversationStore)
    store.start_conversation.return_value = 1
    store.get_messages_for_model.return_value = []
    store.get_messages.return_value = []
    return store


def _make_instance(
    name: str = "Jarvis",
    mention: str = "@jarvis_bot",
    username: str = "jarvis_bot",
) -> BotInstance:
    api = MagicMock()
    api.send_message = AsyncMock(return_value={"message_id": 42})
    api.edit_message = AsyncMock(return_value={})
    api.send_chat_action = AsyncMock()
    api.send_document = AsyncMock(return_value={"message_id": 99})

    agent = MagicMock()
    agent.config = MagicMock(model="test-model")
    agent.run_stream = MagicMock()
    agent.tool_executor = MagicMock()
    agent.tool_executor.set_telegram_channel = MagicMock()

    return BotInstance(
        config=TelegramBotConfig(name=name, bot_token="t", mention=mention),
        api=api,
        agent=agent,
        skill_manager=MagicMock(),
        conversation_store=MagicMock(),
        bot_user_id=1,
        bot_username=username,
    )


def test_bot_config_creation():
    bot = TelegramBotConfig(
        name="TestBot",
        bot_token="test_token",
        mention="@test_bot",
    )
    assert bot.name == "TestBot"
    assert bot.bot_token == "test_token"
    assert bot.mention == "@test_bot"


def test_multi_config_defaults():
    config = TelegramMultiConfig(
        enabled=True,
        bots=[TelegramBotConfig(name="Bot1", bot_token="t1", mention="@bot1")],
        allowed_chat_ids=[123],
    )
    assert config.enabled is True
    assert len(config.bots) == 1
    assert config.allowed_chat_ids == [123]
    assert config.show_tool_progress is True


def test_multi_channel_initialization(config, conversation_store):
    channel = MultiTelegramChannel(
        config=config,
        conversation_store=conversation_store,
    )
    assert channel.config == config
    assert channel._multi_config == config.telegram_multi
    assert len(channel._bots) == 0


def test_find_mentioned_bot(config, conversation_store):
    channel = MultiTelegramChannel(
        config=config,
        conversation_store=conversation_store,
    )
    jarvis = _make_instance("Jarvis", "@jarvis_bot", "jarvis_bot")
    friday = _make_instance("Friday", "@friday_bot", "friday_bot")
    channel._bots = {
        "@jarvis_bot": jarvis,
        "@friday_bot": friday,
    }

    assert channel._find_mentioned_bot("Hey @jarvis_bot what's up?") is jarvis
    assert channel._find_mentioned_bot("@friday_bot help me") is friday
    assert channel._find_mentioned_bot("Hello everyone!") is None


def test_is_this_bot_mentioned_matches_username_and_config(config, conversation_store):
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    instance = _make_instance("king", "@king_bot", "king_201009_bot")
    assert channel._is_this_bot_mentioned("@king_201009_bot look at this", instance)
    assert channel._is_this_bot_mentioned("hey @king_bot hi", instance)
    assert not channel._is_this_bot_mentioned("@other_bot hi", instance)


def test_strip_mention(config, conversation_store):
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    instance = _make_instance("Jarvis", "@jarvis_bot", "jarvis_bot")

    result = channel._strip_mention("@jarvis_bot what's the weather?", instance)
    assert result == "what's the weather?"

    result = channel._strip_mention("Hey @jarvis_bot, can you help?", instance)
    assert "can you help?" in result
    assert "@jarvis_bot" not in result.lower()


def test_conversation_id_generation(config, conversation_store):
    with patch("ares.channels.telegram_multi.ChannelStore") as MockStore:
        mock_store = MagicMock()
        mock_store.get_conversation_id.return_value = None
        MockStore.return_value = mock_store

        call_count = [0]

        def mock_start():
            call_count[0] += 1
            return call_count[0]

        conversation_store.start_conversation = mock_start

        channel = MultiTelegramChannel(
            config=config,
            conversation_store=conversation_store,
        )
        channel.state_store = mock_store

        id1 = channel._conversation_id(123, "Jarvis")
        id2 = channel._conversation_id(123, "Friday")
        id3 = channel._conversation_id(456, "Jarvis")

        assert id1 != id2
        assert id1 != id3
        assert id2 != id3


def test_turn_locks_are_per_bot_not_per_chat(config, conversation_store):
    """Different bots in the same chat must not share a lock (true parallelism)."""
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    lock_a = channel._turn_lock("Jarvis", -100)
    lock_b = channel._turn_lock("Friday", -100)
    lock_a2 = channel._turn_lock("Jarvis", -100)
    assert lock_a is lock_a2
    assert lock_a is not lock_b


@pytest.mark.asyncio
async def test_bots_process_messages_in_parallel(config, conversation_store):
    """Two bots handling turns at once must overlap in time, not run inline."""
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    channel.state_store = MagicMock()
    channel.state_store.get_conversation_id.side_effect = [10, 20]
    conversation_store.get_messages_for_model.return_value = []
    conversation_store.add_message = MagicMock()

    started: list[str] = []
    order: list[str] = []
    barrier = asyncio.Event()

    async def slow_stream_jarvis(prompt, **kwargs):
        started.append("jarvis")
        order.append("jarvis-start")
        # Wait until Friday has also started — proves we are concurrent
        for _ in range(50):
            if "friday" in started:
                break
            await asyncio.sleep(0.01)
        order.append("jarvis-mid")
        yield "Jarvis answer"
        order.append("jarvis-end")

    async def slow_stream_friday(prompt, **kwargs):
        started.append("friday")
        order.append("friday-start")
        await asyncio.sleep(0.02)
        order.append("friday-mid")
        yield "Friday answer"
        order.append("friday-end")

    jarvis = _make_instance("Jarvis", "@jarvis_bot", "jarvis_bot")
    friday = _make_instance("Friday", "@friday_bot", "friday_bot")
    jarvis.agent.run_stream = slow_stream_jarvis
    friday.agent.run_stream = slow_stream_friday
    # Disable progress messages to keep the test focused on concurrency
    config.telegram_multi.show_tool_progress = False

    channel._bots = {"@jarvis_bot": jarvis, "@friday_bot": friday}

    await asyncio.gather(
        channel._handle_chat_message(jarvis, -100, "@jarvis_bot do work", 1),
        channel._handle_chat_message(friday, -100, "@friday_bot do work", 2),
    )

    assert "jarvis" in started and "friday" in started
    # Friday must have started before Jarvis finished — not strict serial order
    assert order.index("friday-start") < order.index("jarvis-end")
    jarvis.api.send_message.assert_awaited()
    friday.api.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_tool_progress_events_are_surfaced(config, conversation_store):
    """Tool tokens should update progress like the single-bot channel."""
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    channel.state_store = MagicMock()
    channel.state_store.get_conversation_id.return_value = 7
    conversation_store.get_messages_for_model.return_value = []
    conversation_store.add_message = MagicMock()

    async def tool_stream(prompt, **kwargs):
        yield "[tool_start:read_file]"
        yield "[tool_progress:read_file:Reading demo_patients.csv]"
        yield '[tool:read_file:{"ok": true, "rows": 3}]'
        yield "Patient A needs attention."

    jarvis = _make_instance("Jarvis", "@jarvis_bot", "jarvis_bot")
    jarvis.agent.run_stream = tool_stream
    channel._bots = {"@jarvis_bot": jarvis}

    await channel._handle_chat_message(
        jarvis, -100, "@jarvis_bot Look at demo_patients.csv", 5
    )

    # Progress message was posted and edited
    assert jarvis.api.send_message.await_count >= 2  # progress + final answer
    # edit_message called for tool events / finish
    assert jarvis.api.edit_message.await_count >= 1

    # Final answer delivered
    final_calls = [
        c for c in jarvis.api.send_message.await_args_list
        if "Patient A needs attention" in str(c)
    ]
    assert final_calls

    # Done status mentions tools
    done_edits = [
        c.args[2] if len(c.args) >= 3 else c.kwargs.get("text", "")
        for c in jarvis.api.edit_message.await_args_list
    ]
    assert any("done" in str(t).lower() or "tool" in str(t).lower() for t in done_edits)


@pytest.mark.asyncio
async def test_poll_spawns_background_tasks_without_awaiting_turn(
    config, conversation_store
):
    """Poll loop must advance and stay free while a turn is still running."""
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    instance = _make_instance()
    channel._bots = {"@jarvis_bot": instance}

    turn_started = asyncio.Event()
    turn_release = asyncio.Event()

    async def blocking_handle(inst, update):
        turn_started.set()
        await turn_release.wait()

    channel._handle_update = blocking_handle  # type: ignore[method-assign]

    instance.api.get_updates = AsyncMock(
        side_effect=[
            [{"update_id": 1, "message": {"message_id": 1, "chat": {"id": -1, "type": "group"}, "text": "@jarvis_bot hi"}}],
            asyncio.CancelledError(),
        ]
    )

    poll_task = asyncio.create_task(channel._poll_bot(instance))
    await asyncio.wait_for(turn_started.wait(), timeout=2.0)

    # Poll should have spawned work and returned to looping; inflight non-empty
    assert len(instance.inflight) >= 1

    turn_release.set()
    # Let the background turn finish, then cancel poll
    await asyncio.sleep(0.05)
    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task


@pytest.mark.asyncio
async def test_progress_render_includes_bot_name():
    instance = _make_instance("Ironclad", "@Ironclad_Trade_Bot", "Ironclad_Trade_Bot")
    progress = _MultiBotProgress(instance, chat_id=-1, reply_to=1, show_progress=False)
    progress.events = ["Thinking", "Using tool: read file"]
    rendered = progress._render()
    assert "Ironclad" in rendered
    assert "Using tool: read file" in rendered


@pytest.mark.asyncio
async def test_delivery_adapter_sends_via_bot_api(tmp_path, config, conversation_store):
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    instance = _make_instance()
    adapter = _BotDeliveryAdapter(instance, channel)

    file_path = tmp_path / "report.csv"
    file_path.write_text("a,b\n1,2\n", encoding="utf-8")

    result = await adapter.deliver_file(path=file_path, chat_id=-1001234567890)
    assert result["ok"] is True
    assert result["name"] == "report.csv"
    instance.api.send_document.assert_awaited()


def test_tool_label_formats_mcp_and_local(config, conversation_store):
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    assert "Using tool: read file" == channel._tool_label("read_file", "Using")
    assert "Finished MCP:" in channel._tool_label("mcp__github__list_issues", "Finished")


@pytest.mark.asyncio
async def test_unmentioned_bot_ignores_group_message(config, conversation_store):
    channel = MultiTelegramChannel(config=config, conversation_store=conversation_store)
    jarvis = _make_instance("Jarvis", "@jarvis_bot", "jarvis_bot")
    friday = _make_instance("Friday", "@friday_bot", "friday_bot")
    channel._bots = {"@jarvis_bot": jarvis, "@friday_bot": friday}

    update = {
        "update_id": 9,
        "message": {
            "message_id": 3,
            "chat": {"id": -1001234567890, "type": "supergroup"},
            "text": "@friday_bot only you please",
        },
    }
    await channel._handle_update(jarvis, update)
    jarvis.api.send_message.assert_not_awaited()
