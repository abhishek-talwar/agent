# AGENTS.md — Helix repository guide

This file is persistent memory for agents working in this repo. Read it first.

## What this project is

Helix is a local-first + web AI agent (a Hermes Agent / OpenClaw style tool) whose
two headline differentiators are:

1. **Reviewable diffs** — every file mutation is proposed as a unified diff and only
   written after approval, with hunk-level accept/reject, dry-run preview, stale-diff
   detection, and rollback.
2. **Structured problem solving** — problems are decomposed into a dependency DAG,
   steps carry hypotheses and evidence, results are verified against explicit
   acceptance criteria, and failures trigger diagnostic backtracking.

If you change anything, preserve these two properties. They are the point of the project.

## Setup

```bash
pip install -e .            # runtime deps
pip install pytest pytest-asyncio   # test deps
```

Python 3.11+. Runtime deps: fastapi, uvicorn, httpx, pydantic, jinja2,
python-multipart, websockets, rich, typer.

## Commands

```bash
python -m pytest -q                 # run the suite (34 tests)
helix serve                         # web app on 127.0.0.1:8000
python -m helix serve --port 12000  # alternative entry point
helix chat "run: ls -la" --mock     # one agent turn, offline mock model
helix solve "statement" --check "cmd that must exit 0"
helix diffs | helix diff <id> | helix preview <id> | helix apply <id> | helix rollback <id>
```

Everything works with no model configured thanks to `MockLLM` (`--mock` /
`force_mock: true`). Use it in tests; never depend on a live model.

## Architecture map

```
helix/config.py        HelixConfig + env overrides (HELIX_BASE_URL/MODEL/API_KEY/WORKSPACE/HOME)
helix/llm.py           LLMClient protocol, OpenAICompatClient, MockLLM
helix/diff_engine.py   DiffEngine, FileDiff, Hunk, DiffSet — the reviewable-diff core
helix/agent/loop.py    AgentLoop (ReAct), AgentEvent stream, parse_plan_json
helix/tools/           base (ToolContext/Registry/tool_schema), shell, files, python_exec, web
helix/solver/models.py Problem/Criterion/Step/SolvePlan
helix/solver/planner.py  plan_problem (LLM plan + heuristic fallback), replan_after_failure
helix/solver/verifier.py Verifier (shell/python/file_exists/contains criteria)
helix/solver/engine.py   Solver orchestrator + backtracking loop
helix/memory/store.py  MemoryStore (Markdown notes + skills)
helix/server/app.py    FastAPI create_app + HelixService
helix/web/             index.html, styles.css, app.js (no build step, vanilla JS)
helix/cli.py           Typer CLI
```

## Invariants and conventions

- **Never write files directly in tools.** All mutations go through
  `DiffEngine.propose_*` so they become reviewable diffs. `WriteFileTool`/`EditFileTool`
  return a `diff_id`; `require_diff_approval` decides whether it also auto-applies.
- **Never claim success without verification.** The solver only reports `solved` when
  every `Criterion` passes; the verifier attaches raw evidence.
- **Path safety**: all workspace paths are resolved and checked against escape
  (`DiffEngine._resolve`, `files._safe`).
- **Tool schemas** are built by `tools.base.tool_schema`, not by a Protocol method —
  `Protocol` methods are not inherited by structurally-conforming classes.
- **Python subprocesses** (verifier + python_exec) set `PYTHONPATH` to the workspace so
  `import <workspace module>` works even though the snippet runs from a temp file.
- `ToolResult.meta["diff_id"]` is how the loop collects proposed diffs.
- Keep the web UI dependency-free (vanilla JS, no bundler).

## Testing notes

- `tests/test_diff_engine.py` — the diff engine (partial hunk accept, stale refusal,
  rollback, path escape, persistence).
- `tests/test_solver.py` — planner graph validation, backtracking, verifier criteria,
  and that the solver reports `solved=False` when criteria genuinely cannot pass.
- `tests/test_agent_and_api.py` — agent loop, tools, memory, and the REST/WS API
  via `fastapi.testclient.TestClient`.
- Fixtures create isolated `workspace`/`home` dirs under `tmp_path`; always pass an
  explicit `HelixConfig` rather than relying on the process-wide `CONFIG`.
