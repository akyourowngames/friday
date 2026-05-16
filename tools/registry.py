import inspect
from functools import wraps

_tools = {}

_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def tool(name=None, description=None, examples=None):
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

            properties[param_name] = {
                "type": json_type,
                "description": f"{param_name}",
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


def execute_tool(name, **kwargs):
    if name not in _tools:
        return f"Error: Tool '{name}' not found"
    try:
        result = _tools[name]["function"](**kwargs)
        return str(result) if result is not None else "Done"
    except Exception as e:
        return f"Error executing '{name}': {e}"
