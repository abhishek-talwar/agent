"""Helix command-line interface.

    helix serve                       # start the local web app
    helix chat "run: ls -la"          # one-shot agent turn
    helix solve "statement" --check "python -m pytest -q"
    helix diffs                       # list diff sets
    helix diff <id>                   # show a diff
    helix apply <id> [--hunks a,b]    # apply a diff (optionally per-hunk)
    helix rollback <id>               # roll back a diff
    helix memory add "title" "body"
    helix skills
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .agent.loop import AgentLoop
from .config import CONFIG
from .diff_engine import DiffError, make_engine
from .llm import build_client
from .memory import MemoryStore
from .solver.engine import Solver
from .tools import ToolContext, build_registry

app = typer.Typer(add_completion=False, help="Helix — local-first AI agent with reviewable diffs and structured problem solving.")
console = Console()


def _service(mock: bool = False):
    cfg = CONFIG
    engine = make_engine(cfg.workspace, cfg.home)
    ctx = ToolContext(
        workspace=cfg.workspace,
        diff_engine=engine,
        home=cfg.home,
        shell_timeout=cfg.shell_timeout,
        require_diff_approval=cfg.require_diff_approval,
    )
    registry = build_registry(ctx)
    llm = build_client(cfg.models[0], force_mock=mock)
    return cfg, engine, ctx, registry, llm


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Start the local web app (REST + WebSocket + UI)."""
    import uvicorn

    console.print(Panel.fit(f"Helix web UI → http://{host}:{port}", style="bold cyan"))
    uvicorn.run("helix.server.app:app", host=host, port=port, reload=reload)


@app.command()
def chat(message: str, mock: bool = typer.Option(False, "--mock")) -> None:
    """Run a single agent turn."""
    _, engine, ctx, registry, llm = _service(mock)
    loop = AgentLoop(llm, registry, on_event=lambda e: console.print(f"[dim]{e.type}[/dim] {e.data}"))
    run = asyncio.run(loop.run(message))
    if run.error:
        console.print(f"[red]{run.error}[/red]")
    console.print(Panel(run.final or "(no output)", title="Helix", border_style="cyan"))
    if run.diff_ids:
        console.print(f"[yellow]proposed diffs:[/yellow] {', '.join(run.diff_ids)}")
        console.print("review with: [bold]helix diffs[/bold]")


@app.command()
def solve(
    statement: str,
    check: list[str] = typer.Option([], "--check", help="Acceptance criterion: a shell command that must exit 0."),
    mock: bool = typer.Option(False, "--mock"),
) -> None:
    """Solve a problem with structured planning and verification."""
    cfg, engine, ctx, registry, llm = _service(mock)
    solver = Solver(llm, registry, ctx, cfg, on_event=lambda e: None)
    problem = solver.register_problem(
        statement,
        [{"description": c, "kind": "shell", "value": c} for c in check],
    )
    result = asyncio.run(solver.solve(problem))
    table = Table(title="Plan", show_lines=True)
    table.add_column("step")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("title")
    for s in result.plan.steps:
        color = {"done": "green", "failed": "red", "running": "cyan"}.get(s.status.value, "white")
        table.add_row(s.id, s.kind, f"[{color}]{s.status.value}[/{color}]", s.title)
    console.print(table)
    for c in problem.criteria:
        mark = "[green]✔[/green]" if c.satisfied else "[red]✘[/red]"
        console.print(f"{mark} {c.description}")
    console.print(Panel(f"Solved: [bold]{result.solved}[/bold] · iterations: {result.iterations}", border_style="green" if result.solved else "red"))


@app.command()
def diffs() -> None:
    """List diff sets."""
    _, engine, *_ = _service()
    table = Table(title="Diffs")
    table.add_column("id")
    table.add_column("title")
    table.add_column("+")
    table.add_column("-")
    table.add_column("status")
    for d in engine.list_sets():
        table.add_row(d.id, d.title, str(d.total_added), str(d.total_removed), d.status)
    console.print(table)


@app.command()
def diff(diff_id: str) -> None:
    """Show a diff as a unified diff."""
    _, engine, *_ = _service()
    try:
        ds = engine.get(diff_id)
    except DiffError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(Syntax(ds.unified(), "diff", theme="monokai", line_numbers=False))


@app.command()
def apply(
    diff_id: str,
    hunks: Optional[str] = typer.Option(None, help="Comma-separated hunk ids to accept (default: all)."),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Apply a diff set."""
    _, engine, *_ = _service()
    accepted = None
    if hunks:
        ids = [h.strip() for h in hunks.split(",") if h.strip()]
        ds = engine.get(diff_id)
        accepted = {f.path: [h.id for h in f.hunks if h.id in ids] for f in ds.files}
    try:
        ds = engine.apply(diff_id, accepted, force=force)
    except DiffError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]applied[/green] {ds.id} ({ds.status})")


@app.command()
def rollback(diff_id: str) -> None:
    """Roll back a diff set."""
    _, engine, *_ = _service()
    try:
        ds = engine.rollback(diff_id)
    except DiffError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]rolled back[/green] {ds.id}")


@app.command()
def preview(diff_id: str) -> None:
    """Dry-run: show what a diff would change without writing."""
    _, engine, *_ = _service()
    try:
        data = engine.preview(diff_id)
    except DiffError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(json.dumps({"conflicts": data["conflicts"], "would_write": data["would_write"], "would_delete": data["would_delete"]}, indent=2))
    console.print(Syntax(data["unified"], "diff", theme="monokai"))


memory_app = typer.Typer(help="Memory notes")
app.add_typer(memory_app, name="memory")


@memory_app.command("add")
def memory_add(title: str, body: str) -> None:
    store = MemoryStore(CONFIG.home)
    entry = store.add(title, body)
    console.print(f"[green]saved[/green] {entry.id}")


@memory_app.command("list")
def memory_list() -> None:
    store = MemoryStore(CONFIG.home)
    for m in store.list():
        console.print(f"- {m['title']}")


@app.command()
def skills() -> None:
    """List auto-generated skills/lessons."""
    store = MemoryStore(CONFIG.home)
    for s in store.skills():
        console.print(Panel(s["body"], title=s["name"], border_style="magenta"))


if __name__ == "__main__":
    app()
