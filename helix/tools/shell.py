"""Shell command execution, sandboxed to the workspace with a timeout."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from .base import ToolContext, ToolResult

# Commands that could cause catastrophic or out-of-scope damage.
_DENY = {
    "rm -rf /",
    "mkfs",
    "shutdown",
    "reboot",
    ":(){:|:&};:",
}


class ShellTool:
    name = "shell"
    description = (
        "Run a shell command inside the workspace and return its stdout/stderr. "
        "Use for building, testing, and inspecting the project. Long output is truncated."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "cwd": {
                "type": "string",
                "description": "Working directory relative to the workspace (default '.').",
            },
        },
        "required": ["command"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, command: str, cwd: str = ".") -> ToolResult:
        for bad in _DENY:
            if bad in command:
                return ToolResult(ok=False, output=f"refused: command contains '{bad}'")
        workdir = (Path(self.ctx.workspace) / cwd).resolve()
        if Path(self.ctx.workspace) not in workdir.parents and workdir != Path(self.ctx.workspace):
            return ToolResult(ok=False, output="refused: cwd escapes workspace")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            return ToolResult(ok=False, output=f"failed to start: {e}")
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.ctx.shell_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(ok=False, output=f"timeout after {self.ctx.shell_timeout}s")
        text = stdout.decode("utf-8", errors="replace")
        if len(text) > 20000:
            text = text[:20000] + f"\n... [truncated {len(text) - 20000} chars]"
        return ToolResult(ok=proc.returncode == 0, output=text or "(no output)", meta={"returncode": proc.returncode})

    @staticmethod
    def parse(command: str) -> list[str]:
        return shlex.split(command)
