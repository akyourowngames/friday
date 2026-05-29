"""Calculator tool: safe arithmetic evaluation.

Evaluates math expressions using an AST whitelist (no eval, no exec, no regex).
Supports + - * / // % ** parentheses, unary minus, and a small set of named
functions/constants. Anything outside the whitelist is rejected with a typed
error.
"""

import ast
import math
import operator
import time

from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    structured_error,
    structured_success,
    utc_now_iso,
)

_CALC_VERSION = "1.0.0"

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor,
    "ceil": math.ceil, "log": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "pow": math.pow,
    "min": min, "max": max,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


class _UnsafeExpression(Exception):
    pass


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise _UnsafeExpression("only numeric constants are allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise _UnsafeExpression(f"unknown name '{node.id}'")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise _UnsafeExpression("unknown or disallowed function")
        if node.keywords:
            raise _UnsafeExpression("keyword arguments are not allowed")
        args = [_eval_node(arg) for arg in node.args]
        return _FUNCS[node.func.id](*args)
    raise _UnsafeExpression("unsupported expression element")


def _trace(name, started_at, started, schema_valid, status, output_fields, error_code=None):
    return make_trace(
        name, _CALC_VERSION, started_at, started, 1, schema_valid,
        "calc", status, output_fields, {"count": 0, "systems": []}, error_code,
    )


def _emit(name, started, started_at, trace_enabled, result=None, error=None, response_format="legacy", legacy="", status="SUCCESS"):
    valid = error is None
    trace = _trace(name, started_at, started, valid, status if valid else "FAILED",
                   len(result) if result else 1, None if valid else error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        if valid:
            return structured_success(name, _CALC_VERSION, result, started, trace)
        return structured_error(name, _CALC_VERSION, error, started, trace)
    return legacy


@tool(
    name="calc",
    description="Evaluate a math expression safely. Supports + - * / // % ** parentheses and functions like sqrt, log, sin, min, max, plus constants pi and e.",
    examples=[
        "calculate 15% of 2400",
        "what is sqrt(144) + 3**2",
        "compute (1280*720)/1e6",
    ],
    param_descriptions={
        "expression": "Arithmetic expression to evaluate",
        "response_format": "legacy or structured",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def calc(expression: str, response_format: str = "legacy", trace_enabled: bool = False):
    started = time.perf_counter()
    started_at = utc_now_iso()
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)

    expr = str(expression or "").strip()
    if not expr:
        err = error_payload("EMPTY_EXPRESSION", "expression must not be empty.", "expression", expression, "math expression", False, "Provide an expression like '2 + 2'.")
        return _emit("calc", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: expression is required", status="FAILED")

    try:
        parsed = ast.parse(expr, mode="eval")
        value = _eval_node(parsed)
    except _UnsafeExpression as exc:
        err = error_payload("UNSAFE_EXPRESSION", str(exc), "expression", expr, "whitelisted arithmetic", False, "Use only numbers, arithmetic operators, and allowed functions.")
        return _emit("calc", started, started_at, trace_enabled, error=err, response_format=response_format, legacy=f"Error: {exc}", status="FAILED")
    except (SyntaxError, ValueError):
        err = error_payload("INVALID_EXPRESSION", "Could not parse the expression.", "expression", expr, "valid math expression", False, "Check the syntax of your expression.")
        return _emit("calc", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: invalid expression", status="FAILED")
    except ZeroDivisionError:
        err = error_payload("DIVISION_BY_ZERO", "Division by zero.", "expression", expr, "non-zero divisor", False, "Avoid dividing by zero.")
        return _emit("calc", started, started_at, trace_enabled, error=err, response_format=response_format, legacy="Error: division by zero", status="FAILED")

    result = {"expression": expr, "value": value}
    return _emit("calc", started, started_at, trace_enabled, result=result, response_format=response_format, legacy=f"{expr} = {value}")
