"""Run a Python snippet in a subprocess with a timeout.

Used by the solver to *test hypotheses* (compute a value, reproduce a bug,
validate a fix) rather than guessing.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from .base import ToolContext, ToolResult


class PythonExecTool:
    name = "python_exec"
    description = (
        "Execute a Python snippet in a subprocess and return stdout/stderr. "
        "Use this to test hypotheses and verify behavior empirically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."},
        },
        "required": ["code"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, code: str) -> ToolResult:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(self.ctx.home)) as f:
            f.write(code)
            tmp = f.name
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.ctx.workspace), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                tmp,
                cwd=str(self.ctx.workspace),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.ctx.shell_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(ok=False, output=f"timeout after {self.ctx.shell_timeout}s")
            text = stdout.decode("utf-8", errors="replace")
            if len(text) > 20000:
                text = text[:20000] + "\n... [truncated]"
            return ToolResult(ok=proc.returncode == 0, output=text or "(no output)", meta={"returncode": proc.returncode})
        finally:
            Path(tmp).unlink(missing_ok=True)
