"""FastAPI application: REST + WebSocket API for the Helix agent.

Endpoints
---------
GET  /api/health                     -> liveness + config summary
GET  /api/config                     -> active model/workspace config
POST /api/chat                       -> run one agent turn (JSON result)
WS   /ws/chat                        -> stream agent events
GET  /api/diffs                      -> list diff sets
GET  /api/diffs/{id}                 -> one diff set (with contents)
GET  /api/diffs/{id}/preview         -> dry-run preview
POST /api/diffs/{id}/apply           -> apply (optionally per-hunk)
POST /api/diffs/{id}/rollback        -> roll back
POST /api/problems                   -> register + solve a problem
GET  /api/problems                   -> list solved problems
GET  /api/memory                     -> list memory notes
POST /api/memory                     -> add a memory note
GET  /api/skills                     -> list auto-generated skills
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent.loop import AgentEvent, AgentLoop
from ..config import CONFIG, HelixConfig
from ..diff_engine import DiffEngine, DiffError, make_engine
from ..llm import build_client
from ..memory import MemoryStore
from ..solver.engine import Solver
from ..tools import ToolContext, build_registry
from ..tools.base import Registry
from ..llm import LLMClient

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ChatRequest(BaseModel):
    message: str
    force_mock: bool = False


class CriterionIn(BaseModel):
    description: str
    kind: str = "shell"
    value: str
    pattern: str | None = None
    path: str | None = None


class ProblemRequest(BaseModel):
    statement: str
    criteria: list[CriterionIn] = []
    constraints: list[str] = []
    context: str = ""
    force_mock: bool = False


class MemoryRequest(BaseModel):
    title: str
    body: str
    tags: list[str] = []


class ApplyRequest(BaseModel):
    accepted_hunk_ids: dict[str, list[str]] | None = None
    force: bool = False


class HelixService:
    """Holds shared, long-lived objects (diff engine, memory, tools)."""

    def __init__(self, config: HelixConfig) -> None:
        self.config = config
        self.diff_engine: DiffEngine = make_engine(config.workspace, config.home)
        self.memory = MemoryStore(config.home)
        self.ctx = ToolContext(
            workspace=config.workspace,
            diff_engine=self.diff_engine,
            home=config.home,
            shell_timeout=config.shell_timeout,
            require_diff_approval=config.require_diff_approval,
        )
        self.registry: Registry = build_registry(self.ctx)
        self.problems: dict[str, dict] = {}

    def llm(self, force_mock: bool = False) -> LLMClient:
        return build_client(self.config.models[0], force_mock=force_mock)


def create_app(config: HelixConfig | None = None) -> FastAPI:
    cfg = config or CONFIG
    service = HelixService(cfg)
    app = FastAPI(title="Helix", version="0.1.0")
    app.state.service = service

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>Helix</h1><p>Web UI not found.</p>")
        return HTMLResponse(index_file.read_text())

    @app.get("/api/health")
    async def health() -> dict:
        m = cfg.models[0]
        return {
            "status": "ok",
            "workspace": str(cfg.workspace),
            "home": str(cfg.home),
            "model": {"name": m.name, "model": m.model, "base_url": m.base_url},
            "require_diff_approval": cfg.require_diff_approval,
        }

    @app.get("/api/config")
    async def get_config() -> dict:
        return cfg.model_dump(mode="json")

    # ---- chat --------------------------------------------------------------
    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> dict:
        loop = AgentLoop(service.llm(req.force_mock), service.registry)
        run = await loop.run(req.message)
        return {
            "final": run.final,
            "error": run.error,
            "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in run.tool_calls],
            "diff_ids": run.diff_ids,
        }

    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket) -> None:
        await ws.accept()
        try:
            raw = await ws.receive_text()
            payload = json.loads(raw)
            message = payload.get("message", "")
            force_mock = bool(payload.get("force_mock", False))

            def forward(ev: AgentEvent) -> None:
                # Fire-and-forget send; the loop awaits between steps.
                try:
                    import asyncio

                    asyncio.get_event_loop().create_task(ws.send_json(ev.to_dict()))
                except RuntimeError:
                    pass

            loop = AgentLoop(service.llm(force_mock), service.registry, on_event=forward)
            run = await loop.run(message)
            await ws.send_json({"type": "final", "final": run.final, "diff_ids": run.diff_ids, "error": run.error})
        except WebSocketDisconnect:
            return
        except (json.JSONDecodeError, KeyError) as e:
            await ws.send_json({"type": "error", "message": f"bad request: {e}"})
        finally:
            try:
                await ws.close()
            except RuntimeError:
                pass

    # ---- diffs -------------------------------------------------------------
    @app.get("/api/diffs")
    async def list_diffs() -> dict:
        return {"diffs": [d.to_dict(include_content=False) for d in service.diff_engine.list_sets()]}

    @app.get("/api/diffs/{diff_id}")
    async def get_diff(diff_id: str) -> dict:
        try:
            return service.diff_engine.get(diff_id).to_dict(include_content=True)
        except DiffError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/diffs/{diff_id}/preview")
    async def preview_diff(diff_id: str) -> dict:
        try:
            return service.diff_engine.preview(diff_id)
        except DiffError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/api/diffs/{diff_id}/apply")
    async def apply_diff(diff_id: str, req: ApplyRequest) -> dict:
        try:
            ds = service.diff_engine.apply(diff_id, req.accepted_hunk_ids, force=req.force)
            return ds.to_dict(include_content=False)
        except DiffError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.post("/api/diffs/{diff_id}/rollback")
    async def rollback_diff(diff_id: str) -> dict:
        try:
            ds = service.diff_engine.rollback(diff_id)
            return ds.to_dict(include_content=False)
        except DiffError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ---- problems ----------------------------------------------------------
    @app.post("/api/problems")
    async def solve(req: ProblemRequest) -> dict:
        solver = Solver(service.llm(req.force_mock), service.registry, service.ctx, cfg)
        problem = solver.register_problem(
            req.statement,
            [c.model_dump() for c in req.criteria],
            req.constraints,
            req.context,
        )
        result = await solver.solve(problem)
        payload = result.to_dict()
        service.problems[problem.id] = payload
        service.memory.add(
            title=f"Problem: {req.statement[:60]}",
            body=json.dumps(payload, indent=2)[:20000],
            tags=["problem", problem.status],
        )
        return payload

    @app.get("/api/problems")
    async def list_problems() -> dict:
        return {"problems": list(service.problems.values())}

    # ---- memory ------------------------------------------------------------
    @app.get("/api/memory")
    async def list_memory() -> dict:
        return {"memory": service.memory.list()}

    @app.post("/api/memory")
    async def add_memory(req: MemoryRequest) -> dict:
        entry = service.memory.add(req.title, req.body, req.tags)
        return {"id": entry.id, "title": entry.title}

    @app.get("/api/skills")
    async def list_skills() -> dict:
        return {"skills": service.memory.skills()}

    return app


app = create_app()
