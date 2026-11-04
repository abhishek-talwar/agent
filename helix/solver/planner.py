"""Decompose a problem into a dependency DAG of steps.

The planner asks the model for a JSON plan, validates the graph (no cycles, valid
dependency ids), and falls back to a sensible heuristic plan if the model output
is unusable. The heuristic path also keeps the whole system runnable offline.
"""

from __future__ import annotations

from ..agent.loop import parse_plan_json
from ..llm import LLMClient, Message
from .models import Problem, SolvePlan, Step, StepStatus, new_id

PLANNER_PROMPT = """You are Helix's planner. Break the problem into a small DAG of concrete steps.

Return ONLY JSON of this shape:
{
  "steps": [
    {
      "id": "s1",
      "title": "short imperative title",
      "kind": "investigate|implement|verify|decide",
      "hypothesis": "what you expect and why (optional, for investigate/decide)",
      "action": "precise instruction for the agent that will execute this step",
      "depends_on": ["s0"]
    }
  ]
}

Rules:
- 3 to 8 steps. Always include at least one "investigate" step first and a final "verify" step.
- depends_on must reference ids defined in this same plan and must not form a cycle.
- Actions must be concrete (name files, commands, or checks), not vague.
- Do not include steps that write files unless necessary; prefer evidence first.
"""


def _validate_graph(steps: list[Step]) -> None:
    ids = {s.id for s in steps}
    for s in steps:
        for dep in s.depends_on:
            if dep not in ids:
                raise ValueError(f"step {s.id} depends on unknown step {dep}")
    # Cycle detection (DFS).
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {s.id: s for s in steps}

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError(f"cycle detected at {node}")
        visiting.add(node)
        for dep in by_id[node].depends_on:
            visit(dep)
        visiting.discard(node)
        visited.add(node)

    for s in steps:
        visit(s.id)


def _heuristic_plan(problem: Problem) -> list[Step]:
    """A dependable offline plan: investigate -> implement -> verify."""
    steps = [
        Step(
            id="s1",
            title="Investigate the current state",
            kind="investigate",
            hypothesis="Understanding the existing code and reproducing the issue is required before changing it.",
            action=(
                f"Explore the workspace and gather evidence about this problem: {problem.statement}. "
                "List files, read the most relevant ones, and run any command that reproduces the issue."
            ),
        ),
        Step(
            id="s2",
            title="Form a hypothesis and plan the fix",
            kind="decide",
            hypothesis="A concrete, testable fix can be identified from the evidence.",
            action=(
                "Based on the investigation, state the root cause and the minimal change that fixes it. "
                "Record the evidence supporting the root cause."
            ),
            depends_on=["s1"],
        ),
        Step(
            id="s3",
            title="Implement the fix as a reviewable diff",
            kind="implement",
            action=(
                "Make the minimal change that addresses the root cause. Propose it with write_file "
                "or edit_file so it is captured as a reviewable diff."
            ),
            depends_on=["s2"],
        ),
        Step(
            id="s4",
            title="Verify against the acceptance criteria",
            kind="verify",
            action=(
                "Run the commands/tests that prove the problem is solved and report the exact output."
            ),
            depends_on=["s3"],
        ),
    ]
    return steps


async def plan_problem(llm: LLMClient, problem: Problem) -> SolvePlan:
    criteria_text = "\n".join(f"- [{c.kind.value}] {c.description}" for c in problem.criteria) or "(none given)"
    prompt = (
        f"Problem:\n{problem.statement}\n\n"
        f"Acceptance criteria:\n{criteria_text}\n\n"
        f"Constraints:\n" + ("\n".join(f"- {c}" for c in problem.constraints) or "(none)") + "\n\n"
        f"Extra context:\n{problem.context or '(none)'}"
    )
    steps: list[Step] = []
    try:
        resp = await llm.complete(
            [Message(role="system", content=PLANNER_PROMPT), Message(role="user", content=prompt)],
            temperature=0.1,
        )
        data = parse_plan_json(resp.content)
        if data and isinstance(data.get("steps"), list):
            for raw in data["steps"]:
                steps.append(
                    Step(
                        id=str(raw.get("id") or new_id("s")),
                        title=str(raw.get("title", "step")),
                        action=str(raw.get("action", "")),
                        kind=raw.get("kind", "implement"),
                        hypothesis=str(raw.get("hypothesis", "")),
                        depends_on=[str(d) for d in raw.get("depends_on", [])],
                    )
                )
            _validate_graph(steps)
    except (ValueError, KeyError, TypeError):
        steps = []

    if not steps:
        steps = _heuristic_plan(problem)

    return SolvePlan(id=new_id("plan-"), problem_id=problem.id, steps=steps)


def replan_after_failure(problem: Problem, plan: SolvePlan, failed: Step, error: str) -> list[Step]:
    """Insert a diagnostic step that depends on the failed step's deps.

    This is Helix's backtracking: instead of blindly retrying, it adds an explicit
    investigation step whose job is to explain the failure, then re-runs verify.
    """
    diag = Step(
        id=new_id("s-"),
        title=f"Diagnose failure in '{failed.title}'",
        kind="investigate",
        hypothesis=f"The failure is: {error}. Determine why before retrying.",
        action=(
            f"The step '{failed.title}' failed with: {error}. Gather evidence on why, "
            "state the corrected approach, and report what needs to change."
        ),
        depends_on=list(failed.depends_on),
    )
    retry = Step(
        id=new_id("s-"),
        title=f"Retry: {failed.title}",
        kind=failed.kind,
        hypothesis=failed.hypothesis,
        action=failed.action,
        depends_on=[diag.id],
    )
    verify = Step(
        id=new_id("s-"),
        title="Re-verify against the acceptance criteria",
        kind="verify",
        action="Run the acceptance checks again and report exact output.",
        depends_on=[retry.id],
    )
    # Reset the failed step so the new diagnostic path can run.
    failed.status = StepStatus.SKIPPED
    failed.result = f"superseded after failure: {error}"
    return [diag, retry, verify]
