from __future__ import annotations

from .args import float_arg, str_arg
from .core import JsonObject, ToolContext, ToolResult, ToolSpec, ok, schema


SPEC = ToolSpec(
    name="unit_convert",
    description="Convert temperature, length, mass, or volume units.",
    parameters=schema(
        {
            "value": {"type": "number"},
            "from_unit": {"type": "string"},
            "to_unit": {"type": "string"},
        },
        required=("value", "from_unit", "to_unit"),
    ),
    examples=("unit_convert value=72 from_unit=fahrenheit to_unit=celsius",),
)

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


def run(ctx: ToolContext, args: JsonObject) -> ToolResult:
    value = float_arg(args, "value")
    from_unit = _canonical_unit(str_arg(args, "from_unit"))
    to_unit = _canonical_unit(str_arg(args, "to_unit"))
    if from_unit in {"celsius", "fahrenheit", "kelvin"} or to_unit in {"celsius", "fahrenheit", "kelvin"}:
        result = _convert_temperature(value, from_unit, to_unit)
    else:
        result = _convert_linear(value, from_unit, to_unit)
    text = f"{value:g} {from_unit} = {result:g} {to_unit}"
    return ok("unit_convert", text, {"value": value, "from_unit": from_unit, "to_unit": to_unit, "result": result})


def _canonical_unit(unit: str) -> str:
    clean = str(unit or "").strip().lower()
    return _UNIT_ALIASES.get(clean, clean)


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
