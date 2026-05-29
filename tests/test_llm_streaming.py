import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.llm import NIMClient


def chunk(text):
    delta = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def completion(text):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    def __init__(self, stream_chunks=None, completion_text="fallback", stream_delay=0.0):
        self.stream_chunks = stream_chunks or []
        self.completion_text = completion_text
        self.stream_delay = stream_delay
        self.calls = []

    def create(self, model, **kwargs):
        self.calls.append({"model": model, "stream": kwargs.get("stream")})
        if kwargs.get("stream"):
            def iterator():
                if self.stream_delay:
                    time.sleep(self.stream_delay)
                for item in self.stream_chunks:
                    yield item
            return iterator()
        return completion(self.completion_text)


class AdaptiveStreamingTests(unittest.TestCase):
    def test_fast_stream_wins_without_completion_hedge(self):
        client = object.__new__(NIMClient)
        completions = FakeCompletions(stream_chunks=[chunk("hello")])
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        kwargs = {"messages": [], "stream": True, "temperature": 0.3, "max_tokens": 10}
        with patch("agent.llm.settings.llm_streaming_hedge_delay_seconds", 0.2):
            result = list(client._adaptive_stream("model-a", kwargs))

        self.assertEqual(result[0].choices[0].delta.content, "hello")
        self.assertEqual([call["stream"] for call in completions.calls], [True])

    def test_completion_hedge_wins_when_first_stream_token_is_late(self):
        client = object.__new__(NIMClient)
        completions = FakeCompletions(
            stream_chunks=[chunk("late stream")],
            completion_text="fast completion",
            stream_delay=0.05,
        )
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        kwargs = {"messages": [], "stream": True, "temperature": 0.3, "max_tokens": 10}
        with patch("agent.llm.settings.llm_streaming_hedge_delay_seconds", 0.01):
            with patch("agent.llm.settings.llm_streaming_hedge_mode", "completion"):
                result = list(client._adaptive_stream("model-a", kwargs))

        self.assertEqual(result[0].choices[0].delta.content, "fast completion")
        self.assertIn(True, [call["stream"] for call in completions.calls])
        self.assertIn(False, [call["stream"] for call in completions.calls])

    def test_completion_hedge_can_use_configured_model(self):
        client = object.__new__(NIMClient)
        completions = FakeCompletions(
            stream_chunks=[chunk("late stream")],
            completion_text="fast fallback",
            stream_delay=0.05,
        )
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        kwargs = {"messages": [], "stream": True, "temperature": 0.3, "max_tokens": 10}
        with patch("agent.llm.settings.llm_streaming_hedge_delay_seconds", 0.01):
            with patch("agent.llm.settings.llm_streaming_hedge_model", "fallback-model"):
                with patch("agent.llm.settings.llm_streaming_hedge_mode", "completion"):
                    result = list(client._adaptive_stream("primary-model", kwargs))

        self.assertEqual(result[0].choices[0].delta.content, "fast fallback")
        self.assertIn({"model": "primary-model", "stream": True}, completions.calls)
        self.assertIn({"model": "fallback-model", "stream": False}, completions.calls)

    def test_configured_hedge_model_can_stream(self):
        client = object.__new__(NIMClient)
        completions = FakeCompletions(
            stream_chunks=[chunk("fallback stream")],
            stream_delay=0.05,
        )
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        kwargs = {"messages": [], "stream": True, "temperature": 0.3, "max_tokens": 10}
        with patch("agent.llm.settings.llm_streaming_hedge_delay_seconds", 0.0):
            with patch("agent.llm.settings.llm_streaming_hedge_model", "fallback-model"):
                with patch("agent.llm.settings.llm_streaming_hedge_mode", "stream"):
                    result = list(client._adaptive_stream("primary-model", kwargs))

        self.assertEqual(result[0].choices[0].delta.content, "fallback stream")
        self.assertIn({"model": "primary-model", "stream": True}, completions.calls)
        self.assertIn({"model": "fallback-model", "stream": True}, completions.calls)


if __name__ == "__main__":
    unittest.main()
