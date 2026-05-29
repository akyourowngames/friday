import json
import queue
import threading
from functools import lru_cache
from types import SimpleNamespace

from openai import OpenAI, APIError, APITimeoutError, RateLimitError

from config import settings

try:
    from httpx import TimeoutException as HTTPXTimeoutException
except Exception:
    HTTPXTimeoutException = TimeoutError

_RETRYABLE_NIM_ERRORS = (
    RateLimitError,
    APITimeoutError,
    APIError,
    HTTPXTimeoutException,
    TimeoutError,
    ConnectionError,
)


@lru_cache(maxsize=1)
def _check_api_key_cached(api_key: str) -> bool:
    """Cached API key validation."""
    return bool(api_key.strip())


class NIMClient:
    def __init__(self):
        self.client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=settings.nim_timeout_seconds,
            max_retries=settings.nim_max_retries,
        )

    def check_api_key(self):
        return _check_api_key_cached(settings.nim_api_key)

    def _models(self):
        models = [settings.model_name]
        models.extend(
            model.strip()
            for model in settings.model_fallbacks.split(",")
            if model.strip()
        )
        deduped = []
        seen = set()
        for model in models:
            if model in seen:
                continue
            seen.add(model)
            deduped.append(model)
        return deduped

    def _runtime_error(self, last_error):
        if isinstance(last_error, RateLimitError):
            return RuntimeError("Rate limited by NVIDIA NIM for all configured chat models.")
        if isinstance(last_error, (APITimeoutError, HTTPXTimeoutException, TimeoutError)):
            return RuntimeError("Request timed out for all configured NVIDIA NIM chat models.")
        if isinstance(last_error, APIError):
            return RuntimeError(f"NVIDIA NIM API error for all configured chat models: {last_error}")
        return RuntimeError("NVIDIA NIM chat request failed before a model returned.")

    def _completion_to_stream(self, response):
        message = response.choices[0].message
        tool_calls = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            tool_calls.append(SimpleNamespace(
                index=index,
                id=getattr(call, "id", ""),
                function=SimpleNamespace(
                    name=getattr(function, "name", "") if function else "",
                    arguments=getattr(function, "arguments", "") if function else "",
                ),
            ))
        content = getattr(message, "content", "") or ""
        delta = SimpleNamespace(content=content, tool_calls=tool_calls or None)
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])])

    def _chunk_has_signal(self, chunk):
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return False
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return False
        return bool(getattr(delta, "content", None) or getattr(delta, "tool_calls", None))

    def _adaptive_stream(self, model, kwargs):
        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        hedge_kwargs = dict(kwargs)
        hedge_stream = settings.llm_streaming_hedge_mode.strip().lower() != "completion"
        hedge_kwargs["stream"] = hedge_stream
        hedge_model = settings.llm_streaming_hedge_model.strip() or model
        events = queue.Queue()
        cancel = threading.Event()

        def put(source, kind, payload=None):
            if not cancel.is_set():
                events.put((source, kind, payload))

        def run_stream():
            try:
                response = self.client.chat.completions.create(model=model, **stream_kwargs)
                for chunk in response:
                    if cancel.is_set():
                        break
                    put("stream", "chunk", chunk)
                put("stream", "done")
            except Exception as exc:
                put("stream", "error", exc)

        def run_completion():
            try:
                response = self.client.chat.completions.create(model=hedge_model, **hedge_kwargs)
                chunks = response if hedge_stream else self._completion_to_stream(response)
                for chunk in chunks:
                    if cancel.is_set():
                        break
                    put("completion", "chunk", chunk)
                put("completion", "done")
            except Exception as exc:
                put("completion", "error", exc)

        threading.Thread(target=run_stream, daemon=True).start()
        completion_started = False

        def start_completion():
            nonlocal completion_started
            if completion_started:
                return
            completion_started = True
            threading.Thread(target=run_completion, daemon=True).start()

        def raise_best(errors):
            if "completion" in errors:
                raise errors["completion"]
            if "stream" in errors:
                raise errors["stream"]

        def iterator():
            winner = None
            stream_buffer = []
            done = set()
            errors = {}
            hedge_delay = max(0.0, settings.llm_streaming_hedge_delay_seconds)
            try:
                while True:
                    timeout = hedge_delay if winner is None and not completion_started else None
                    try:
                        source, kind, payload = events.get(timeout=timeout)
                    except queue.Empty:
                        start_completion()
                        continue

                    if kind == "chunk":
                        if winner is None:
                            if source == "stream":
                                if self._chunk_has_signal(payload):
                                    winner = "stream"
                                    for buffered in stream_buffer:
                                        yield buffered
                                    stream_buffer = []
                                    yield payload
                                else:
                                    stream_buffer.append(payload)
                                continue
                            winner = "completion"
                            cancel.set()
                            yield payload
                            continue

                        if source == winner:
                            yield payload
                        continue

                    if kind == "done":
                        done.add(source)
                        if winner == source:
                            return
                        if winner is None:
                            if source == "stream":
                                start_completion()
                            elif "stream" in done:
                                for buffered in stream_buffer:
                                    yield buffered
                                return
                        continue

                    if kind == "error":
                        errors[source] = payload
                        if winner == source:
                            raise payload
                        if winner is None:
                            if source == "stream":
                                start_completion()
                                if "completion" in errors:
                                    raise_best(errors)
                            elif "stream" in errors:
                                raise_best(errors)
            finally:
                cancel.set()

        return iterator()

    def stream(self, messages, tools=None, tool_choice=None):
        kwargs = {
            "messages": messages,
            "stream": settings.llm_streaming_enabled,
            "temperature": 0.3,
            "max_tokens": settings.llm_max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        models = self._models()
        last_error = None

        def create_from(start_index: int):
            nonlocal last_error
            for index in range(start_index, len(models)):
                model = models[index]
                try:
                    if settings.llm_streaming_enabled and settings.llm_streaming_hedge_enabled:
                        return index, model, self._adaptive_stream(model, kwargs)
                    response = self.client.chat.completions.create(model=model, **kwargs)
                    stream = response if settings.llm_streaming_enabled else self._completion_to_stream(response)
                    return index, model, stream
                except _RETRYABLE_NIM_ERRORS as exc:
                    last_error = exc
            return None, "", None

        model_index, model_name, stream = create_from(0)
        if stream is None:
            raise self._runtime_error(last_error)

        def iterator():
            nonlocal model_index, model_name, stream, last_error
            try:
                emitted = False
                for chunk in stream:
                    emitted = True
                    yield chunk
                return
            except _RETRYABLE_NIM_ERRORS as exc:
                last_error = exc
                if emitted:
                    raise RuntimeError(
                        f"NVIDIA NIM stream interrupted for {model_name}: {exc.__class__.__name__}"
                    )
                next_index = model_index + 1
                model_index, model_name, stream = create_from(next_index)
                if stream is None:
                    raise self._runtime_error(last_error)
                yield from iterator()

        return iterator()

    def extract_summary(self, messages: list, instruction: str = "") -> str:
        prompt = instruction.strip() or (
            "Summarize the key information from this conversation concisely "
            "(2-3 sentences). Focus on the user: their identity, what they asked for, "
            "what they were working on, and any decisions or unresolved threads. "
            "Record concrete outcomes when actions actually happened. Do not record the "
            "assistant's refusals or capability disclaimers. Omit greetings and pleasantries."
        )
        try:
            resp = self.client.chat.completions.create(
                model=settings.model_name,
                messages=[{"role": "system", "content": prompt}] + messages[-6:],
                temperature=0,
                max_tokens=200,
            )
            return resp.choices[0].message.content.strip()
        except (APIError, RateLimitError, APITimeoutError):
            return ""

    def extract_facts(self, user_input: str, assistant_response: str, recent_user_context: str = ""):
        body = (
            "Extract personal facts about the user worth remembering. "
            "Return ONLY a JSON array of strings. "
            "ONLY extract if the fact is specific and personal to this user. "
            "Use the user's own messages as the source of truth. "
            "Recent conversation context may resolve pronouns, corrections, and references, "
            "but assistant text is supporting context, not a source for new facts by itself. "
            "If the current user message asks to store the previous point, extract the latest "
            "specific user-provided fact from the recent context. "
            "Do NOT extract: descriptions of the assistant's behavior, general advice, "
            "common knowledge, pleasantries, or vague statements. "
            "Do NOT extract confidence levels, scores, rankings, retrieval metadata, "
            "or statements about whether the assistant has information. "
            "Do NOT extract temporary greeting corrections, the current time of day, "
            "or the user's reaction to a greeting. "
            "Focus on: names, locations, academic status, health issues, preferences, relationships, work. "
            "IMPORTANT: When user corrects or updates a fact (e.g. new location, recovered health), "
            "extract the NEW fact, not the old one. "
            'Examples of GOOD: ["User name is Krish", "User lives in Bangalore", '
            '"User has heat stroke", "User has recovered from illness", '
            '"User now lives in Delhi", "User is feeling better", "Ankita is in class 11th"] '
            'Examples of BAD: ["Assistant offered support", "Stay calm", '
            '"User is feeling uncertain", "User has medical records", '
            '"User confidence level is 0.32"] '
            'Also BAD: vague health statements like "medical records", '
            '"doctor\'s offices", "urgent care", "area with available" '
            "Return [] if nothing worth remembering."
        )
        messages = [
            {"role": "system", "content": body},
            {
                "role": "user",
                "content": (
                    f"Recent context:\n{recent_user_context or '(none)'}\n"
                    f"Current user: {user_input}\n"
                    f"Current assistant: {assistant_response}"
                ),
            },
        ]
        try:
            resp = self.client.chat.completions.create(
                model=settings.model_name,
                messages=messages,
                temperature=0,
                max_tokens=200,
            )
            text = resp.choices[0].message.content.strip()
            return json.loads(text)
        except (json.JSONDecodeError, APIError, RateLimitError, APITimeoutError):
            return []
