import json

from tools.registry import get_tool, get_tools, execute_tool


class ToolValidator:
    def validate(self, name, args):
        tool = get_tool(name)
        if not tool:
            available = ", ".join(t["name"] for t in get_tools()) if get_tools() else "none"
            return False, f"Tool '{name}' not found. Available: {available}"

        schema = tool.get("parameters", {})
        required = schema.get("required", [])

        for param in required:
            if param not in args:
                return False, f"Missing required parameter '{param}' for '{name}'"

        props = schema.get("properties", {})
        type_map = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}

        for param, value in args.items():
            if param in props:
                expected = props[param].get("type", "string")
                if expected == "integer" and type(value) is bool:
                    return False, f"Parameter '{param}' should be integer, got boolean"
                py_type = type_map.get(expected)
                if py_type and not isinstance(value, py_type):
                    return False, f"Parameter '{param}' should be {expected}, got {type(value).__name__}"

        return True, None

    def validate_and_execute(self, name, args):
        valid, error = self.validate(name, args)
        if not valid:
            return False, error
        result = execute_tool(name, **args)
        return True, result
