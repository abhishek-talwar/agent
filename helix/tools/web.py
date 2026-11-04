"""Web fetch tool for research. Network is opt-in and bounded."""

from __future__ import annotations

import httpx

from .base import ToolContext, ToolResult


class WebFetchTool:
    name = "web_fetch"
    description = (
        "Fetch a URL and return its text content (truncated). Useful for reading docs "
        "or error pages referenced by a problem."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."},
        },
        "required": ["url"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, url: str) -> ToolResult:
        if not url.startswith(("http://", "https://")):
            return ToolResult(ok=False, output="refused: only http(s) URLs are allowed")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Helix-Agent/0.1"})
        except httpx.HTTPError as e:
            return ToolResult(ok=False, output=f"fetch failed: {e}")
        text = resp.text
        if len(text) > 20000:
            text = text[:20000] + "\n... [truncated]"
        return ToolResult(ok=resp.is_success, output=text, meta={"status": resp.status_code})


class WebSearchTool:
    name = "web_search"
    description = "Search the web via DuckDuckGo's HTML endpoint and return result titles/links."
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query."}},
        "required": ["query"],
    }

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    async def run(self, query: str) -> ToolResult:
        import re
        from urllib.parse import quote_plus

        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Helix)"})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return ToolResult(ok=False, output=f"search failed: {e}")
        links = re.findall(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        out = []
        for href, title in links[:10]:
            clean = re.sub(r"<[^>]+>", "", title).strip()
            out.append(f"- {clean}\n  {href}")
        return ToolResult(ok=True, output="\n".join(out) or "(no results)")
