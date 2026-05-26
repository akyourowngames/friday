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
from agent.router import ToolRouter
from config import settings
from memory.brain import Brain
from tools.registry import execute_tool


APP_VERSION = "0.1.0"
CAM_BYPASS_TOKEN = "TTCAMTOKENTT"
FRONTEND_AUDIO_DIR = Path(__file__).resolve().parent / "public" / "frontend" / "audio"

app = FastAPI(title="KING Assistant API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3002",
        "http://127.0.0.1:3002",
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


class MemoryWriteRequest(BaseModel):
    text: str
    importance: float = 0.8


class MemoryForgetRequest(BaseModel):
    query: str


class MemoryReflectRequest(BaseModel):
    label: str = "frontend"


class NavigatorRouteRequest(BaseModel):
    origin: str
    destination: str
    mode: str = "driving"
    alternatives: bool = False
    timeout_ms: int = 0


class CameraAnalyzeRequest(BaseModel):
    image_base64: str
    prompt: str = "Give a short live caption of the current camera frame."
    mime_type: str = "image/jpeg"
    timeout_ms: int = 0


class CameraIntentRequest(BaseModel):
    message: str


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


def _fresh_brain() -> Brain:
    return Brain()


def _node_name(graph: dict[str, Any], node_id: str) -> str:
    node = graph.get("nodes", {}).get(node_id, {})
    return str(node.get("name") or node_id)


def _memory_graph_payload(brain: Brain) -> dict[str, Any]:
    graph = getattr(brain, "_graph", {})
    nodes = []
    for node in graph.get("nodes", {}).values():
        if not isinstance(node, dict):
            continue
        nodes.append(
            {
                "id": str(node.get("id", "")),
                "name": str(node.get("name", "")),
                "type": str(node.get("type", "concept")),
                "importance": node.get("importance", 0.5),
                "created_at": node.get("created_at"),
                "updated_at": node.get("updated_at"),
                "aliases": node.get("aliases", []),
            }
        )

    edges = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        edges.append(
            {
                "id": str(edge.get("id", "")),
                "source": str(edge.get("source", "")),
                "source_name": _node_name(graph, str(edge.get("source", ""))),
                "target": str(edge.get("target", "")),
                "target_name": _node_name(graph, str(edge.get("target", ""))),
                "relation": str(edge.get("relation", "")),
                "strength": edge.get("strength", 0.5),
                "confidence": edge.get("confidence", 0.5),
                "memory_id": str(edge.get("memory_id", "")),
                "tier": str(edge.get("tier", "semantic")),
                "mode": str(edge.get("mode", "multi")),
                "active": bool(edge.get("active", True)),
                "evidence": str(edge.get("evidence", "")),
                "created_at": edge.get("created_at"),
                "updated_at": edge.get("updated_at"),
                "valid_from": edge.get("valid_from"),
                "valid_to": edge.get("valid_to"),
                "inactive_reason": edge.get("inactive_reason"),
                "supersedes": edge.get("supersedes", []),
            }
        )

    return {
        "assessment": brain.system_assessment(),
        "summary": brain.graph_summary("", limit=20),
        "nodes": nodes,
        "edges": edges,
        "reflections": graph.get("reflections", []),
        "memories": brain.list_memories(200),
    }


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
            if name == "navigator":
                events.append(
                    {
                        "event": "navigation_planning",
                        "message": "Resolving route distance with open navigation providers.",
                        "route": "navigation",
                        "tool_name": name,
                    }
                )
            if name == "camera_vision":
                events.append(
                    {
                        "event": "camera_analyzing",
                        "message": "Inspecting the attached camera frame with the vision tool.",
                        "route": "vision",
                        "tool_name": name,
                    }
                )
    return events


def _tool_route(tool_name: str) -> str:
    if tool_name in {"web_search", "web_fetch", "reddit", "hackernews"}:
        return "realtime"
    if tool_name == "navigator":
        return "navigation"
    if tool_name == "camera_vision":
        return "vision"
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

    if tool_name == "navigator":
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        origin = result.get("origin") if isinstance(result.get("origin"), dict) else {}
        destination = result.get("destination") if isinstance(result.get("destination"), dict) else {}
        headline = ""
        narrative = result.get("narrative")
        if isinstance(narrative, dict):
            headline = str(narrative.get("headline") or "")
        query = f"{result.get('origin_query', '')} to {result.get('destination_query', '')}".strip()
        answer = "Navigator panel opened from grounded route data."
        if route.get("fallback_used"):
            answer = "Navigator panel opened with straight-line fallback data."
        precision_note = str(result.get("precision_note") or "")
        if precision_note:
            answer = "Navigator panel opened with representative-point route data."
        return {
            "source": "navigator",
            "query": query or headline or "Navigator route",
            "answer": answer,
            "origin": origin,
            "destination": destination,
            "mode": str(result.get("mode") or ""),
            "provider_sequence": result.get("provider_sequence") if isinstance(result.get("provider_sequence"), list) else [],
            "route": route,
            "route_places": result.get("route_places") if isinstance(result.get("route_places"), list) else [],
            "route_place_status": result.get("route_place_status") if isinstance(result.get("route_place_status"), dict) else {},
            "straight_line": result.get("straight_line") if isinstance(result.get("straight_line"), dict) else {},
            "degraded": bool(result.get("degraded")),
            "degraded_reason": str(result.get("degraded_reason") or ""),
            "precision_note": precision_note,
            "narrative": narrative if isinstance(narrative, dict) else {},
            "results": [
                {
                    "title": headline or "Navigator route",
                    "content": f"{route.get('distance_km', '?')} km, {route.get('duration_text', 'time unavailable')}",
                    "url": "",
                }
            ],
        }

    if tool_name == "camera_vision":
        description = str(result.get("description") or result.get("transcript") or "").strip()
        query = str(result.get("prompt") or "Camera frame").strip()
        return {
            "source": "camera_vision",
            "query": query,
            "answer": "Camera vision returned grounded frame analysis.",
            "description": description,
            "transcript": str(result.get("transcript") or description),
            "provider": str(result.get("provider") or ""),
            "model": str(result.get("model") or ""),
            "mime_type": str(result.get("mime_type") or ""),
            "image_bytes": result.get("image_bytes"),
            "captured_at": result.get("captured_at"),
            "results": [
                {
                    "title": "Camera frame",
                    "content": description or "No visual description was returned.",
                    "url": "",
                }
            ],
        }

    return None


def _panel_payloads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    panels = []
    for item in _latest_tool_results(messages):
        payload = _panel_payload(item["tool"], item["payload"])
        if payload and payload.get("results"):
            panels.append(payload)
    return panels


def _clean_camera_prompt(message: str) -> str:
    prompt = str(message or "").replace(CAM_BYPASS_TOKEN, "").strip()
    if not prompt:
        return "Describe what is visible in this camera frame. If readable text is present, include it."
    return prompt


def _run_camera_tool(prompt: str, image_base64: str, mime_type: str = "image/jpeg", timeout_ms: int = 0) -> dict[str, Any]:
    return execute_tool(
        "camera_vision",
        image_base64=image_base64,
        prompt=prompt,
        mime_type=mime_type,
        timeout_ms=timeout_ms or settings.camera_default_timeout_ms,
        response_format="structured",
    )


def _camera_intent_payload(message: str) -> dict[str, Any]:
    query = _clean_camera_prompt(message)
    if not query:
        return {
            "should_use_camera": False,
            "selected": [],
            "scores": [],
            "reason": "empty_message",
        }
    router = ToolRouter()
    selected = router.select_tools(query)
    decision = router.last_decision()
    names = [str(tool.get("name") or "") for tool in selected if isinstance(tool, dict)]
    camera_score = 0.0
    for item in decision.get("scores", []):
        if isinstance(item, dict) and item.get("tool") == "camera_vision":
            camera_score = float(item.get("score") or 0.0)
            break
    return {
        "should_use_camera": "camera_vision" in names,
        "selected": names,
        "scores": decision.get("scores", []),
        "camera_score": camera_score,
        "reason": str(decision.get("reason") or ""),
    }


def _camera_error_message(result: dict[str, Any]) -> str:
    error = result.get("error") if isinstance(result, dict) else None
    if not isinstance(error, dict):
        return "Camera vision failed without a structured error."
    message = str(error.get("message") or "Camera vision failed.")
    suggestion = str(error.get("suggestion") or "").strip()
    return f"{message} {suggestion}".strip()


async def _stream_camera_chat(payload: ChatRequest, session_id: str):
    prompt = _clean_camera_prompt(payload.message)
    started = time.perf_counter()
    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "camera_analyzing",
                "message": "Inspecting the frame with the camera vision tool.",
                "route": "vision",
                "tool_name": "camera_vision",
            },
        }
    )
    work = asyncio.create_task(asyncio.to_thread(_run_camera_tool, prompt, payload.imgbase64 or "", "image/jpeg", settings.camera_default_timeout_ms))
    pulse = 0
    while not work.done():
        await asyncio.sleep(1.3)
        pulse += 1
        yield _sse(
            {
                "session_id": session_id,
                "activity": {
                    "event": "camera_waiting",
                    "message": "Vision model is reading the current frame...",
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "route": "vision",
                    "tool_name": "camera_vision",
                    "pulse": pulse,
                },
            }
        )
    result = await work
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if isinstance(result, dict) and "error" in result:
        yield _sse(
            {
                "session_id": session_id,
                "activity": {
                    "event": "camera_failed",
                    "message": _camera_error_message(result),
                    "elapsed_ms": elapsed_ms,
                    "route": "vision",
                    "tool_name": "camera_vision",
                },
            }
        )
        yield _sse({"session_id": session_id, "error": _camera_error_message(result), "done": True})
        return
    panel = _panel_payload("camera_vision", result) or {}
    response = str(panel.get("description") or "I could not get a visual description from that frame.").strip()
    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "camera_completed",
                "message": "Camera vision result is ready.",
                "elapsed_ms": elapsed_ms,
                "route": "vision",
                "tool_name": "camera_vision",
            },
        }
    )
    if panel:
        yield _sse({"session_id": session_id, "vision_result": panel})
    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "first_chunk",
                "message": "Assistant response ready.",
                "elapsed_ms": elapsed_ms,
                "route": "vision",
            },
        }
    )
    for chunk in _chunk_text(response or "(No response)", target_size=72):
        yield _sse({"session_id": session_id, "chunk": chunk})
        await asyncio.sleep(0.015)
    yield _sse(
        {
            "session_id": session_id,
            "activity": {
                "event": "stream_complete",
                "message": "Response completed.",
                "elapsed_ms": elapsed_ms,
                "route": "vision",
            },
        }
    )
    yield _sse({"session_id": session_id, "done": True})


async def _stream_chat(payload: ChatRequest):
    message = payload.message.strip()
    if not message:
        yield _sse({"error": "Message is empty", "done": True})
        return

    session_id, agent, lock = _get_session(payload.session_id)
    if payload.imgbase64 and payload.imgbase64.strip():
        async for item in _stream_camera_chat(payload, session_id):
            yield item
        return

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
        if panel.get("source") == "navigator":
            yield _sse(
                {
                    "session_id": session_id,
                    "activity": {
                        "event": "navigation_completed",
                        "message": panel.get("answer", "Navigator panel ready."),
                        "route": "navigation",
                        "tool_name": "navigator",
                    },
                }
            )
            yield _sse({"session_id": session_id, "navigator_result": panel})
            continue
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


@app.get("/memory/graph")
def memory_graph():
    brain = _fresh_brain()
    return _memory_graph_payload(brain)


@app.post("/memory/remember")
def memory_remember(payload: MemoryWriteRequest):
    brain = _fresh_brain()
    result = brain.remember(payload.text, importance=payload.importance)
    return {"result": result, "memory": _memory_graph_payload(brain)}


@app.post("/memory/forget")
def memory_forget(payload: MemoryForgetRequest):
    brain = _fresh_brain()
    result = brain.forget(payload.query)
    return {"result": result, "memory": _memory_graph_payload(brain)}


@app.post("/memory/reflect")
def memory_reflect(payload: MemoryReflectRequest):
    brain = _fresh_brain()
    reflection = brain.reflect(payload.label)
    return {"result": reflection, "memory": _memory_graph_payload(brain)}


@app.post("/navigator/route")
def navigator_route(payload: NavigatorRouteRequest):
    result = execute_tool(
        "navigator",
        origin=payload.origin,
        destination=payload.destination,
        mode=payload.mode,
        alternatives=payload.alternatives,
        timeout_ms=payload.timeout_ms,
        response_format="structured",
    )
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return _panel_payload("navigator", result) or result


@app.post("/camera/analyze")
def camera_analyze(payload: CameraAnalyzeRequest):
    result = _run_camera_tool(
        payload.prompt,
        payload.image_base64,
        payload.mime_type,
        payload.timeout_ms or settings.camera_default_timeout_ms,
    )
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return _panel_payload("camera_vision", result) or result


@app.post("/camera/intent")
def camera_intent(payload: CameraIntentRequest):
    return _camera_intent_payload(payload.message)


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
