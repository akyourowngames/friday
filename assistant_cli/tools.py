from __future__ import annotations

import ast
import base64
import hashlib
import json
import math
import operator
import random
import secrets
import string
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import Settings


JsonObject = dict[str, Any]
ToolHandler = Callable[["ToolContext", JsonObject], "ToolResult"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonObject
    examples: tuple[str, ...] = ()

    def openai_schema(self) -> JsonObject:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    text: str
    data: JsonObject
    latency_ms: int = 0

    def as_dict(self) -> JsonObject:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "text": self.text,
            "data": self.data,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class ToolContext:
    settings: Settings
    workspace_root: Path
    http: Any


class StatelessHttpClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(url, timeout=self.timeout, follow_redirects=True, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.post(url, timeout=self.timeout, follow_redirects=True, **kwargs)

    def close(self) -> None:
        return None


class ToolRegistry:
    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def openai_schemas(self) -> list[JsonObject]:
        return [spec.openai_schema() for spec in self.specs()]

    def close(self) -> None:
        close = getattr(self.context.http, "close", None)
        if callable(close):
            close()

    def execute(self, name: str, args: JsonObject | None = None) -> ToolResult:
        tool_name = str(name or "").strip()
        if tool_name not in self._handlers:
            available = ", ".join(self.names())
            return ToolResult(
                tool=tool_name or "unknown",
                ok=False,
                text=f"Unknown tool. Available tools: {available}",
                data={"available": self.names()},
            )

        clean_args = args or {}
        if not isinstance(clean_args, dict):
            return ToolResult(
                tool=tool_name,
                ok=False,
                text="Tool arguments must be a JSON object.",
                data={"received_type": type(clean_args).__name__},
            )

        start = time.perf_counter()
        try:
            result = self._handlers[tool_name](self.context, clean_args)
        except Exception as exc:
            result = ToolResult(tool=tool_name, ok=False, text=str(exc), data={"error": type(exc).__name__})
        latency_ms = int((time.perf_counter() - start) * 1000)
        return replace(result, latency_ms=latency_ms)


def build_default_registry(
    settings: Settings,
    workspace_root: Path | None = None,
    http_client: httpx.Client | None = None,
) -> ToolRegistry:
    context = ToolContext(
        settings=settings,
        workspace_root=(workspace_root or Path.cwd()).resolve(),
        http=http_client or StatelessHttpClient(settings.tool_timeout_seconds),
    )
    registry = ToolRegistry(context)
    for spec, handler in _default_tools():
        registry.register(spec, handler)
    return registry


def _default_tools() -> list[tuple[ToolSpec, ToolHandler]]:
    return [
        (
            ToolSpec(
                name="realtime_search",
                description="Search current web results with Tavily. Requires TAVILY_API_KEY.",
                parameters=_schema(
                    {
                        "query": {"type": "string", "description": "Search query."},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                        "include_answer": {"type": "boolean", "default": True},
                    },
                    required=("query",),
                ),
                examples=("realtime_search query=\"latest NVIDIA NIM models\" max_results=5",),
            ),
            _tool_realtime_search,
        ),
        (
            ToolSpec(
                name="weather",
                description="Get current weather by location or latitude/longitude using Open-Meteo.",
                parameters=_schema(
                    {
                        "location": {"type": "string", "description": "City or place name."},
                        "latitude": {"type": "number"},
                        "longitude": {"type": "number"},
                    }
                ),
                examples=("weather location=Delhi",),
            ),
            _tool_weather,
        ),
        (
            ToolSpec(
                name="geocode",
                description="Convert a place name into latitude/longitude coordinates.",
                parameters=_schema(
                    {
                        "location": {"type": "string", "description": "City, address, or place name."},
                        "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    required=("location",),
                ),
                examples=("geocode location=\"New Delhi\"",),
            ),
            _tool_geocode,
        ),
        (
            ToolSpec(
                name="reverse_geocode",
                description="Convert latitude/longitude into a human-readable place.",
                parameters=_schema(
                    {
                        "latitude": {"type": "number"},
                        "longitude": {"type": "number"},
                    },
                    required=("latitude", "longitude"),
                ),
            ),
            _tool_reverse_geocode,
        ),
        (
            ToolSpec(
                name="current_time",
                description="Show current date and time for a timezone.",
                parameters=_schema({"timezone": {"type": "string", "default": "local"}}),
                examples=("current_time timezone=Asia/Kolkata",),
            ),
            _tool_current_time,
        ),
        (
            ToolSpec(
                name="calculator",
                description="Safely evaluate arithmetic expressions.",
                parameters=_schema({"expression": {"type": "string"}}, required=("expression",)),
                examples=("calculator expression=\"(22 / 7) * 3\"",),
            ),
            _tool_calculator,
        ),
        (
            ToolSpec(
                name="unit_convert",
                description="Convert temperature, length, mass, or volume units.",
                parameters=_schema(
                    {
                        "value": {"type": "number"},
                        "from_unit": {"type": "string"},
                        "to_unit": {"type": "string"},
                    },
                    required=("value", "from_unit", "to_unit"),
                ),
                examples=("unit_convert value=72 from_unit=fahrenheit to_unit=celsius",),
            ),
            _tool_unit_convert,
        ),
        (
            ToolSpec(
                name="hash_text",
                description="Hash text with sha256, sha1, or md5.",
                parameters=_schema(
                    {
                        "text": {"type": "string"},
                        "algorithm": {"type": "string", "enum": ["sha256", "sha1", "md5"], "default": "sha256"},
                    },
                    required=("text",),
                ),
            ),
            _tool_hash_text,
        ),
        (
            ToolSpec(
                name="base64_encode",
                description="Encode text as base64.",
                parameters=_schema({"text": {"type": "string"}}, required=("text",)),
            ),
            _tool_base64_encode,
        ),
        (
            ToolSpec(
                name="base64_decode",
                description="Decode base64 into text.",
                parameters=_schema({"text": {"type": "string"}}, required=("text",)),
            ),
            _tool_base64_decode,
        ),
        (
            ToolSpec(
                name="json_format",
                description="Validate and pretty-print JSON.",
                parameters=_schema({"json_text": {"type": "string"}}, required=("json_text",)),
            ),
            _tool_json_format,
        ),
        (
            ToolSpec(
                name="uuid_generate",
                description="Generate UUID values.",
                parameters=_schema({"count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1}}),
            ),
            _tool_uuid_generate,
        ),
        (
            ToolSpec(
                name="random_number",
                description="Generate random integers.",
                parameters=_schema(
                    {
                        "minimum": {"type": "integer", "default": 1},
                        "maximum": {"type": "integer", "default": 100},
                        "count": {"type": "integer", "minimum": 1, "maximum": 50, "default": 1},
                    }
                ),
            ),
            _tool_random_number,
        ),
        (
            ToolSpec(
                name="password_generate",
                description="Generate a local random password.",
                parameters=_schema(
                    {
                        "length": {"type": "integer", "minimum": 8, "maximum": 128, "default": 20},
                        "symbols": {"type": "boolean", "default": True},
                    }
                ),
            ),
            _tool_password_generate,
        ),
        (
            ToolSpec(
                name="url_fetch",
                description="Fetch text from an HTTP or HTTPS URL.",
                parameters=_schema(
                    {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 4000},
                    },
                    required=("url",),
                ),
            ),
            _tool_url_fetch,
        ),
        (
            ToolSpec(
                name="file_list",
                description="List files inside the current workspace.",
                parameters=_schema(
                    {
                        "path": {"type": "string", "default": "."},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                    }
                ),
            ),
            _tool_file_list,
        ),
        (
            ToolSpec(
                name="file_read",
                description="Read a UTF-8 text file inside the current workspace.",
                parameters=_schema(
                    {
                        "path": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 4000},
                    },
                    required=("path",),
                ),
            ),
            _tool_file_read,
        ),
        (
            ToolSpec(
                name="note_add",
                description="Append a local note to storage/tool_notes.jsonl.",
                parameters=_schema(
                    {
                        "text": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    required=("text",),
                ),
            ),
            _tool_note_add,
        ),
        (
            ToolSpec(
                name="note_list",
                description="List recent local notes from storage/tool_notes.jsonl.",
                parameters=_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}),
            ),
            _tool_note_list,
        ),
    ]


def _schema(properties: JsonObject, required: tuple[str, ...] = ()) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _ok(tool: str, text: str, data: JsonObject | None = None) -> ToolResult:
    return ToolResult(tool=tool, ok=True, text=text, data=data or {})


def _fail(tool: str, text: str, data: JsonObject | None = None) -> ToolResult:
    return ToolResult(tool=tool, ok=False, text=text, data=data or {})


def _str_arg(args: JsonObject, key: str, default: str = "") -> str:
    value = args.get(key, default)
    return str(value if value is not None else "").strip()


def _int_arg(args: JsonObject, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _float_arg(args: JsonObject, key: str) -> float:
    try:
        return float(args[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc


def _safe_path(root: Path, raw_path: str) -> Path:
    raw = str(raw_path or ".").strip() or "."
    target = Path(raw)
    if not target.is_absolute():
        target = root / target
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Path must stay inside the current workspace.") from exc
    return resolved_target


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _tool_realtime_search(ctx: ToolContext, args: JsonObject) -> ToolResult:
    query = _str_arg(args, "query")
    if not query:
        return _fail("realtime_search", "query is required")
    if not ctx.settings.tavily_api_key:
        return _fail(
            "realtime_search",
            "TAVILY_API_KEY is missing. Add it to .env to enable realtime web search.",
            {"missing": "TAVILY_API_KEY"},
        )

    max_results = _int_arg(args, "max_results", 5, 1, 10)
    include_answer = bool(args.get("include_answer", True))
    response = ctx.http.post(
        "https://api.tavily.com/search",
        json={
            "api_key": ctx.settings.tavily_api_key,
            "query": query,
            "search_depth": "advanced",
            "include_answer": include_answer,
            "max_results": max_results,
        },
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        rows = []
    lines = [f"Tavily results for: {query}"]
    answer = str(data.get("answer") or "").strip() if isinstance(data, dict) else ""
    if answer:
        lines.append("")
        lines.append(answer)
    for index, item in enumerate(rows[:max_results], 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        score = item.get("score")
        meta = f" score={score}" if score is not None else ""
        lines.append(f"{index}. {title}{meta}")
        if content:
            lines.append(f"   {content[:400]}")
        if url:
            lines.append(f"   {url}")
    return _ok("realtime_search", "\n".join(lines), {"query": query, "results": rows, "answer": answer})


def _geocode(ctx: ToolContext, location: str, count: int = 5) -> list[JsonObject]:
    response = ctx.http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": count, "language": "en", "format": "json"},
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("results") if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _tool_geocode(ctx: ToolContext, args: JsonObject) -> ToolResult:
    location = _str_arg(args, "location")
    if not location:
        return _fail("geocode", "location is required")
    count = _int_arg(args, "count", 5, 1, 10)
    rows = _geocode(ctx, location, count)
    if not rows:
        return _fail("geocode", f"No coordinates found for {location}.", {"location": location})
    lines = [f"Geocode results for: {location}"]
    for index, row in enumerate(rows, 1):
        name = _place_label(row)
        lines.append(f"{index}. {name} | lat={row.get('latitude')} lon={row.get('longitude')}")
    return _ok("geocode", "\n".join(lines), {"location": location, "results": rows})


def _tool_weather(ctx: ToolContext, args: JsonObject) -> ToolResult:
    location = _str_arg(args, "location")
    if "latitude" in args and "longitude" in args:
        latitude = _float_arg(args, "latitude")
        longitude = _float_arg(args, "longitude")
        place = location or f"{latitude},{longitude}"
    else:
        if not location:
            return _fail("weather", "Provide location or latitude and longitude.")
        matches = _geocode(ctx, location, 1)
        if not matches:
            return _fail("weather", f"No coordinates found for {location}.", {"location": location})
        first = matches[0]
        latitude = float(first["latitude"])
        longitude = float(first["longitude"])
        place = _place_label(first)

    response = ctx.http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "timezone": "auto",
        },
    )
    response.raise_for_status()
    data = response.json()
    current = data.get("current") if isinstance(data, dict) else {}
    units = data.get("current_units") if isinstance(data, dict) else {}
    if not isinstance(current, dict):
        current = {}
    if not isinstance(units, dict):
        units = {}

    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    precipitation = current.get("precipitation")
    lines = [
        f"Weather for {place}",
        f"Temperature: {temp} {units.get('temperature_2m', '')}".strip(),
        f"Feels like: {feels} {units.get('apparent_temperature', '')}".strip(),
        f"Humidity: {humidity} {units.get('relative_humidity_2m', '')}".strip(),
        f"Wind: {wind} {units.get('wind_speed_10m', '')}".strip(),
        f"Precipitation: {precipitation} {units.get('precipitation', '')}".strip(),
    ]
    return _ok("weather", "\n".join(lines), {"place": place, "latitude": latitude, "longitude": longitude, "current": current})


def _tool_reverse_geocode(ctx: ToolContext, args: JsonObject) -> ToolResult:
    latitude = _float_arg(args, "latitude")
    longitude = _float_arg(args, "longitude")
    response = ctx.http.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"format": "jsonv2", "lat": latitude, "lon": longitude, "zoom": 10},
        headers={"User-Agent": "FridayCLI/1.0"},
    )
    response.raise_for_status()
    data = response.json()
    label = str(data.get("display_name") or "").strip() if isinstance(data, dict) else ""
    if not label:
        return _fail("reverse_geocode", "No place found for those coordinates.")
    return _ok("reverse_geocode", label, {"latitude": latitude, "longitude": longitude, "place": label})


def _place_label(row: JsonObject) -> str:
    parts = [
        str(row.get("name") or "").strip(),
        str(row.get("admin1") or "").strip(),
        str(row.get("country") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def _tool_current_time(ctx: ToolContext, args: JsonObject) -> ToolResult:
    timezone_name = _str_arg(args, "timezone", "local")
    if timezone_name.lower() in {"", "local"}:
        now = datetime.now().astimezone()
        label = str(now.tzinfo)
    else:
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone_name}") from exc
        now = datetime.now(tz)
        label = timezone_name
    text = f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')} ({label})"
    return _ok("current_time", text, {"timezone": label, "iso": now.isoformat()})


_BIN_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Callable[..., float]] = {
    "abs": abs,
    "ceil": math.ceil,
    "floor": math.floor,
    "round": round,
    "sqrt": math.sqrt,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}


def _tool_calculator(ctx: ToolContext, args: JsonObject) -> ToolResult:
    expression = _str_arg(args, "expression")
    if not expression:
        return _fail("calculator", "expression is required")
    if len(expression) > 240:
        return _fail("calculator", "Expression is too long.")
    tree = ast.parse(expression, mode="eval")
    value = _eval_math(tree.body)
    return _ok("calculator", f"{expression} = {value}", {"expression": expression, "value": value})


def _eval_math(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_math(node.left)
        right = _eval_math(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("Exponent too large.")
        value = _BIN_OPS[type(node.op)](left, right)
        if abs(value) > 1e15:
            raise ValueError("Result too large.")
        return value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_math(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        values = [_eval_math(arg) for arg in node.args]
        if len(values) > 3:
            raise ValueError("Too many function arguments.")
        return _FUNCS[node.func.id](*values)
    raise ValueError("Only arithmetic expressions are supported.")


_UNIT_ALIASES = {
    "c": "celsius",
    "f": "fahrenheit",
    "k": "kelvin",
    "m": "meter",
    "meters": "meter",
    "metre": "meter",
    "metres": "meter",
    "km": "kilometer",
    "kilometers": "kilometer",
    "cm": "centimeter",
    "centimeters": "centimeter",
    "mm": "millimeter",
    "millimeters": "millimeter",
    "mi": "mile",
    "miles": "mile",
    "ft": "foot",
    "feet": "foot",
    "in": "inch",
    "inches": "inch",
    "kg": "kilogram",
    "kilograms": "kilogram",
    "g": "gram",
    "grams": "gram",
    "lb": "pound",
    "lbs": "pound",
    "pounds": "pound",
    "oz": "ounce",
    "ounces": "ounce",
    "l": "liter",
    "liters": "liter",
    "litre": "liter",
    "litres": "liter",
    "ml": "milliliter",
    "milliliters": "milliliter",
}
_LINEAR_UNITS = {
    "meter": ("length", 1.0),
    "kilometer": ("length", 1000.0),
    "centimeter": ("length", 0.01),
    "millimeter": ("length", 0.001),
    "mile": ("length", 1609.344),
    "yard": ("length", 0.9144),
    "foot": ("length", 0.3048),
    "inch": ("length", 0.0254),
    "kilogram": ("mass", 1.0),
    "gram": ("mass", 0.001),
    "pound": ("mass", 0.45359237),
    "ounce": ("mass", 0.028349523125),
    "liter": ("volume", 1.0),
    "milliliter": ("volume", 0.001),
}


def _canonical_unit(unit: str) -> str:
    clean = str(unit or "").strip().lower()
    return _UNIT_ALIASES.get(clean, clean)


def _tool_unit_convert(ctx: ToolContext, args: JsonObject) -> ToolResult:
    value = _float_arg(args, "value")
    from_unit = _canonical_unit(_str_arg(args, "from_unit"))
    to_unit = _canonical_unit(_str_arg(args, "to_unit"))
    if from_unit in {"celsius", "fahrenheit", "kelvin"} or to_unit in {"celsius", "fahrenheit", "kelvin"}:
        result = _convert_temperature(value, from_unit, to_unit)
    else:
        result = _convert_linear(value, from_unit, to_unit)
    text = f"{value:g} {from_unit} = {result:g} {to_unit}"
    return _ok("unit_convert", text, {"value": value, "from_unit": from_unit, "to_unit": to_unit, "result": result})


def _convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
    valid = {"celsius", "fahrenheit", "kelvin"}
    if from_unit not in valid or to_unit not in valid:
        raise ValueError("Temperature conversions require celsius, fahrenheit, or kelvin.")
    if from_unit == "celsius":
        celsius = value
    elif from_unit == "fahrenheit":
        celsius = (value - 32) * 5 / 9
    else:
        celsius = value - 273.15
    if to_unit == "celsius":
        return celsius
    if to_unit == "fahrenheit":
        return celsius * 9 / 5 + 32
    return celsius + 273.15


def _convert_linear(value: float, from_unit: str, to_unit: str) -> float:
    if from_unit not in _LINEAR_UNITS or to_unit not in _LINEAR_UNITS:
        raise ValueError("Unsupported unit.")
    from_kind, from_factor = _LINEAR_UNITS[from_unit]
    to_kind, to_factor = _LINEAR_UNITS[to_unit]
    if from_kind != to_kind:
        raise ValueError(f"Cannot convert {from_kind} to {to_kind}.")
    return value * from_factor / to_factor


def _tool_hash_text(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = _str_arg(args, "text")
    algorithm = _str_arg(args, "algorithm", "sha256").lower()
    if algorithm not in {"sha256", "sha1", "md5"}:
        return _fail("hash_text", "algorithm must be sha256, sha1, or md5")
    digest = hashlib.new(algorithm, text.encode("utf-8")).hexdigest()
    return _ok("hash_text", digest, {"algorithm": algorithm, "digest": digest})


def _tool_base64_encode(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = _str_arg(args, "text")
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return _ok("base64_encode", encoded, {"encoded": encoded})


def _tool_base64_decode(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = _str_arg(args, "text")
    decoded = base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")
    return _ok("base64_decode", decoded, {"decoded": decoded})


def _tool_json_format(ctx: ToolContext, args: JsonObject) -> ToolResult:
    raw = _str_arg(args, "json_text")
    value = json.loads(raw)
    formatted = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return _ok("json_format", formatted, {"value": value})


def _tool_uuid_generate(ctx: ToolContext, args: JsonObject) -> ToolResult:
    count = _int_arg(args, "count", 1, 1, 20)
    values = [str(uuid.uuid4()) for _ in range(count)]
    return _ok("uuid_generate", "\n".join(values), {"values": values})


def _tool_random_number(ctx: ToolContext, args: JsonObject) -> ToolResult:
    minimum = _int_arg(args, "minimum", 1, -1_000_000_000, 1_000_000_000)
    maximum = _int_arg(args, "maximum", 100, -1_000_000_000, 1_000_000_000)
    count = _int_arg(args, "count", 1, 1, 50)
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    values = [random.randint(minimum, maximum) for _ in range(count)]
    return _ok("random_number", ", ".join(str(value) for value in values), {"values": values})


def _tool_password_generate(ctx: ToolContext, args: JsonObject) -> ToolResult:
    length = _int_arg(args, "length", 20, 8, 128)
    include_symbols = bool(args.get("symbols", True))
    alphabet = string.ascii_letters + string.digits
    if include_symbols:
        alphabet += "!@#$%^&*_-+=?"
    password = "".join(secrets.choice(alphabet) for _ in range(length))
    return _ok("password_generate", password, {"length": length, "symbols": include_symbols})


def _tool_url_fetch(ctx: ToolContext, args: JsonObject) -> ToolResult:
    url = _str_arg(args, "url")
    max_chars = _int_arg(args, "max_chars", 4000, 200, 20000)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _fail("url_fetch", "Only http and https URLs are allowed.")
    response = ctx.http.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    text = response.text[:max_chars]
    truncated = len(response.text) > max_chars
    if truncated:
        text += "\n... [truncated]"
    return _ok(
        "url_fetch",
        text,
        {"url": url, "status_code": response.status_code, "content_type": content_type, "truncated": truncated},
    )


def _tool_file_list(ctx: ToolContext, args: JsonObject) -> ToolResult:
    target = _safe_path(ctx.workspace_root, _str_arg(args, "path", "."))
    max_entries = _int_arg(args, "max_entries", 100, 1, 500)
    if not target.exists():
        return _fail("file_list", f"Path does not exist: {_rel(ctx.workspace_root, target)}")
    if not target.is_dir():
        return _fail("file_list", f"Path is not a directory: {_rel(ctx.workspace_root, target)}")
    entries = sorted(target.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))[:max_entries]
    rows = [
        {"path": _rel(ctx.workspace_root, path), "type": "dir" if path.is_dir() else "file", "size": path.stat().st_size}
        for path in entries
    ]
    lines = [f"{len(rows)} entries under {_rel(ctx.workspace_root, target) or '.'}"]
    for row in rows:
        marker = "DIR " if row["type"] == "dir" else "FILE"
        lines.append(f"{marker} {row['path']}")
    return _ok("file_list", "\n".join(lines), {"entries": rows, "truncated": len(rows) == max_entries})


def _tool_file_read(ctx: ToolContext, args: JsonObject) -> ToolResult:
    target = _safe_path(ctx.workspace_root, _str_arg(args, "path"))
    max_chars = _int_arg(args, "max_chars", 4000, 200, 20000)
    if not target.exists():
        return _fail("file_read", f"File does not exist: {_rel(ctx.workspace_root, target)}")
    if not target.is_file():
        return _fail("file_read", f"Path is not a file: {_rel(ctx.workspace_root, target)}")
    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > max_chars
    shown = text[:max_chars] + ("\n... [truncated]" if truncated else "")
    return _ok("file_read", shown, {"path": _rel(ctx.workspace_root, target), "truncated": truncated})


def _notes_path(ctx: ToolContext) -> Path:
    return ctx.workspace_root / "storage" / "tool_notes.jsonl"


def _tool_note_add(ctx: ToolContext, args: JsonObject) -> ToolResult:
    text = _str_arg(args, "text")
    if not text:
        return _fail("note_add", "text is required")
    tags_value = args.get("tags", [])
    tags = [str(tag).strip() for tag in tags_value if str(tag).strip()] if isinstance(tags_value, list) else []
    path = _notes_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "tags": tags,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return _ok("note_add", f"Saved note {row['id']}", {"note": row, "path": _rel(ctx.workspace_root, path)})


def _tool_note_list(ctx: ToolContext, args: JsonObject) -> ToolResult:
    limit = _int_arg(args, "limit", 10, 1, 50)
    path = _notes_path(ctx)
    if not path.exists():
        return _ok("note_list", "No notes saved yet.", {"notes": []})
    rows: list[JsonObject] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    notes = rows[-limit:]
    lines = [f"{len(notes)} recent notes"]
    for row in notes:
        tags = ", ".join(row.get("tags", [])) if isinstance(row.get("tags"), list) else ""
        suffix = f" [{tags}]" if tags else ""
        lines.append(f"- {row.get('created_at', '')}: {row.get('text', '')}{suffix}")
    return _ok("note_list", "\n".join(lines), {"notes": notes})
