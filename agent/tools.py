import inspect
import json
from typing import Callable, Any

from pydantic import BaseModel


class ToolRegistry:
    """Central registry mapping tool names to Python functions with OpenAI-compatible JSON schemas."""

    def __init__(self):
        self._tools: dict[str, Callable] = {}
        self._definitions: list[dict] = []

    def register(self, name: str, description: str, parameters_model: type[BaseModel]):
        """Decorator factory that registers a function as a callable tool."""
        def decorator(func: Callable) -> Callable:
            schema = parameters_model.model_json_schema()
            # Remove pydantic metadata keys that OpenAI/DeepSeek doesn't expect
            schema.pop("title", None)
            # Ensure we have the required fields list from pydantic
            if "required" not in schema:
                schema["required"] = []
            self._tools[name] = func
            self._definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                },
            })
            return func
        return decorator

    def get_tool_definitions(self) -> list[dict]:
        """Return all tool definitions in OpenAI tools format."""
        return self._definitions

    async def execute(self, name: str, arguments: dict) -> Any:
        """Execute a registered tool by name with the given arguments."""
        if name not in self._tools:
            return {"error": f"Unknown tool: {name}"}
        func = self._tools[name]
        try:
            if inspect.iscoroutinefunction(func):
                result = await func(**arguments)
            else:
                result = func(**arguments)
            return result
        except Exception as e:
            return {"error": f"Tool '{name}' failed: {type(e).__name__}: {str(e)}"}
