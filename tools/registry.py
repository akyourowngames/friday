import inspect
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from functools import wraps

_tools = {}
_DISPATCH_VERSION = "2.0.0"
_RESPONSE_FORMATS = ("legacy", "structured")
_MAX_TIMEOUT_MS = 60000

_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def tool(name=None, description=None, examples=None, param_descriptions=None):
    def decorator(func):
        nonlocal name, description
        if name is None:
            name = func.__name__
        if description is None:
            description = func.__doc__ or ""

        sig = inspect.signature(func)
        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            param_type = param.annotation if param.annotation is not inspect.Parameter.empty else str
            json_type = _TYPE_MAP.get(param_type, "string")

            pd = (param_descriptions or {}).get(param_name, param_name)
            properties[param_name] = {
                "type": json_type,
                "description": pd,
            }

            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        schema = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

        _tools[name] = {
            "name": name,
            "description": description,
            "examples": examples or [],
            "function": func,
            "parameters": schema,
        }

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator


def get_tool(name):
    return _tools.get(name)


def get_tools():
    return list(_tools.values())


def get_tool_schemas():
    schemas = []
    for name, info in _tools.items():
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": info["parameters"],
            },
        })
    return schemas


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return bool(value)


def _normalize_timeout(timeout_ms):
    if timeout_ms is None:
        return None, None
    if isinstance(timeout_ms, bool):
        return None, _error_payload(
            "INVALID_TIMEOUT",
            "timeout_ms must be an integer between 1 and 60000.",
            "timeout_ms",
            timeout_ms,
            "integer 1..60000",
            False,
            "Pass a positive integer timeout in milliseconds, or omit timeout_ms.",
        )
    try:
        normalized = int(timeout_ms)
    except (TypeError, ValueError):
        return None, _error_payload(
            "INVALID_TIMEOUT",
            "timeout_ms must be an integer between 1 and 60000.",
            "timeout_ms",
            timeout_ms,
            "integer 1..60000",
            False,
            "Pass a positive integer timeout in milliseconds, or omit timeout_ms.",
        )
    if normalized < 1 or normalized > _MAX_TIMEOUT_MS:
        return None, _error_payload(
            "INVALID_TIMEOUT",
            "timeout_ms is outside the supported range.",
            "timeout_ms",
            timeout_ms,
            "integer 1..60000",
            False,
            "Use a timeout between 1 and 60000 milliseconds.",
        )
    return normalized, None


def _signature_text(sig):
    parts = []
    for param_name, param in sig.parameters.items():
        annotation = param.annotation
        parts.append(f"{param_name}: {_TYPE_MAP.get(annotation, '?')}")
    return ", ".join(parts)


def _error_payload(code, message, field, value, expected, retryable, suggestion):
    return {
        "code": code,
        "message": message,
        "field": field,
        "value": value,
        "expected": expected,
        "retryable": retryable,
        "suggestion": suggestion,
    }


def _meta(tool_name, started, attempt=1):
    return {
        "tool": tool_name,
        "version": _DISPATCH_VERSION,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "attempt": attempt,
        "timestamp": _now_iso(),
    }


def _structured_success(tool_name, output, arguments_used, started, trace=None):
    response = {
        "result": {
            "tool": tool_name,
            "output": output,
            "arguments_used": arguments_used,
        },
        "meta": _meta(tool_name, started),
    }
    if trace is not None:
        response["trace"] = trace
    return response


def _structured_error(tool_name, error, started, trace=None):
    response = {
        "error": error,
        "meta": _meta(tool_name, started),
    }
    if trace is not None:
        response["trace"] = trace
    return response


def _make_trace(tool_name, started_at, started, inputs_received, schema_valid, execution_path, status, output_fields, error_code=None):
    return {
        "event": "TOOL TRACE",
        "tool": tool_name,
        "version": _DISPATCH_VERSION,
        "call_id": str(uuid.uuid4()),
        "started_at": started_at,
        "inputs_received": inputs_received,
        "schema_valid": "YES" if schema_valid else "NO",
        "execution_path": execution_path,
        "external_calls": {
            "count": 0,
            "systems": [],
        },
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "output_fields": output_fields,
        "status": status,
        "error_code": error_code,
    }


def _emit_trace(trace, enabled):
    if enabled:
        print(json.dumps(trace, sort_keys=True))


def _legacy_type_error(name, sig, error):
    return f"Error executing '{name}': {error}. Signature: {name}({_signature_text(sig)})"


def _call_with_optional_timeout(func, kwargs, timeout_ms):
    if timeout_ms is None:
        return func(**kwargs), None
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func, **kwargs)
    try:
        return future.result(timeout=timeout_ms / 1000), None
    except TimeoutError:
        future.cancel()
        return None, "timeout"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def execute_tool(name, response_format="legacy", trace_enabled=False, timeout_ms=None, **kwargs):
    started = time.perf_counter()
    started_at = _now_iso()
    requested_response_format = response_format
    trace_enabled = _coerce_bool(trace_enabled)
    if response_format not in _RESPONSE_FORMATS:
        response_format = "legacy"

    if name not in _tools:
        error = _error_payload(
            "TOOL_NOT_FOUND",
            f"Tool '{name}' was not found.",
            "name",
            name,
            "registered tool name",
            False,
            "Use one of the tool names returned by get_tools().",
        )
        trace = _make_trace(name, started_at, started, len(kwargs), False, "lookup", "FAILED", 1, error["code"])
        _emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return _structured_error(name, error, started, trace if trace_enabled else None)
        return f"Error: Tool '{name}' not found"

    func = _tools[name]["function"]
    sig = inspect.signature(func)
    valid = set(sig.parameters.keys())
    tool_kwargs = dict(kwargs)
    timeout_value_for_dispatcher = timeout_ms
    trace_enabled_for_dispatcher = trace_enabled
    forwarded_tool_controls = False
    if "response_format" in valid:
        tool_kwargs["response_format"] = requested_response_format
        response_format = "legacy"
        forwarded_tool_controls = True
    if "trace_enabled" in valid:
        tool_kwargs["trace_enabled"] = trace_enabled
        trace_enabled_for_dispatcher = False
        forwarded_tool_controls = True
    if "timeout_ms" in valid:
        tool_kwargs["timeout_ms"] = timeout_ms
        timeout_value_for_dispatcher = None
        forwarded_tool_controls = True
    unknown = set(tool_kwargs.keys()) - valid
    if unknown:
        accepted = ", ".join(sorted(valid))
        received = ", ".join(sorted(unknown))
        error = _error_payload(
            "UNKNOWN_PARAMETER",
            f"Tool '{name}' received unknown parameter(s).",
            "parameters",
            received,
            accepted,
            False,
            "Remove unsupported parameters or inspect the tool schema before dispatch.",
        )
        trace = _make_trace(name, started_at, started, len(tool_kwargs), False, "schema_validation", "FAILED", 1, error["code"])
        _emit_trace(trace, trace_enabled_for_dispatcher)
        if response_format == "structured":
            return _structured_error(name, error, started, trace if trace_enabled_for_dispatcher else None)
        return (
            f"Error: '{name}' received unknown parameter(s): {received}. "
            f"Accepted: {accepted}"
        )

    timeout_value, timeout_error = _normalize_timeout(timeout_value_for_dispatcher)
    if timeout_error is not None:
        trace = _make_trace(name, started_at, started, len(tool_kwargs), False, "input_validation", "FAILED", 1, timeout_error["code"])
        _emit_trace(trace, trace_enabled_for_dispatcher)
        if response_format == "structured":
            return _structured_error(name, timeout_error, started, trace if trace_enabled_for_dispatcher else None)
        return f"Error executing '{name}': invalid timeout_ms"

    try:
        result, call_error = _call_with_optional_timeout(func, tool_kwargs, timeout_value)
        if call_error == "timeout":
            error = _error_payload(
                "TOOL_TIMEOUT",
                f"Tool '{name}' exceeded timeout_ms.",
                "timeout_ms",
                timeout_value,
                "tool completion before timeout",
                True,
                "Retry with a larger timeout or narrow the requested operation.",
            )
            trace = _make_trace(name, started_at, started, len(tool_kwargs), True, "execute_timeout", "FAILED", 1, error["code"])
            _emit_trace(trace, trace_enabled_for_dispatcher)
            if response_format == "structured":
                return _structured_error(name, error, started, trace if trace_enabled_for_dispatcher else None)
            return f"Error executing '{name}': timed out after {timeout_value}ms"
        if forwarded_tool_controls and requested_response_format == "structured":
            return result
        output = str(result) if result is not None else "Done"
        trace = _make_trace(name, started_at, started, len(tool_kwargs), True, "execute", "SUCCESS", 2)
        _emit_trace(trace, trace_enabled_for_dispatcher)
        if response_format == "structured":
            return _structured_success(name, output, dict(tool_kwargs), started, trace if trace_enabled_for_dispatcher else None)
        return output
    except TypeError as e:
        error = _error_payload(
            "TOOL_TYPE_ERROR",
            f"Tool '{name}' received arguments that do not match its callable signature.",
            "parameters",
            sorted(tool_kwargs),
            _signature_text(sig),
            False,
            "Inspect the tool schema and pass values matching the callable signature.",
        )
        trace = _make_trace(name, started_at, started, len(tool_kwargs), True, "execute_type_error", "FAILED", 1, error["code"])
        _emit_trace(trace, trace_enabled_for_dispatcher)
        if response_format == "structured":
            return _structured_error(name, error, started, trace if trace_enabled_for_dispatcher else None)
        return _legacy_type_error(name, sig, e)
    except Exception as e:
        error = _error_payload(
            "TOOL_EXECUTION_ERROR",
            f"Tool '{name}' failed while executing.",
            "tool",
            name,
            "successful tool execution",
            True,
            "Retry if the operation is safe, or inspect internal logs for raw exception details.",
        )
        trace = _make_trace(name, started_at, started, len(tool_kwargs), True, "execute_exception", "FAILED", 1, error["code"])
        _emit_trace(trace, trace_enabled_for_dispatcher)
        if response_format == "structured":
            return _structured_error(name, error, started, trace if trace_enabled_for_dispatcher else None)
        return f"Error executing '{name}': {e.__class__.__name__}"
