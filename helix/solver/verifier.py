"""Verify a problem against its acceptance criteria.

This is the guard against the classic agent failure mode of *claiming* success.
Each criterion is executed and recorded with the raw evidence that satisfied or
violated it. Nothing is "solved" until every criterion passes.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..tools.base import ToolContext
from .models import Criterion, CriterionKind, Problem


class Verifier:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def check(self, criterion: Criterion) -> Criterion:
        kind = criterion.kind
        if kind == CriterionKind.SHELL:
            criterion.satisfied, criterion.evidence = await self._run_shell(criterion.value)
        elif kind == CriterionKind.PYTHON:
            criterion.satisfied, criterion.evidence = await self._run_python(criterion.value)
        elif kind == CriterionKind.FILE_EXISTS:
            path = Path(self.ctx.workspace) / criterion.value
            criterion.satisfied = path.exists()
            criterion.evidence = f"{criterion.value} {'exists' if path.exists() else 'is missing'}"
        elif kind == CriterionKind.CONTAINS:
            criterion.satisfied, criterion.evidence = self._contains(criterion)
        else:  # pragma: no cover - exhaustive enum
            criterion.satisfied = False
            criterion.evidence = f"unknown criterion kind: {kind}"
        return criterion

    async def check_all(self, problem: Problem) -> tuple[bool, list[Criterion]]:
        results = [await self.check(c) for c in problem.criteria]
        return all(c.satisfied for c in results), results

    def _python_env(self) -> dict:
        """Environment for python snippets so ``import <workspace module>`` works.

        The snippet lives in a temp file, so its own directory (not the cwd) is on
        ``sys.path``; we prepend the workspace explicitly.
        """
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.ctx.workspace), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        return env

    async def _run_shell(self, command: str) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.ctx.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.ctx.shell_timeout)
        except asyncio.TimeoutError:
            return False, f"timeout after {self.ctx.shell_timeout}s: {command}"
        except OSError as e:
            return False, f"failed to run: {e}"
        text = stdout.decode("utf-8", errors="replace")[:4000]
        return proc.returncode == 0, f"$ {command}\n{text}".strip()

    async def _run_python(self, code: str) -> tuple[bool, str]:
        import sys
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(self.ctx.home)) as f:
            f.write(code)
            tmp = f.name
        env = self._python_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                tmp,
                cwd=str(self.ctx.workspace),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.ctx.shell_timeout)
            text = stdout.decode("utf-8", errors="replace")[:4000]
            return proc.returncode == 0, text.strip()
        except asyncio.TimeoutError:
            return False, "python check timed out"
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _contains(self, criterion: Criterion) -> tuple[bool, str]:
        path = Path(self.ctx.workspace) / (criterion.path or "")
        if not path.exists():
            return False, f"{criterion.path} is missing"
        text = path.read_text(errors="replace")
        if criterion.pattern is None:
            ok = criterion.value in text
            return ok, f"{'found' if ok else 'not found'} {criterion.value!r} in {criterion.path}"
        ok = re.search(criterion.pattern, text) is not None
        return ok, f"pattern {criterion.pattern!r} {'matched' if ok else 'did not match'} in {criterion.path}"
