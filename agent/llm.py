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


def _find_balanced_object(text: str, from_end: bool) -> str | None:
    """Return a balanced top-level {...} object substring (last if from_end)."""
    if from_end:
        end = text.rfind("}")
        while end != -1:
            depth = 0
            for i in range(end, -1, -1):
                ch = text[i]
                if ch == "}":
                    depth += 1
                elif ch == "{":
                    depth -= 1
                    if depth == 0:
                        return text[i:end + 1]
            end = text.rfind("}", 0, end)
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _sanitize_arguments_string(raw: str) -> str:
    """Repair a possibly-corrupted tool-call arguments string to valid JSON.

    Streamed argument deltas can concatenate into invalid JSON (duplicated or
    overlapping fragments). A malformed arguments string must never be sent to the
    chat API: it 400s the entire request on every subsequent turn, permanently
    breaking the session. Repairs the common cases, else falls back to "{}".
    """
    text = str(raw or "").strip()
    if not text:
        return "{}"
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    for candidate in (_find_balanced_object(text, True), _find_balanced_object(text, False)):
        if candidate:
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return "{}"


def _sanitize_message_tool_calls(messages):
    """Return messages with every tool_call's arguments string made JSON-valid.

    Boundary guard: protects against both freshly-corrupted streams and any
    already-poisoned conversation history carried into this request. Only rebuilds
    messages that actually need fixing, leaving valid ones untouched.
    """
    if not messages:
        return messages
    sanitized = None
    for m_idx, message in enumerate(messages):
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not tool_calls:
            continue
        fixed_calls = None
        for c_idx, call in enumerate(tool_calls):
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            args = function.get("arguments")
            if not isinstance(args, str):
                continue
            repaired = _sanitize_arguments_string(args)
            if repaired == args:
                continue
            if fixed_calls is None:
                fixed_calls = [dict(c, function=dict(c.get("function", {}))) if isinstance(c, dict) else c for c in tool_calls]
            fixed_calls[c_idx]["function"]["arguments"] = repaired
        if fixed_calls is not None:
            if sanitized is None:
                sanitized = list(messages)
            sanitized[m_idx] = dict(message, tool_calls=fixed_calls)
    return sanitized if sanitized is not None else messages


class NIMClient:
    def __init__(self):
        self.client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=settings.nim_timeout_seconds,
            max_retries=settings.nim_max_retries,
        )

    def _resolve_model(self, model_override: str = "") -> str:
        """Return the model to use: override > extraction_model > model_name."""
        if model_override:
            return model_override
        return settings.model_name

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
        messages = _sanitize_message_tool_calls(messages)
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
            "Extract every personal fact the user states about themselves or people/things in their life. "
            "Return a JSON array of strings, each one fact. "
            "Include: names, relationships, locations, preferences, work, health, projects, goals. "
            "Use the user's exact words. When they correct a fact, extract the new version."
        )
        messages = [
            {"role": "system", "content": body},
            {
                "role": "user",
                "content": user_input,
            },
        ]
        model = settings.extraction_model or settings.model_name
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=200,
            )
            text = resp.choices[0].message.content.strip()
            # Strip markdown fences if present (```json ... ```)
            lines = text.splitlines()
            if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
                text = "\n".join(lines[1:-1]).strip()
            return json.loads(text)
        except (json.JSONDecodeError, APIError, RateLimitError, APITimeoutError):
            return []
