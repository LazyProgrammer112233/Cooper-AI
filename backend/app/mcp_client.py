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

# Read-only tools only: SQL reads (always through the guard) and FieldAssist's own business docs, used to learn
# metrics Cooper doesn't know yet. Write tools, code search and knowledge tools are never called.
ALLOWED_TOOLS = frozenset({"ch_query", "docs_search", "docs_get_report", "docs_get_page"})


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

        from .mcp_auth import NotConnected, _short, auth

        # A fixed token in .env wins; otherwise use the FieldAssist sign-in saved by the Connect button.
        oauth = None
        if not config.PULSE_MCP_TOKEN:
            if not auth.storage.has_tokens():
                raise DataError("Pulse MCP isn't connected yet. Click \"Connect and authorize Pulse MCP\" first.")
            oauth = auth.query_auth()
        body_error: BaseException | None = None
        try:
            async with streamablehttp_client(self.url, headers=_headers(), timeout=30, auth=oauth) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if oauth is not None:
                        auth.mark_ok()
                    try:
                        yield _Session(session)
                    except BaseException as exc:  # the caller's own error (guard, timeout): pass it on unchanged
                        body_error = exc
                        raise
        except DataError:
            raise
        except BaseException as exc:  # network, auth, protocol
            if body_error is not None:
                raise body_error from None
            if not isinstance(exc, Exception) and not isinstance(exc, BaseExceptionGroup):
                raise
            msg = _short(exc)
            if _is_not_connected(exc, NotConnected):
                auth.mark_expired()
                raise DataError("Your Pulse MCP sign-in has expired. Click \"Connect and authorize Pulse MCP\" again.") from exc
            raise DataError(f"Could not reach Pulse MCP at {self.url}: {msg}") from exc


def _is_not_connected(exc: BaseException, kind: type) -> bool:
    if isinstance(exc, kind):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_not_connected(e, kind) for e in exc.exceptions)
    return isinstance(exc.__cause__, kind) if exc.__cause__ else False


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
