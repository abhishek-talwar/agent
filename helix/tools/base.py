"""Tool base classes and registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from ..diff_engine import DiffEngine


@dataclass
class ToolResult:
    ok: bool
    output: str
    meta: dict[str, Any] | None = None

    def to_text(self) -> str:
        return self.output


class Tool(Protocol):
    name: str
    description: str
    parameters: dict

    async def run(self, **kwargs: Any) -> ToolResult: ...


def tool_schema(tool: "Tool") -> dict:
    """Build an OpenAI-style function schema from a tool's attributes.

    Built here rather than as a Protocol method so that plain classes satisfy the
    protocol structurally without needing to inherit anything.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


@dataclass
class ToolContext:
    """Shared state handed to every tool."""

    workspace: "PathLike"  # type: ignore[name-defined]
    diff_engine: DiffEngine
    home: "PathLike"  # type: ignore[name-defined]
    shell_timeout: float = 120.0
    require_diff_approval: bool = True
    http_fetch: Callable[[str], Awaitable[str]] | None = None


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [tool_schema(t) for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())


from pathlib import Path  # noqa: E402  (typing convenience for ToolContext)
PathLike = Path
