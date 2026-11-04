"""Data model for structured problem solving.

A :class:`Problem` is decomposed into :class:`Step` objects that form a dependency
DAG. Steps carry hypotheses and evidence, and the whole solve is judged against
explicit acceptance criteria.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"  # dependencies satisfied
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class CriterionKind(str, Enum):
    SHELL = "shell"  # run a command, pass if exit code 0
    PYTHON = "python"  # run a snippet, pass if exit code 0
    FILE_EXISTS = "file_exists"
    CONTAINS = "contains"  # file contains a regex


@dataclass
class Criterion:
    """A single, checkable acceptance condition."""

    description: str
    kind: CriterionKind
    value: str  # command / code / path
    pattern: str | None = None  # for CONTAINS
    path: str | None = None  # for CONTAINS
    satisfied: bool | None = None
    evidence: str = ""

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "kind": self.kind.value,
            "value": self.value,
            "pattern": self.pattern,
            "path": self.path,
            "satisfied": self.satisfied,
            "evidence": self.evidence,
        }


@dataclass
class Step:
    """A unit of work in the plan."""

    id: str
    title: str
    action: str  # natural-language instruction for the agent loop
    depends_on: list[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    kind: Literal["investigate", "implement", "verify", "decide"] = "implement"
    hypothesis: str = ""
    evidence: str = ""
    result: str = ""
    diff_ids: list[str] = field(default_factory=list)
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "action": self.action,
            "depends_on": self.depends_on,
            "status": self.status.value,
            "kind": self.kind,
            "hypothesis": self.hypothesis,
            "evidence": self.evidence,
            "result": self.result,
            "diff_ids": self.diff_ids,
            "attempts": self.attempts,
        }


@dataclass
class Problem:
    """A problem statement plus how we will know it is solved."""

    id: str
    statement: str
    criteria: list[Criterion] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    context: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "open"  # open | solving | solved | failed | needs_input

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "statement": self.statement,
            "criteria": [c.to_dict() for c in self.criteria],
            "constraints": self.constraints,
            "context": self.context,
            "created_at": self.created_at,
            "status": self.status,
        }


@dataclass
class SolvePlan:
    """The DAG of steps for a problem, plus its live execution state."""

    id: str
    problem_id: str
    steps: list[Step] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    status: str = "planned"  # planned | running | solved | failed
    created_at: float = field(default_factory=time.time)

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def ready_steps(self) -> list[Step]:
        done = {s.id for s in self.steps if s.status in (StepStatus.DONE, StepStatus.SKIPPED)}
        ready = []
        for s in self.steps:
            if s.status not in (StepStatus.PENDING, StepStatus.READY):
                continue
            if all(d in done for d in s.depends_on):
                ready.append(s)
        return ready

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "problem_id": self.problem_id,
            "status": self.status,
            "created_at": self.created_at,
            "steps": [s.to_dict() for s in self.steps],
            "log": self.log,
        }


def new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex[:10]) if prefix else uuid.uuid4().hex[:12]
