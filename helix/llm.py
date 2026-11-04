"""LLM provider abstraction.

Supports any OpenAI-compatible chat-completions endpoint (OpenAI, OpenRouter,
Ollama, LM Studio, llama.cpp, vLLM) with tool calling, plus a deterministic
offline ``MockLLM`` so the whole system runs and is testable with no keys.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import ModelConfig


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list["ToolCall"] = field(default_factory=list)

    def to_api(self) -> dict:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_api() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

    def to_api(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict | None = None


class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse: ...


class OpenAICompatClient:
    """Thin async client for ``/chat/completions`` with tool support."""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.to_api() for m in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        return LLMResponse(content=msg.get("content") or "", tool_calls=calls, raw=data)


# --------------------------------------------------------------------------
# Offline mock: makes the agent fully runnable and deterministic without a model.
# --------------------------------------------------------------------------
class MockLLM:
    """A scripted model good enough to exercise every code path offline.

    It understands a small set of intents via keyword matching and always emits
    a tool call when one is plausible, so the planner/tool loop is exercised.
    """

    def __init__(self) -> None:
        self._call_counter = 0

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        tool_names = {t["function"]["name"] for t in (tools or [])}
        tool_msgs = [m for m in messages if m.role == "tool"]

        # If we've already used tools, produce a final answer.
        if tool_msgs:
            return LLMResponse(content=self._summarize(messages))

        text = last_user.lower()
        # Shell intent
        m = re.search(r"run[:\s]+(.+)", last_user, re.IGNORECASE)
        if m and "shell" in tool_names:
            return self._call("shell", {"command": m.group(1).strip()})
        # Write-file intent
        m = re.search(r"write file ([^\s]+) with content:\s*(.*)", last_user, re.IGNORECASE | re.DOTALL)
        if m and "write_file" in tool_names:
            return self._call("write_file", {"path": m.group(1), "content": m.group(2)})
        # List-files intent
        if ("list" in text or "ls " in text) and "list_files" in tool_names:
            return self._call("list_files", {"path": "."})
        # Search intent
        if "search" in text and "search" in tool_names:
            q = last_user.split("search", 1)[-1].strip(" :\"'")
            return self._call("search", {"pattern": q or "."})
        return LLMResponse(content=self._answer(last_user))

    def _call(self, name: str, args: dict) -> LLMResponse:
        self._call_counter += 1
        return LLMResponse(
            tool_calls=[ToolCall(id=f"mock-{self._call_counter}", name=name, arguments=args)]
        )

    def _answer(self, prompt: str) -> str:
        return (
            "[mock-llm] I received your request. Configure a real model via HELIX_BASE_URL / "
            "HELIX_MODEL (or use Ollama locally) for full capability.\n\n"
            f"Request: {prompt.strip()[:400]}"
        )

    def _summarize(self, messages: list[Message]) -> str:
        tool_outputs = [m.content for m in messages if m.role == "tool"]
        joined = "\n".join(o[:500] for o in tool_outputs[-3:])
        return f"[mock-llm] Completed tool calls. Results:\n{joined}"


def build_client(config: ModelConfig, *, force_mock: bool = False) -> LLMClient:
    if force_mock:
        return MockLLM()
    return OpenAICompatClient(config)
