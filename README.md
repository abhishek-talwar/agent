# Helix

**A local-first *and* web AI agent with reviewable diffs and structured problem solving.**

Helix is a coding/automation agent in the spirit of Hermes Agent and OpenClaw — it
lives on your machine, talks over a web UI, CLI, or API, remembers things as plain
files, and takes real actions (shell, files, Python, web). It exists to fix the two
things those agents leave on the table:

| Gap in Hermes / OpenClaw | What Helix adds |
| --- | --- |
| File edits land with little or no structured review; no per-change accept/reject, no rollback of a specific change. | **Reviewable diffs.** Every mutation is proposed as a unified diff. Nothing is written until you approve it — and you can approve *individual hunks*, see a dry-run preview, detect stale/conflicting edits, and roll back any change. |
| Turn-by-turn reaction. Weak on decomposition, hypothesis testing, and honest verification — agents often *claim* success. | **Structured problem solving.** Helix decomposes a problem into a dependency DAG, records a hypothesis and evidence per step, verifies against explicit acceptance criteria, and backtracks with a diagnostic step instead of blindly retrying. It refuses to report "solved" until the criteria actually pass. |

## Why local-first

State lives in plain files under `~/.helix` (or `$HELIX_HOME`):

```
~/.helix/
  diffs/    # every diff set, as JSON, replayable and rollback-able
  memory/   # notes as Markdown you can grep and version
  skills/   # lessons the agent writes back as it solves problems
```

Models can be **fully local** (Ollama, LM Studio, llama.cpp, vLLM) or any
OpenAI-compatible endpoint. Nothing leaves your machine unless you point it at a
remote model.

## Install

```bash
pip install -e .          # from the repo root
```

Requires Python 3.11+.

## Run the web app

```bash
helix serve               # http://127.0.0.1:8000
```

The UI has four tabs:

- **Solve** — enter a problem, add acceptance criteria, watch the plan DAG and the
  verification evidence fill in.
- **Chat** — a streaming agent conversation with a live tool trace.
- **Diffs** — review every proposed change, toggle hunks on/off, apply or roll back.
- **Memory** — notes and auto-generated skills.

## Configure a model

Helix defaults to a local Ollama endpoint. Point it anywhere with env vars:

```bash
# Ollama (fully local)
export HELIX_BASE_URL=http://localhost:11434/v1
export HELIX_MODEL=qwen2.5-coder:7b

# OpenAI-compatible (OpenAI, OpenRouter, vLLM, LM Studio, ...)
export HELIX_BASE_URL=https://api.openai.com/v1
export HELIX_API_KEY=sk-...
export HELIX_MODEL=gpt-4o-mini
```

No model handy? Every entry point has a deterministic **offline mock** (`--mock`,
or `force_mock: true` in the API) so you can exercise the whole system with zero setup.

## CLI

```bash
helix chat "run: python -m pytest -q"     # one agent turn
helix solve "fix the 500 on /api/users" \
  --check "python -m pytest tests/test_users.py -q"

helix diffs                    # list proposed changes
helix diff <id>                # show a unified diff
helix preview <id>             # dry-run: conflicts + what would change
helix apply <id> --hunks a1,b2 # apply only these hunks
helix rollback <id>            # restore pre-change state

helix memory add "Deploys" "run migrations before restart"
helix skills                   # lessons the agent has written back
```

## HTTP / WebSocket API

```
GET  /api/health
POST /api/chat                     {"message": "...", "force_mock": false}
WS   /ws/chat                      stream agent events

GET  /api/diffs
GET  /api/diffs/{id}
GET  /api/diffs/{id}/preview       dry-run
POST /api/diffs/{id}/apply         {"accepted_hunk_ids": {"path": ["hunk"]}, "force": false}
POST /api/diffs/{id}/rollback

POST /api/problems                 {"statement": "...", "criteria": [...], "constraints": [...]}
GET  /api/problems
GET  /api/memory      POST /api/memory
GET  /api/skills
```

### Example: a verified solve

```bash
curl -s -X POST localhost:8000/api/problems -H 'Content-Type: application/json' -d '{
  "statement": "Ensure greet.py defines hello() returning a greeting",
  "criteria": [
    {"description": "greet.py exists", "kind": "file_exists", "value": "greet.py"},
    {"description": "hello() returns hi", "kind": "python",
     "value": "import greet\nassert greet.hello() == \"hi\"\nprint(\"verified\")"}
  ]
}'
```

`solved` is `true` only when every criterion passes with real evidence attached.

## How the two differentiators work

### 1. Reviewable diffs (`helix/diff_engine.py`)

- `build_hunks(old, new)` turns two file versions into unified-diff hunks via stdlib
  `difflib`.
- Mutating tools (`write_file`, `edit_file`) **never touch disk** — they call
  `propose_file` / `propose_edit` and return a diff id.
- `apply(id, accepted_hunk_ids)` applies only the hunks you accept. Rejected hunks
  leave the file exactly as it was.
- `check_conflicts(id)` refuses to apply if the file changed on disk since the diff
  was created (unless `force=true`), so it can't silently clobber your work.
- `rollback(id)` restores pre-change contents (and deletes files that the diff created).
- Diff sets persist under `~/.helix/diffs/`, so review and rollback survive restarts.

### 2. Structured problem solving (`helix/solver/`)

- **Planner** (`planner.py`) asks the model for a step DAG, validates it (no cycles,
  valid deps), and falls back to a heuristic investigate→decide→implement→verify plan.
- **Engine** (`engine.py`) executes ready steps in dependency order, each through the
  agent loop, recording hypotheses and evidence.
- **Backtracking**: a failed step spawns a diagnostic investigation step, a retry, and
  a re-verify — instead of a blind retry.
- **Verifier** (`verifier.py`) executes each acceptance criterion (shell / python /
  file_exists / contains) and attaches the raw evidence. `solved` is a computed fact,
  not a claim.

## Layout

```
helix/
  config.py        configuration + env loading
  llm.py           OpenAI-compatible client + offline mock
  diff_engine.py   the reviewable-diff engine
  agent/loop.py    ReAct-style tool-calling loop (streams events)
  tools/           shell, files (diff-routed), search, python, web
  solver/          problem model, planner, verifier, orchestrator
  memory/store.py  local Markdown memory + skills
  server/app.py    FastAPI REST + WebSocket
  web/             the browser UI
  cli.py           Typer CLI
tests/             34 tests covering the diff engine, solver, tools, and API
```

## Tests

```bash
python -m pytest -q
```

## Safety notes

- Shell commands are sandboxed to the workspace with a timeout, and a small denylist
  blocks catastrophic commands.
- Path escapes from the workspace are rejected.
- Writes are gated behind diff approval by default
  (`require_diff_approval`; disable with care).
- The web UI binds to `127.0.0.1` by default. Do not expose it publicly without adding
  authentication — it can run shell commands.

## License

MIT
