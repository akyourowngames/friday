from __future__ import annotations

import ast
import math
import operator
from typing import Callable

from .args import str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, fail, ok, schema


SPEC = ToolSpec(
    name="calculator",
    description="Safely evaluate arithmetic expressions.",
    parameters=schema({"expression": {"type": "string"}}, required=("expression",)),
    examples=("calculator expression=\"(22 / 7) * 3\"",),
)

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


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    expression = str_arg(args, "expression")
    if not expression:
        return fail("calculator", "expression is required")
    if len(expression) > 240:
        return fail("calculator", "Expression is too long.")
    tree = ast.parse(expression, mode="eval")
    value = _eval_math(tree.body)
    return ok("calculator", f"{expression} = {value}", {"expression": expression, "value": value})


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
