"""Tests for the structured problem-solving engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from helix.config import HelixConfig, ModelConfig
from helix.diff_engine import make_engine
from helix.llm import MockLLM
from helix.solver.engine import Solver
from helix.solver.models import Criterion, CriterionKind, Problem, Step, StepStatus, SolvePlan
from helix.solver.planner import _validate_graph, replan_after_failure
from helix.solver.verifier import Verifier
from helix.tools import ToolContext, build_registry


@pytest.fixture()
def env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = HelixConfig(home=home, workspace=ws, models=[ModelConfig()])
    engine = make_engine(ws, home)
    ctx = ToolContext(workspace=ws, diff_engine=engine, home=home, require_diff_approval=True)
    registry = build_registry(ctx)
    return cfg, ctx, registry, ws


def test_verifier_shell_criterion(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    v = Verifier(ctx)
    ok = asyncio.run(v.check(Criterion(description="true", kind=CriterionKind.SHELL, value="true")))
    assert ok.satisfied
    bad = asyncio.run(v.check(Criterion(description="false", kind=CriterionKind.SHELL, value="false")))
    assert not bad.satisfied
    assert "false" in bad.evidence


def test_verifier_file_and_contains(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    (ws / "out.txt").write_text("hello world\n")
    v = Verifier(ctx)
    exists = asyncio.run(v.check(Criterion(description="exists", kind=CriterionKind.FILE_EXISTS, value="out.txt")))
    assert exists.satisfied
    contains = asyncio.run(
        v.check(
            Criterion(
                description="contains",
                kind=CriterionKind.CONTAINS,
                value="world",
                path="out.txt",
                pattern="world",
            )
        )
    )
    assert contains.satisfied


def test_verifier_python_can_import_workspace_module(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    (ws / "greet.py").write_text('def hello():\n    return "hi"\n')
    v = Verifier(ctx)
    res = asyncio.run(
        v.check(
            Criterion(
                description="import works",
                kind=CriterionKind.PYTHON,
                value='import greet\nassert greet.hello() == "hi"\nprint("verified")',
            )
        )
    )
    assert res.satisfied, res.evidence
    assert "verified" in res.evidence


def test_verifier_python_criterion(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    v = Verifier(ctx)
    good = asyncio.run(
        v.check(Criterion(description="py ok", kind=CriterionKind.PYTHON, value="assert 1 + 1 == 2"))
    )
    assert good.satisfied
    bad = asyncio.run(v.check(Criterion(description="py bad", kind=CriterionKind.PYTHON, value="raise SystemExit(1)")))
    assert not bad.satisfied


def test_graph_validation_detects_cycle() -> None:
    steps = [
        Step(id="a", title="a", action="", depends_on=["b"]),
        Step(id="b", title="b", action="", depends_on=["a"]),
    ]
    with pytest.raises(ValueError, match="cycle"):
        _validate_graph(steps)


def test_graph_validation_detects_missing_dep() -> None:
    steps = [Step(id="a", title="a", action="", depends_on=["nope"])]
    with pytest.raises(ValueError, match="unknown step"):
        _validate_graph(steps)


def test_ready_steps_respects_dependencies() -> None:
    plan = SolvePlan(
        id="p",
        problem_id="x",
        steps=[
            Step(id="a", title="a", action=""),
            Step(id="b", title="b", action="", depends_on=["a"]),
        ],
    )
    assert [s.id for s in plan.ready_steps()] == ["a"]
    plan.steps[0].status = StepStatus.DONE
    assert [s.id for s in plan.ready_steps()] == ["b"]


def test_backtracking_inserts_diagnostic_step() -> None:
    problem = Problem(id="p1", statement="x")
    plan = SolvePlan(id="pl", problem_id="p1", steps=[Step(id="s1", title="do", action="act", depends_on=["s0"])])
    new_steps = replan_after_failure(problem, plan, plan.steps[0], "boom")
    assert len(new_steps) == 3
    assert new_steps[0].kind == "investigate"
    assert new_steps[1].depends_on == [new_steps[0].id]
    assert plan.steps[0].status == StepStatus.SKIPPED


def test_solver_uses_heuristic_plan_with_mock(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    solver = Solver(MockLLM(), registry, ctx, cfg)
    problem = solver.register_problem(
        "create a file that proves the solve works",
        criteria=[{"description": "file exists", "kind": "file_exists", "value": "proof.txt"}],
    )
    # Mock LLM cannot create files, so this should NOT be solved — proving we
    # never falsely report success.
    result = asyncio.run(solver.solve(problem))
    assert result.solved is False
    assert result.problem.status in ("failed", "needs_input")
    assert len(result.plan.steps) >= 4


def test_solver_reports_solved_when_criteria_met(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    # Pre-create the artifact the criterion checks, then solve with a criterion
    # that will genuinely pass.
    (ws / "proof.txt").write_text("done\n")
    solver = Solver(MockLLM(), registry, ctx, cfg)
    problem = solver.register_problem(
        "verify the artifact exists",
        criteria=[{"description": "proof exists", "kind": "file_exists", "value": "proof.txt"}],
    )
    result = asyncio.run(solver.solve(problem))
    assert result.solved is True
    assert result.problem.status == "solved"
