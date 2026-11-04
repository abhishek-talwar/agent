"""The core agent loop: think -> call tools -> observe -> repeat.

Emits a stream of events so the web UI and CLI can render progress live. Every
file mutation the loop performs is routed through the diff engine by the tools,
so nothing is written without an approved diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from ..llm import LLMClient, Message, ToolCall
from ..tools.base import Registry

SYSTEM_PROMPT = """You are Helix, a careful, local-first software engineering agent.

You solve problems methodically:
1. Investigate before changing anything. Read files and run commands to gather evidence.
2. State a hypothesis before acting on it, and test it empirically (run code, run tests).
3. Make the smallest change that solves the problem. Prefer targeted edits over rewrites.
4. Never claim success without verifying. Run the tests or the command that proves it.
5. File writes go through a diff review step: call write_file/edit_file to propose a
   change; a human approves the diff before it lands.

Report honestly. If a command fails, say so and adapt. Keep responses concise.
"""


@dataclass
class AgentEvent:
    type: str  # assistant | tool_call | tool_result | error | done
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, **self.data}


@dataclass
class AgentRun:
    """Accumulated result of one agent loop invocation."""

    final: str = ""
    messages: list[Message] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    diff_ids: list[str] = field(default_factory=list)
    error: str | None = None


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        registry: Registry,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        max_iterations: int = 12,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.on_event = on_event

    def _emit(self, event: AgentEvent) -> None:
        if self.on_event:
            self.on_event(event)

    async def run(self, task: str, history: list[Message] | None = None) -> AgentRun:
        run = AgentRun()
        messages: list[Message] = [Message(role="system", content=self.system_prompt)]
        if history:
            messages.extend(history)
        messages.append(Message(role="user", content=task))
        schemas = self.registry.schemas()

        for _ in range(self.max_iterations):
            try:
                resp = await self.llm.complete(messages, tools=schemas)
            except Exception as e:  # noqa: BLE001 - surface provider errors to caller
                run.error = f"llm error: {e}"
                self._emit(AgentEvent("error", {"message": run.error}))
                break

            if resp.content:
                self._emit(AgentEvent("assistant", {"content": resp.content}))
                run.final = resp.content

            if not resp.tool_calls:
                break

            messages.append(Message(role="assistant", content=resp.content, tool_calls=resp.tool_calls))
            for call in resp.tool_calls:
                run.tool_calls.append(call)
                self._emit(AgentEvent("tool_call", {"name": call.name, "arguments": call.arguments}))
                tool = self.registry.get(call.name)
                if tool is None:
                    result_text = f"unknown tool: {call.name}"
                    self._emit(AgentEvent("tool_result", {"name": call.name, "output": result_text, "ok": False}))
                    messages.append(
                        Message(role="tool", content=result_text, tool_call_id=call.id, name=call.name)
                    )
                    continue
                try:
                    result = await tool.run(**call.arguments)
                except TypeError as e:
                    result = None
                    result_text = f"bad arguments for {call.name}: {e}"
                    self._emit(AgentEvent("tool_result", {"name": call.name, "output": result_text, "ok": False}))
                    messages.append(
                        Message(role="tool", content=result_text, tool_call_id=call.id, name=call.name)
                    )
                    continue
                if result.meta and result.meta.get("diff_id"):
                    run.diff_ids.append(result.meta["diff_id"])
                self._emit(
                    AgentEvent(
                        "tool_result",
                        {
                            "name": call.name,
                            "output": result.output,
                            "ok": result.ok,
                            "meta": result.meta or {},
                        },
                    )
                )
                messages.append(
                    Message(role="tool", content=result.output, tool_call_id=call.id, name=call.name)
                )

        run.messages = messages
        self._emit(AgentEvent("done", {"final": run.final, "diff_ids": run.diff_ids, "error": run.error}))
        return run

    async def stream(self, task: str) -> AsyncIterator[AgentEvent]:
        """Convenience async generator wrapper around ``run``."""
        events: list[AgentEvent] = []
        self.on_event = events.append
        import asyncio

        task_fut = asyncio.ensure_future(self.run(task))
        seen = 0
        while not task_fut.done() or seen < len(events):
            if seen < len(events):
                yield events[seen]
                seen += 1
            else:
                await asyncio.sleep(0.02)
        yield AgentEvent("final", {"result": (await task_fut).final})


def parse_plan_json(text: str) -> dict | None:
    """Best-effort extraction of a JSON object from an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
