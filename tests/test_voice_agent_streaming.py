"""Tests for continuous voice real-time streaming helpers."""

import asyncio
from unittest.mock import MagicMock

import numpy as np

from ares.voice.agent import ContinuousVoiceAgent, _MAX_SENTENCE_CHARS


def test_stream_to_sentences_skips_tool_tokens_and_flushes():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.conversation_history = []

        async def run_stream(text, history):
            yield "[tool:shell:echo hi]"
            yield "Hello "
            yield "world."

        agent.agent = MagicMock()
        agent.agent.run_stream = run_stream
        agent._sentence_ready = lambda text: ContinuousVoiceAgent._sentence_ready(agent, text)

        q = asyncio.Queue()
        result = await ContinuousVoiceAgent._stream_to_sentences(agent, "test", q)
        queued = []
        while not q.empty():
            queued.append(await q.get())
        return result, queued

    result, queued = asyncio.run(run())
    assert result == "Hello world."
    assert queued[-1] is None
    assert "Hello world." in queued


def test_stream_to_sentences_max_length_flushes():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent.conversation_history = []
        long_text = "x" * (_MAX_SENTENCE_CHARS + 5)

        async def run_stream(text, history):
            yield long_text

        agent.agent = MagicMock()
        agent.agent.run_stream = run_stream
        agent._sentence_ready = lambda text: ContinuousVoiceAgent._sentence_ready(agent, text)
        q = asyncio.Queue()
        await ContinuousVoiceAgent._stream_to_sentences(agent, "test", q)
        return await q.get()

    assert asyncio.run(run()) == "x" * (_MAX_SENTENCE_CHARS + 5)


def test_barge_in_watcher_sets_stop_event():
    async def run():
        agent = MagicMock(spec=ContinuousVoiceAgent)
        agent.console = MagicMock()
        agent._read_frames = MagicMock(return_value=[np.ones(480, dtype=np.float32)])
        agent._is_speech = MagicMock(return_value=True)
        agent._drain = MagicMock()
        stop = asyncio.Event()
        await ContinuousVoiceAgent._barge_in_watcher(agent, stop, asyncio.get_event_loop().time() - 1.0)
        return stop.is_set(), agent._drain

    is_set, drain = asyncio.run(run())
    assert is_set
    drain.assert_called_once()
