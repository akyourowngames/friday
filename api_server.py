import asyncio
import contextlib
import io
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from agent.core import Agent


APP_VERSION = "0.1.0"
FRONTEND_AUDIO_DIR = Path(__file__).resolve().parent / "public" / "frontend" / "audio"

app = FastAPI(title="KING Assistant API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_sessions: dict[str, Agent] = {}
_session_locks: dict[str, threading.Lock] = {}
_sessions_guard = threading.Lock()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    tts: bool = False
    imgbase64: str | None = None


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _get_session(session_id: str | None) -> tuple[str, Agent, threading.Lock]:
    sid = session_id or uuid.uuid4().hex
    with _sessions_guard:
        agent = _sessions.get(sid)
        if agent is None:
            agent = Agent()
            _sessions[sid] = agent
        lock = _session_locks.setdefault(sid, threading.Lock())
    return sid, agent, lock


def _run_agent(agent: Agent, lock: threading.Lock, message: str) -> dict[str, Any]:
    with lock:
        before = len(agent.messages)
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            response = agent.process(message)
        new_messages = agent.messages[before:]
    return {
        "response": str(response or "").strip(),
        "messages": new_messages,
    }


def _chunk_text(text: str, target_size: int = 34) -> list[str]:
    chunks = []
    current = ""
    for word in str(text or "").split(" "):
        next_part = word if not current else f" {word}"
        if current and len(current) + len(next_part) > target_size:
            chunks.append(current)
            current = word
        else:
            current += next_part
    if current:
        chunks.append(current)
    return chunks or ["(No response)"]


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name") or "tool"
            events.append(
                {
                    "event": "tool_chain_planned",
                    "message": f"Using {name}.",
                    "route": _tool_route(name),
                    "tool_name": name,
                }
            )
            if name in {"web_search", "web_fetch", "reddit", "hackernews"}:
                events.append(
                    {
                        "event": "searching_web",
                        "message": f"Gathering evidence with {name}.",
                        "route": "realtime",
                        "tool_name": name,
                    }
                )
    return events


def _tool_route(tool_name: str) -> str:
    if tool_name in {"web_search", "web_fetch", "reddit", "hackernews"}:
        return "realtime"
    if tool_name in {"file_read", "file_list", "terminal"}:
        return "task"
    return "assistant"


def _latest_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    id_to_name = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                function = call.get("function") or {}
                if call.get("id"):
                    id_to_name[call["id"]] = function.get("name", "")
        if msg.get("role") != "tool":
            continue
        parsed = _parse_json_object(msg.get("content") or "")
        meta = parsed.get("meta") if isinstance(parsed.get("meta"), dict) else {}
        tool_name = meta.get("tool") or id_to_name.get(msg.get("tool_call_id"), "")
        if tool_name:
            results.append({"tool": tool_name, "payload": parsed})
    return results


def _panel_payload(tool_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    if tool_name == "web_search":
        source_items = result.get("results") if isinstance(result.get("results"), list) else []
        items = [
            {
                "title": str(item.get("title", "")),
                "content": str(item.get("body", "")),
                "url": str(item.get("url", "")),
            }
            for item in source_items
            if isinstance(item, dict)
        ]
        return {
            "source": "web_search",
            "query": str(result.get("query", "Web search")),
            "answer": f"Web evidence panel opened with {len(items)} source result(s).",
            "results": items,
        }

    if tool_name == "web_fetch":
        title = str(result.get("title") or result.get("final_url") or "Fetched page")
        text = str(result.get("text") or "")
        return {
            "source": "web_fetch",
            "query": str(result.get("requested_url") or result.get("final_url") or "Fetched page"),
            "answer": "Fetched page content is available as grounded evidence.",
            "results": [
                {
                    "title": title,
                    "content": text[:700],
                    "url": str(result.get("final_url") or result.get("requested_url") or ""),
                }
            ],
        }

    if tool_name in {"reddit", "hackernews"}:
        source_items = result.get("items") if isinstance(result.get("items"), list) else []
        items = []
        for item in source_items:
            if not isinstance(item, dict):
                continue
            detail_parts = []
            for key in ("subreddit", "author", "comments", "score", "domain"):
                value = item.get(key)
                if value not in (None, ""):
                    detail_parts.append(f"{key}: {value}")
            items.append(
                {
                    "title": str(item.get("title") or item.get("url") or "Source"),
                    "content": " | ".join(detail_parts),
                    "url": str(item.get("url") or item.get("hn_url") or ""),
                }
            )
        query = result.get("query") or result.get("action") or tool_name
        label = "Reddit" if tool_name == "reddit" else "Hacker News"
        return {
            "source": tool_name,
            "query": str(query),
            "answer": f"{label} evidence panel opened with {len(items)} item(s).",
            "results": items,
        }

    return None


def _panel_payloads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    panels = []
    for item in _latest_tool_results(messages):
        payload = _panel_payload(item["tool"], item["payload"])
        if payload and payload.get("results"):
            panels.append(payload)
    return panels


async def _stream_chat(payload: ChatRequest):
    message = payload.message.strip()
    if not message:
        yield _sse({"error": "Message is empty", "done": True})
        return

    session_id, agent, lock = _get_session(payload.session_id)
    started = time.perf_counter()
    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "query_detected",
                "message": "KING received your command.",
                "route": "assistant",
            },
        }
    )
    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "streaming_started",
                "message": "Running assistant core with grounded tools.",
                "route": "assistant",
            },
        }
    )

    work = asyncio.create_task(asyncio.to_thread(_run_agent, agent, lock, message))
    pulse = 0
    while not work.done():
        await asyncio.sleep(0.9)
        pulse += 1
        yield _sse(
            {
                "session_id": session_id,
                "activity": {
                    "event": "waiting_for_model",
                    "message": "KING core is working...",
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "route": "assistant",
                    "pulse": pulse,
                },
            }
        )

    try:
        result = await work
    except Exception as exc:
        yield _sse({"session_id": session_id, "error": f"KING API error: {exc}", "done": True})
        return

    response = result["response"]
    messages = result["messages"]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    for event in _tool_events(messages):
        yield _sse({"session_id": session_id, "activity": event})
    for panel in _panel_payloads(messages):
        yield _sse(
            {
                "session_id": session_id,
                "activity": {
                    "event": "search_completed",
                    "message": panel.get("answer", "Evidence panel ready."),
                    "route": "realtime",
                    "tool_name": panel.get("source", "evidence"),
                },
            }
        )
        yield _sse({"session_id": session_id, "search_results": panel})

    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "first_chunk",
                "message": "Assistant response ready.",
                "elapsed_ms": elapsed_ms,
                "route": "assistant",
            },
        }
    )
    for chunk in _chunk_text(response or "(No response)"):
        yield _sse({"session_id": session_id, "chunk": chunk})
        await asyncio.sleep(0.025)
    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "stream_complete",
                "message": "Response completed.",
                "elapsed_ms": elapsed_ms,
                "route": "assistant",
            },
        }
    )
    yield _sse({"session_id": session_id, "done": True})


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "king-api",
        "version": APP_VERSION,
        "sessions": len(_sessions),
    }


@app.post("/chat/jarvis/stream")
async def chat_jarvis_stream(payload: ChatRequest):
    return StreamingResponse(_stream_chat(payload), media_type="text/event-stream")


@app.get("/app/audio/{filename}")
def app_audio(filename: str):
    audio_path = (FRONTEND_AUDIO_DIR / filename).resolve()
    if not str(audio_path).startswith(str(FRONTEND_AUDIO_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid audio path")
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(audio_path, media_type="audio/mpeg")


@app.get("/tasks/{task_id}")
def task_status(task_id: str):
    return {
        "id": task_id,
        "status": "failed",
        "error": "Task viewer is not wired yet for KING.",
    }
