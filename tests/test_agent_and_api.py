"""Tests for the agent loop, tools, memory, and the HTTP API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helix.agent.loop import AgentLoop
from helix.config import HelixConfig, ModelConfig
from helix.diff_engine import make_engine
from helix.llm import MockLLM
from helix.memory import MemoryStore
from helix.server.app import HelixService, create_app
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


def test_agent_loop_runs_shell_tool(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    loop = AgentLoop(MockLLM(), registry)
    run = asyncio.run(loop.run("run: echo helix-works"))
    assert run.error is None
    assert any(c.name == "shell" for c in run.tool_calls)
    assert "helix-works" in run.final


def test_agent_loop_write_file_creates_diff_not_write(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    loop = AgentLoop(MockLLM(), registry)
    run = asyncio.run(loop.run("write file newfile.txt with content: hello"))
    assert run.diff_ids, "expected a proposed diff"
    assert not (ws / "newfile.txt").exists(), "file must not be written before approval"


def test_write_file_tool_applies_when_approval_disabled(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    ctx.require_diff_approval = False
    loop = AgentLoop(MockLLM(), registry)
    asyncio.run(loop.run("write file auto.txt with content: direct"))
    assert (ws / "auto.txt").read_text() == "direct"


def test_read_and_list_tools(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    (ws / "z.txt").write_text("alpha\nbeta\n")
    read = asyncio.run(registry.get("read_file").run(path="z.txt"))
    assert read.ok and "beta" in read.output
    listed = asyncio.run(registry.get("list_files").run())
    assert "z.txt" in listed.output


def test_search_tool(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    (ws / "s.py").write_text("def needle():\n    pass\n")
    res = asyncio.run(registry.get("search").run(pattern="needle", glob="*.py"))
    assert res.ok and "needle" in res.output


def test_shell_refuses_dangerous_command(env) -> None:
    import asyncio

    cfg, ctx, registry, ws = env
    res = asyncio.run(registry.get("shell").run(command="rm -rf /"))
    assert not res.ok and "refused" in res.output


def test_memory_store_roundtrip(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.add("Note one", "remember the milk", tags=["shopping"])
    found = store.search("milk")
    assert found
    store.record_lesson("Deploy steps", "always run migrations first")
    store.record_lesson("Deploy steps", "smoke test after deploy")
    skills = store.skills()
    assert skills and "migrations" in skills[0]["body"] and "smoke test" in skills[0]["body"]


def test_api_health_and_config(tmp_path: Path) -> None:
    cfg = HelixConfig(home=tmp_path / "h", workspace=tmp_path / "w", models=[ModelConfig()])
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)
    client = TestClient(app)
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert client.get("/api/config").status_code == 200


def test_api_chat_creates_diff_and_can_apply(tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir(parents=True)
    cfg = HelixConfig(home=tmp_path / "h", workspace=ws, models=[ModelConfig()])
    client = TestClient(create_app(cfg))
    res = client.post("/api/chat", json={"message": "write file api.txt with content: from-api", "force_mock": True}).json()
    assert res["diff_ids"], res
    diff_id = res["diff_ids"][0]
    assert not (ws / "api.txt").exists()
    applied = client.post(f"/api/diffs/{diff_id}/apply", json={})
    assert applied.status_code == 200
    assert (ws / "api.txt").read_text() == "from-api"


def test_api_problem_solve_and_verify(tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir(parents=True)
    (ws / "artifact.txt").write_text("ok\n")
    cfg = HelixConfig(home=tmp_path / "h", workspace=ws, models=[ModelConfig()])
    client = TestClient(create_app(cfg))
    res = client.post(
        "/api/problems",
        json={
            "statement": "confirm the artifact exists",
            "criteria": [{"description": "artifact present", "kind": "file_exists", "value": "artifact.txt"}],
            "force_mock": True,
        },
    ).json()
    assert res["solved"] is True
    assert res["plan"]["steps"]
    assert client.get("/api/problems").json()["problems"]


def test_api_diff_rollback(tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir(parents=True)
    cfg = HelixConfig(home=tmp_path / "h", workspace=ws, models=[ModelConfig()])
    client = TestClient(create_app(cfg))
    res = client.post("/api/chat", json={"message": "write file r.txt with content: v1", "force_mock": True}).json()
    diff_id = res["diff_ids"][0]
    client.post(f"/api/diffs/{diff_id}/apply", json={})
    assert (ws / "r.txt").exists()
    client.post(f"/api/diffs/{diff_id}/rollback")
    assert not (ws / "r.txt").exists()


def test_api_websocket_chat(tmp_path: Path) -> None:
    ws = tmp_path / "w"
    ws.mkdir(parents=True)
    cfg = HelixConfig(home=tmp_path / "h", workspace=ws, models=[ModelConfig()])
    client = TestClient(create_app(cfg))
    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_json({"message": "run: echo ws-ok", "force_mock": True})
        seen_final = False
        for _ in range(20):
            msg = websocket.receive_json()
            if msg.get("type") == "final":
                seen_final = True
                break
        assert seen_final
