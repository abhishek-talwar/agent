"""The problem-solving orchestrator.

Flow:
  1. Register the problem with explicit acceptance criteria.
  2. Plan a dependency DAG of steps.
  3. Execute ready steps in dependency order, each via the agent loop.
  4. Verify every acceptance criterion with real evidence.
  5. On failure, backtrack: insert a diagnostic step, retry, and re-verify.

Unlike Hermes/OpenClaw, which primarily react turn-by-turn, Helix keeps an
explicit plan, records hypotheses and evidence per step, and refuses to declare
success until the acceptance criteria actually pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from ..agent.loop import AgentEvent, AgentLoop
from ..config import HelixConfig
from ..llm import LLMClient
from ..tools.base import Registry, ToolContext
from .models import (
    Criterion,
    CriterionKind,
    Problem,
    SolvePlan,
    Step,
    StepStatus,
    new_id,
)
from .planner import plan_problem, replan_after_failure
from .verifier import Verifier


@dataclass
class SolveResult:
    problem: Problem
    plan: SolvePlan
    solved: bool
    iterations: int
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "problem": self.problem.to_dict(),
            "plan": self.plan.to_dict(),
            "solved": self.solved,
            "iterations": self.iterations,
            "events": self.events,
        }


class Solver:
    def __init__(
        self,
        llm: LLMClient,
        registry: Registry,
        ctx: ToolContext,
        config: HelixConfig,
        *,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.ctx = ctx
        self.config = config
        self.verifier = Verifier(ctx)
        self.on_event = on_event

    # ---- problem registration ---------------------------------------------
    def register_problem(
        self,
        statement: str,
        criteria: list[dict] | None = None,
        constraints: list[str] | None = None,
        context: str = "",
    ) -> Problem:
        crits: list[Criterion] = []
        for c in criteria or []:
            crits.append(
                Criterion(
                    description=c.get("description", c.get("value", "")),
                    kind=CriterionKind(c.get("kind", "shell")),
                    value=c.get("value", ""),
                    pattern=c.get("pattern"),
                    path=c.get("path"),
                )
            )
        return Problem(
            id=new_id("prob-"),
            statement=statement,
            criteria=crits,
            constraints=constraints or [],
            context=context,
            status="open",
        )

    # ---- main solve --------------------------------------------------------
    async def solve(self, problem: Problem) -> SolveResult:
        problem.status = "solving"
        plan = await plan_problem(self.llm, problem)
        plan.status = "running"
        events: list[dict] = []
        self._log(plan, "plan_created", {"steps": [s.to_dict() for s in plan.steps]})

        iterations = 0
        failure_budget = 3
        failures = 0

        while iterations < self.config.max_solver_steps:
            iterations += 1
            ready = plan.ready_steps()
            if not ready:
                break
            # Execute ready steps sequentially for determinism; independent steps
            # could be parallelized, but ordering keeps evidence legible.
            for step in ready:
                if step.status in (StepStatus.DONE, StepStatus.SKIPPED):
                    continue
                ok = await self._run_step(step, plan, problem)
                if not ok:
                    failures += 1
                    if failures <= failure_budget:
                        new_steps = replan_after_failure(problem, plan, step, step.result)
                        plan.steps.extend(new_steps)
                        self._log(
                            plan,
                            "backtrack",
                            {"failed_step": step.id, "error": step.result, "added": [s.id for s in new_steps]},
                        )
                    else:
                        plan.status = "failed"
                        problem.status = "failed"
                        self._log(plan, "failure_budget_exhausted", {"step": step.id})
                        return SolveResult(problem, plan, False, iterations, events)
                else:
                    failures = 0

        # Verify against the acceptance criteria.
        solved, results = await self.verifier.check_all(problem)
        plan.status = "solved" if solved else "failed"
        problem.status = "solved" if solved else "needs_input"
        self._log(plan, "verification", {"solved": solved, "criteria": [c.to_dict() for c in results]})
        return SolveResult(problem, plan, solved, iterations, events)

    async def _run_step(self, step: Step, plan: SolvePlan, problem: Problem) -> bool:
        step.status = StepStatus.RUNNING
        step.attempts += 1
        self._log(plan, "step_start", {"id": step.id, "title": step.title, "kind": step.kind})

        task = step.action
        if step.hypothesis:
            task = f"Hypothesis to test: {step.hypothesis}\n\n{task}"
        task += (
            f"\n\nContext — overall problem: {problem.statement}\n"
            f"Acceptance criteria:\n"
            + "\n".join(f"- {c.description}" for c in problem.criteria)
        )

        def forward(ev: AgentEvent) -> None:
            self._log(plan, "agent_event", {"step": step.id, **ev.to_dict()})
            if self.on_event:
                self.on_event(ev)

        loop = AgentLoop(self.llm, self.registry, max_iterations=10, on_event=forward)
        run = await loop.run(task)

        step.result = run.final or ("(no output)" if not run.error else run.error)
        step.evidence = "\n".join(
            e.get("output", "") for e in plan.log if e.get("type") == "agent_event"
            and e.get("step") == step.id and e.get("type") == "tool_result"
        )[:4000]
        step.diff_ids.extend(run.diff_ids)

        if run.error:
            step.status = StepStatus.FAILED
            step.result = run.error
            self._log(plan, "step_failed", {"id": step.id, "error": run.error})
            return False

        step.status = StepStatus.DONE
        self._log(plan, "step_done", {"id": step.id, "result": step.result[:1000]})
        return True

    def _log(self, plan: SolvePlan, event_type: str, data: dict) -> None:
        plan.log.append({"type": event_type, **data})


async def solve_problem(
    statement: str,
    criteria: list[dict] | None,
    llm: LLMClient,
    registry: Registry,
    ctx: ToolContext,
    config: HelixConfig,
) -> SolveResult:
    """Convenience one-shot entry point."""
    solver = Solver(llm, registry, ctx, config)
    problem = solver.register_problem(statement, criteria)
    return await solver.solve(problem)
