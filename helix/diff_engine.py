"""Reviewable, hunk-level diff engine.

This is the feature that is missing from Hermes and OpenClaw: every mutation the
agent proposes is turned into a unified diff that a human can inspect, accept or
reject *per hunk*, and roll back. Nothing is written to disk until it is approved
(unless the caller explicitly bypasses approval).

Design goals:
  * Deterministic and dependency-free (stdlib ``difflib``).
  * Hunk-level granularity so a reviewer can accept one change and reject another.
  * Stale-diff detection: if the file changed on disk since the diff was built,
    applying it is refused instead of silently clobbering the user's work.
  * Persistent sessions so diffs survive process restarts and can be rolled back.
"""

from __future__ import annotations

import difflib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

HunkKind = Literal["context", "add", "remove"]


@dataclass
class Hunk:
    """A contiguous block of changes within a single file."""

    id: str
    old_start: int  # 1-based line number in the original file
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[HunkKind, str]] = field(default_factory=list)

    @property
    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "header": self.header,
            "lines": [{"kind": k, "text": t} for k, t in self.lines],
            "added": sum(1 for k, _ in self.lines if k == "add"),
            "removed": sum(1 for k, _ in self.lines if k == "remove"),
        }


@dataclass
class FileDiff:
    """The diff for one file."""

    path: str  # workspace-relative, POSIX separators
    old_content: str | None  # None means the file did not exist
    new_content: str
    hunks: list[Hunk] = field(default_factory=list)
    is_new_file: bool = False
    is_delete: bool = False
    binary: bool = False

    @property
    def added(self) -> int:
        return sum(h.to_dict()["added"] for h in self.hunks)

    @property
    def removed(self) -> int:
        return sum(h.to_dict()["removed"] for h in self.hunks)

    def unified(self) -> str:
        header = [f"--- {'/dev/null' if self.old_content is None else 'a/' + self.path}"]
        header.append(f"+++ {'/dev/null' if self.is_delete else 'b/' + self.path}")
        body: list[str] = []
        for h in self.hunks:
            body.append(h.header)
            for kind, text in h.lines:
                prefix = {"context": " ", "add": "+", "remove": "-"}[kind]
                body.append(prefix + text)
        return "\n".join(header + body)

    def to_dict(self, include_content: bool = True) -> dict:
        d = {
            "path": self.path,
            "is_new_file": self.is_new_file,
            "is_delete": self.is_delete,
            "binary": self.binary,
            "added": self.added,
            "removed": self.removed,
            "hunks": [h.to_dict() for h in self.hunks],
            "unified": self.unified(),
        }
        if include_content:
            d["old_content"] = self.old_content
            d["new_content"] = self.new_content
        return d


@dataclass
class DiffSet:
    """A named collection of file diffs forming one reviewable proposal."""

    id: str
    title: str
    workspace: Path
    files: list[FileDiff] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | applied | partially_applied | rolled_back | rejected
    reason: str = ""  # why the agent made this change (its "intent")
    applied_hunks: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total_added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def total_removed(self) -> int:
        return sum(f.removed for f in self.files)

    def to_dict(self, include_content: bool = True) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "reason": self.reason,
            "status": self.status,
            "created_at": self.created_at,
            "workspace": str(self.workspace),
            "total_added": self.total_added,
            "total_removed": self.total_removed,
            "files": [f.to_dict(include_content=include_content) for f in self.files],
            "applied_hunks": self.applied_hunks,
        }

    def unified(self) -> str:
        return "\n".join(f.unified() for f in self.files)


def _splitlines_keepends(text: str) -> list[str]:
    """Split into lines without trailing newlines, preserving an empty tail faithfully."""
    return text.splitlines()


def build_hunks(old: str, new: str, context: int = 3) -> list[Hunk]:
    """Turn two versions of a file into a list of unified-diff hunks."""
    old_lines = _splitlines_keepends(old)
    new_lines = _splitlines_keepends(new)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    hunks: list[Hunk] = []
    for group in matcher.get_grouped_opcodes(context):
        first = group[0]
        last = group[-1]
        old_start = first[1] + 1
        old_count = last[2] - first[1]
        new_start = first[3] + 1
        new_count = last[4] - first[3]
        lines: list[tuple[HunkKind, str]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for line in old_lines[i1:i2]:
                    lines.append(("context", line))
            elif tag == "replace":
                for line in old_lines[i1:i2]:
                    lines.append(("remove", line))
                for line in new_lines[j1:j2]:
                    lines.append(("add", line))
            elif tag == "delete":
                for line in old_lines[i1:i2]:
                    lines.append(("remove", line))
            elif tag == "insert":
                for line in new_lines[j1:j2]:
                    lines.append(("add", line))
        hunks.append(
            Hunk(
                id=uuid.uuid4().hex[:8],
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=lines,
            )
        )
    return hunks


def apply_hunks_to_text(old: str, hunks: list[Hunk], accepted_ids: set[str]) -> str:
    """Apply only the accepted hunks to ``old`` and return the resulting text.

    Hunks must be sorted by ``old_start`` (they are, as produced by difflib).
    """
    old_lines = _splitlines_keepends(old)
    out: list[str] = []
    cursor = 0
    for h in sorted(hunks, key=lambda x: x.old_start):
        out.extend(old_lines[cursor : h.old_start - 1])
        if h.id in accepted_ids:
            out.extend(t for k, t in h.lines if k != "remove")
        else:
            out.extend(t for k, t in h.lines if k != "add")
        cursor = h.old_start - 1 + h.old_count
    out.extend(old_lines[cursor:])
    return "\n".join(out) + ("\n" if old.endswith("\n") or not old_lines else "")


class DiffError(RuntimeError):
    """Raised when a diff cannot be applied safely."""


class DiffEngine:
    """Creates, stores, applies and rolls back diff sets."""

    def __init__(self, workspace: Path, store_dir: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, DiffSet] = {}

    # ---- path safety -------------------------------------------------------
    def _resolve(self, rel_path: str) -> Path:
        target = (self.workspace / rel_path).resolve()
        if self.workspace not in target.parents and target != self.workspace:
            raise DiffError(f"path escapes workspace: {rel_path}")
        return target

    def _read(self, rel_path: str) -> tuple[str | None, bool]:
        """Return (content, existed). None content means the file was absent."""
        path = self._resolve(rel_path)
        if not path.exists():
            return None, False
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            return None, True  # binary; we don't diff binaries
        return raw.decode("utf-8", errors="replace"), True

    # ---- proposal ----------------------------------------------------------
    def propose_file(self, rel_path: str, new_content: str, *, reason: str = "") -> DiffSet:
        """Propose replacing a file's entire contents with ``new_content``."""
        old, existed = self._read(rel_path)
        fdiff = self._make_file_diff(rel_path, old, existed, new_content)
        ds = DiffSet(
            id=uuid.uuid4().hex[:12],
            title=f"Edit {rel_path}",
            workspace=self.workspace,
            files=[fdiff],
            reason=reason,
        )
        self._save(ds)
        return ds

    def propose_edit(
        self,
        rel_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
        reason: str = "",
    ) -> DiffSet:
        """Propose a targeted string replacement, computed as a real diff."""
        current, existed = self._read(rel_path)
        if not existed or current is None:
            raise DiffError(f"file does not exist: {rel_path}")
        if old_string not in current:
            raise DiffError("old_string not found in file")
        count = current.count(old_string)
        if count > 1 and not replace_all:
            raise DiffError(
                f"old_string is not unique ({count} matches); pass replace_all=true"
            )
        updated = (
            current.replace(old_string, new_string)
            if replace_all
            else current.replace(old_string, new_string, 1)
        )
        fdiff = self._make_file_diff(rel_path, current, True, updated)
        ds = DiffSet(
            id=uuid.uuid4().hex[:12],
            title=f"Edit {rel_path}",
            workspace=self.workspace,
            files=[fdiff],
            reason=reason,
        )
        self._save(ds)
        return ds

    def propose_multi(self, edits: Iterable[dict], *, title: str = "Multi-file change", reason: str = "") -> DiffSet:
        """Propose several file writes/deletes atomically as one reviewable set.

        Each edit dict: {"path": str, "content": str | None}. ``None`` content
        means delete the file.
        """
        files: list[FileDiff] = []
        for edit in edits:
            rel = edit["path"]
            content = edit.get("content")
            old, existed = self._read(rel)
            if content is None:
                if not existed:
                    continue
                files.append(
                    FileDiff(path=rel, old_content=old, new_content="", hunks=build_hunks(old or "", ""), is_delete=True)
                )
            else:
                files.append(self._make_file_diff(rel, old, existed, content))
        ds = DiffSet(
            id=uuid.uuid4().hex[:12],
            title=title,
            workspace=self.workspace,
            files=files,
            reason=reason,
        )
        self._save(ds)
        return ds

    def _make_file_diff(self, rel_path: str, old: str | None, existed: bool, new: str) -> FileDiff:
        if not existed:
            return FileDiff(
                path=rel_path,
                old_content=None,
                new_content=new,
                hunks=build_hunks("", new),
                is_new_file=True,
            )
        return FileDiff(
            path=rel_path,
            old_content=old or "",
            new_content=new,
            hunks=build_hunks(old or "", new),
        )

    # ---- persistence -------------------------------------------------------
    def _path_for(self, diff_id: str) -> Path:
        return self.store_dir / f"{diff_id}.json"

    def _save(self, ds: DiffSet) -> None:
        self._cache[ds.id] = ds
        payload = ds.to_dict(include_content=True)
        self._path_for(ds.id).write_text(json.dumps(payload, indent=2))

    def get(self, diff_id: str) -> DiffSet:
        if diff_id in self._cache:
            return self._cache[diff_id]
        path = self._path_for(diff_id)
        if not path.exists():
            raise DiffError(f"unknown diff: {diff_id}")
        data = json.loads(path.read_text())
        ds = self._from_dict(data)
        self._cache[ds.id] = ds
        return ds

    def _from_dict(self, data: dict) -> DiffSet:
        files = []
        for f in data["files"]:
            hunks = [
                Hunk(
                    id=h["id"],
                    old_start=h["old_start"],
                    old_count=h["old_count"],
                    new_start=h["new_start"],
                    new_count=h["new_count"],
                    lines=[(l["kind"], l["text"]) for l in h["lines"]],
                )
                for h in f["hunks"]
            ]
            files.append(
                FileDiff(
                    path=f["path"],
                    old_content=f.get("old_content"),
                    new_content=f.get("new_content", ""),
                    hunks=hunks,
                    is_new_file=f.get("is_new_file", False),
                    is_delete=f.get("is_delete", False),
                )
            )
        return DiffSet(
            id=data["id"],
            title=data["title"],
            workspace=Path(data["workspace"]),
            files=files,
            created_at=data.get("created_at", time.time()),
            status=data.get("status", "pending"),
            reason=data.get("reason", ""),
            applied_hunks=data.get("applied_hunks", {}),
        )

    def list_sets(self, limit: int = 50) -> list[DiffSet]:
        items: list[DiffSet] = []
        for path in sorted(self.store_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                items.append(self._from_dict(json.loads(path.read_text())))
            except (json.JSONDecodeError, KeyError):
                continue
            if len(items) >= limit:
                break
        return items

    # ---- apply / rollback --------------------------------------------------
    def check_conflicts(self, ds: DiffSet) -> list[str]:
        """Return paths whose on-disk content no longer matches the diff's base."""
        conflicts: list[str] = []
        for f in ds.files:
            current, existed = self._read(f.path)
            if f.old_content is None:
                if existed:
                    conflicts.append(f.path)
            elif current != f.old_content:
                conflicts.append(f.path)
        return conflicts

    def apply(
        self,
        diff_id: str,
        accepted_hunk_ids: dict[str, list[str]] | None = None,
        *,
        force: bool = False,
    ) -> DiffSet:
        """Apply a diff set.

        ``accepted_hunk_ids`` maps file path -> list of hunk ids to accept. Hunks
        not listed are rejected. If ``None``, all hunks are accepted.
        """
        ds = self.get(diff_id)
        conflicts = self.check_conflicts(ds)
        if conflicts and not force:
            raise DiffError(
                "files changed on disk since the diff was created: "
                + ", ".join(conflicts)
                + " (re-run the change or pass force=true)"
            )
        applied: dict[str, list[str]] = {}
        for f in ds.files:
            if accepted_hunk_ids is None:
                accepted = {h.id for h in f.hunks}
            else:
                accepted = set(accepted_hunk_ids.get(f.path, []))
            applied[f.path] = sorted(accepted)
            target = self._resolve(f.path)
            if f.is_delete:
                if target.exists():
                    target.unlink()
                continue
            if f.old_content is None:
                result = f.new_content
            else:
                result = apply_hunks_to_text(f.old_content, f.hunks, accepted)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result)
        total = sum(len(v) for v in applied.values())
        all_hunks = sum(len(f.hunks) for f in ds.files)
        ds.applied_hunks = applied
        ds.status = "applied" if total == all_hunks else "partially_applied"
        self._save(ds)
        return ds

    def rollback(self, diff_id: str) -> DiffSet:
        """Restore the files in a diff set to their pre-diff contents."""
        ds = self.get(diff_id)
        for f in ds.files:
            target = self._resolve(f.path)
            if f.old_content is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f.old_content)
        ds.status = "rolled_back"
        self._save(ds)
        return ds

    def preview(self, diff_id: str) -> dict:
        """Dry-run: show what would change without touching disk."""
        ds = self.get(diff_id)
        return {
            "diff": ds.to_dict(include_content=False),
            "unified": ds.unified(),
            "conflicts": self.check_conflicts(ds),
            "would_write": [f.path for f in ds.files if not f.is_delete],
            "would_delete": [f.path for f in ds.files if f.is_delete],
        }


def make_engine(workspace: Path, home: Path) -> DiffEngine:
    return DiffEngine(workspace=workspace, store_dir=Path(home) / "diffs")
