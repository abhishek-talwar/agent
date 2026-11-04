"""Tool registry assembly."""

from __future__ import annotations

from .base import Registry, ToolContext, ToolResult
from .files import EditFileTool, ListFilesTool, ReadFileTool, SearchTool, WriteFileTool
from .python_exec import PythonExecTool
from .shell import ShellTool
from .web import WebFetchTool, WebSearchTool

__all__ = [
    "Registry",
    "ToolContext",
    "ToolResult",
    "build_registry",
]


def build_registry(ctx: ToolContext) -> Registry:
    reg = Registry()
    for tool_cls in (
        ShellTool,
        ReadFileTool,
        ListFilesTool,
        SearchTool,
        WriteFileTool,
        EditFileTool,
        PythonExecTool,
        WebFetchTool,
        WebSearchTool,
    ):
        reg.register(tool_cls(ctx))
    return reg
