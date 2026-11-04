"""File tools.

Critically, every mutating file tool goes through the :class:`DiffEngine` instead
of writing directly. That is how Helix guarantees *reviewable diffs*: the tool
returns a diff id, and the write only lands when the diff is approved.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from ..diff_engine import DiffError
from .base import ToolContext, ToolResult

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build"}


def _safe(workspace: Path, rel: str) -> Path:
    target = (workspace / rel).resolve()
    if workspace not in target.parents and target != workspace:
        raise DiffError(f"path escapes workspace: {rel}")
    return target


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace. Returns numbered lines."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "start": {"type": "integer", "description": "First line (1-based)."},
            "end": {"type": "integer", "description": "Last line (inclusive)."},
        },
        "required": ["path"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, path: str, start: int | None = None, end: int | None = None) -> ToolResult:
        try:
            target = _safe(Path(self.ctx.workspace), path)
        except DiffError as e:
            return ToolResult(ok=False, output=str(e))
        if not target.exists() or not target.is_file():
            return ToolResult(ok=False, output=f"not a file: {path}")
        text = target.read_text(errors="replace")
        lines = text.splitlines()
        s = max(1, start or 1)
        e = min(len(lines), end or len(lines))
        numbered = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(s, e + 1))
        return ToolResult(ok=True, output=numbered or "(empty file)", meta={"lines": len(lines)})


class ListFilesTool:
    name = "list_files"
    description = "List files and directories in the workspace, skipping VCS and build dirs."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory relative to workspace."},
            "glob": {"type": "string", "description": "Optional glob filter, e.g. '*.py'."},
        },
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, path: str = ".", glob: str | None = None) -> ToolResult:
        root = Path(self.ctx.workspace)
        try:
            base = _safe(root, path)
        except DiffError as e:
            return ToolResult(ok=False, output=str(e))
        if not base.exists():
            return ToolResult(ok=False, output=f"no such directory: {path}")
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                full = Path(dirpath) / fn
                rel = full.relative_to(root).as_posix()
                if glob and not fnmatch.fnmatch(fn, glob):
                    continue
                out.append(rel)
                if len(out) >= 2000:
                    break
            if len(out) >= 2000:
                break
        return ToolResult(ok=True, output="\n".join(sorted(out)) or "(no files)")


class SearchTool:
    name = "search"
    description = "Search file contents with a regular expression across the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regular expression."},
            "glob": {"type": "string", "description": "File glob filter, e.g. '*.py'."},
            "max_results": {"type": "integer", "description": "Cap on matches (default 100)."},
        },
        "required": ["pattern"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, pattern: str, glob: str | None = None, max_results: int = 100) -> ToolResult:
        import re

        root = Path(self.ctx.workspace)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return ToolResult(ok=False, output=f"invalid regex: {e}")
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if glob and not fnmatch.fnmatch(fn, glob):
                    continue
                full = Path(dirpath) / fn
                try:
                    raw = full.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:4096]:
                    continue
                for i, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        rel = full.relative_to(root).as_posix()
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= max_results:
                            break
                if len(hits) >= max_results:
                    break
            if len(hits) >= max_results:
                break
        return ToolResult(ok=True, output="\n".join(hits) or "(no matches)")


class WriteFileTool:
    name = "write_file"
    description = (
        "Propose writing a file. This does NOT write immediately: it creates a reviewable "
        "diff and returns its id. The change is applied only after approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "content": {"type": "string", "description": "Full new file contents."},
            "reason": {"type": "string", "description": "Why this change is needed."},
        },
        "required": ["path", "content"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, path: str, content: str, reason: str = "") -> ToolResult:
        try:
            ds = self.ctx.diff_engine.propose_file(path, content, reason=reason)
        except DiffError as e:
            return ToolResult(ok=False, output=str(e))
        meta = {"diff_id": ds.id, "path": path, "added": ds.total_added, "removed": ds.total_removed}
        if not self.ctx.require_diff_approval:
            self.ctx.diff_engine.apply(ds.id)
            return ToolResult(ok=True, output=f"applied {path} (+{ds.total_added}/-{ds.total_removed})", meta=meta)
        return ToolResult(
            ok=True,
            output=(
                f"proposed change to {path} as diff {ds.id} "
                f"(+{ds.total_added}/-{ds.total_removed}). Awaiting approval."
            ),
            meta=meta,
        )


class EditFileTool:
    name = "edit_file"
    description = (
        "Propose a targeted string replacement in a file, emitted as a reviewable diff. "
        "The change is applied only after approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
            "reason": {"type": "string", "description": "Why this change is needed."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        reason: str = "",
    ) -> ToolResult:
        try:
            ds = self.ctx.diff_engine.propose_edit(
                path, old_string, new_string, replace_all=replace_all, reason=reason
            )
        except DiffError as e:
            return ToolResult(ok=False, output=str(e))
        meta = {"diff_id": ds.id, "path": path, "added": ds.total_added, "removed": ds.total_removed}
        if not self.ctx.require_diff_approval:
            self.ctx.diff_engine.apply(ds.id)
            return ToolResult(ok=True, output=f"applied {path} (+{ds.total_added}/-{ds.total_removed})", meta=meta)
        return ToolResult(
            ok=True,
            output=f"proposed edit to {path} as diff {ds.id} (+{ds.total_added}/-{ds.total_removed}). Awaiting approval.",
            meta=meta,
        )
