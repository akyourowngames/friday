from tools.registry import get_tool, get_tools, execute_tool


class ToolValidator:
    def validate(self, name, args):
        tool = get_tool(name)
        if not tool:
            available = ", ".join(t["name"] for t in get_tools()) if get_tools() else "none"
            return False, f"Tool '{name}' not found. Available: {available}"

        schema = tool.get("parameters", {})
        required = schema.get("required", [])
        props = schema.get("properties", {})
        unknown = sorted(param for param in args if param not in props)
        if unknown:
            accepted = ", ".join(sorted(props)) if props else "none"
            return (
                False,
                f"Unknown parameter(s) for '{name}': {', '.join(unknown)}. Accepted: {accepted}",
            )

        for param in required:
            if param not in args:
                return False, f"Missing required parameter '{param}' for '{name}'"

        for param, value in list(args.items()):
            expected = props[param].get("type", "string")
            if expected == "integer" and type(value) is bool:
                return False, f"Parameter '{param}' should be integer, got boolean"
            if expected == "integer" and isinstance(value, str):
                try:
                    args[param] = int(value)
                except ValueError:
                    if param in required:
                        return False, f"Parameter '{param}' should be integer, got string"
                    args.pop(param, None)
                    continue
            if expected == "integer" and isinstance(value, float) and value.is_integer():
                args[param] = int(value)
            elif expected == "number" and isinstance(value, str):
                try:
                    args[param] = float(value)
                except ValueError:
                    return False, f"Parameter '{param}' should be number, got string"
            elif expected == "boolean" and isinstance(value, str):
                if value.lower() in ("true", "1", "yes"):
                    args[param] = True
                elif value.lower() in ("false", "0", "no"):
                    args[param] = False

        return True, None

    def validate_and_execute(self, name, args):
        valid, error = self.validate(name, args)
        if not valid:
            return False, error
        result = execute_tool(name, **args)
        return True, result
