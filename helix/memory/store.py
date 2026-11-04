"""Local-first memory.

Everything is plain Markdown/JSON on disk under ``HELIX_HOME`` so it can be
grepped, version-controlled, and deleted by the user — the OpenClaw philosophy,
kept intact. Two kinds of memory:

  * conversation/session notes (``memory/*.md``)
  * auto-generated skills and lessons (``skills/*.md``)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MemoryEntry:
    id: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class MemoryStore:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.memory_dir = self.home / "memory"
        self.skills_dir = self.home / "skills"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    # ---- notes -------------------------------------------------------------
    def add(self, title: str, body: str, tags: list[str] | None = None) -> MemoryEntry:
        entry = MemoryEntry(
            id=f"{int(time.time() * 1000)}-{abs(hash(title)) % 10000}",
            title=title,
            body=body,
            tags=tags or [],
        )
        path = self.memory_dir / f"{entry.id}.md"
        front = f"---\ntitle: {title}\ntags: {', '.join(entry.tags)}\ncreated: {entry.created_at}\n---\n\n"
        path.write_text(front + body)
        return entry

    def search(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        q = query.lower()
        results: list[MemoryEntry] = []
        for path in sorted(self.memory_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            text = path.read_text(errors="replace")
            if q in text.lower():
                results.append(MemoryEntry(id=path.stem, title=path.stem, body=text))
            if len(results) >= limit:
                break
        return results

    def list(self, limit: int = 50) -> list[dict]:
        out = []
        for path in sorted(self.memory_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            out.append({"id": path.stem, "title": path.stem, "path": str(path)})
        return out

    # ---- skills / lessons --------------------------------------------------
    def record_lesson(self, name: str, lesson: str) -> Path:
        """Persist a reusable lesson learned while solving a problem."""
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.lower())[:60]
        path = self.skills_dir / f"{slug}.md"
        if path.exists():
            existing = path.read_text()
            path.write_text(existing.rstrip() + f"\n\n- {lesson.strip()}\n")
        else:
            path.write_text(f"# {name}\n\n- {lesson.strip()}\n")
        return path

    def skills(self) -> list[dict]:
        return [{"name": p.stem, "path": str(p), "body": p.read_text(errors="replace")} for p in self.skills_dir.glob("*.md")]
