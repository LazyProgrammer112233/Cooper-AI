"""Pulse MCP client.

Cooper talks to data only through Pulse MCP, and only through one read-only tool:
`ch_query` on the shared analytics server. Every other Pulse MCP tool (PRs, work
items, knowledge writes, Bugsink, SQL Server, Postgres) is unreachable from here,
whatever the LLM or the user asks for.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from . import config

ALLOWED_TOOLS = frozenset({"ch_query"})


class DataError(Exception):
    """The data source returned an error or could not be reached."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    elapsed: float
    truncated: bool = False
    sql: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if config.PULSE_MCP_TOKEN:
        headers["Authorization"] = f"Bearer {config.PULSE_MCP_TOKEN}"
    for part in config.PULSE_MCP_HEADERS.split(";"):
        if ":" in part:
            name, value = part.split(":", 1)
            headers[name.strip()] = value.strip()
    return headers


def parse_tool_text(text: str) -> dict[str, Any]:
    """Pulse MCP returns one JSON document per call as text content."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError(f"Unexpected reply from Pulse MCP: {text[:200]}") from exc
    if isinstance(payload, dict) and payload.get("error"):
        raise DataError(str(payload["error"])[:500])
    return payload


class PulseMCP:
    """Opens one MCP session per question and runs the question's queries on it."""

    def __init__(self, url: str | None = None):
        self.url = url or config.PULSE_MCP_URL

    @asynccontextmanager
    async def session(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        try:
            async with streamablehttp_client(self.url, headers=_headers(), timeout=30) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield _Session(session)
        except DataError:
            raise
        except Exception as exc:  # network, auth, protocol
            raise DataError(f"Could not reach Pulse MCP at {self.url}: {exc}") from exc


class _Session:
    def __init__(self, session):
        self._session = session

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in ALLOWED_TOOLS:
            raise PermissionError(f"Tool {tool!r} is not allowed for Cooper")
        result = await self._session.call_tool(tool, arguments)
        texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        if result.isError:
            raise DataError(" ".join(texts)[:500] or "Pulse MCP tool error")
        return parse_tool_text("".join(texts))

    async def ch_query(self, sql: str, top: int = config.MAX_ROWS, timeout: float = 20) -> QueryResult:
        started = time.monotonic()
        args = {
            "server": config.CH_SERVER,
            "database": config.CH_DATABASE,
            "query": sql,
            "top": top,
            "maxChars": 60000,
        }
        try:
            payload = await asyncio.wait_for(self.call("ch_query", args), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DataError("The data query took too long") from exc
        return QueryResult(
            columns=payload.get("columns", []),
            rows=payload.get("rows", []),
            elapsed=time.monotonic() - started,
            truncated=bool(payload.get("truncated")),
            sql=sql,
        )
