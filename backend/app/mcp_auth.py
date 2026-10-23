"""Sign in to Pulse MCP with a FieldAssist ID (standard MCP OAuth: discovery, client
registration, PKCE, refresh), started from the "Connect and authorize" button in the UI.

Flow:
    browser  POST /api/mcp/connect  -> server starts an MCP session that needs sign-in and
                                       returns the FieldAssist sign-in URL
    browser  goes to that URL, signs in, comes back to /api/mcp/callback?code=...&state=...
    server   hands the code to the waiting session, which swaps it for tokens and saves them

Tokens are kept in one local file (backend/data/auth/, git-ignored) and refreshed
automatically. Queries never start a sign-in themselves: without tokens they fail with
NotConnected and the UI shows the button again.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from pydantic import AnyUrl

from . import config


class NotConnected(Exception):
    """Pulse MCP has not been authorized on this laptop yet (or the sign-in expired)."""


class FileTokenStorage:
    """Tokens and the registered client for one MCP server, in a single JSON file."""

    def __init__(self, path: Path, server_url: str):
        self.path = path
        self.server_url = server_url

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if data.get("server_url") == self.server_url else {}

    def _write(self, **changes) -> None:
        data = self._read()
        data.update(changes, server_url=self.server_url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        self._write(tokens=tokens.model_dump(mode="json", exclude_none=True), saved_at=int(time.time()))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._write(client=client_info.model_dump(mode="json", exclude_none=True))

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class PulseAuth:
    def __init__(self, server_url: str | None = None):
        self.server_url = server_url or config.PULSE_MCP_URL
        self.storage = FileTokenStorage(config.AUTH_DIR / "pulse_mcp.json", self.server_url)
        self.redirect_uri = f"{config.PUBLIC_URL.rstrip('/')}/api/mcp/callback"
        self._flow: asyncio.Task | None = None
        self._auth_url: asyncio.Future | None = None
        self._code: asyncio.Future | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ providers

    def _provider(self, redirect_handler, callback_handler):
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        metadata = OAuthClientMetadata(
            client_name="Cooper AI (local)",
            redirect_uris=[AnyUrl(self.redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        return OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=metadata,
            storage=self.storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=300,
        )

    def query_auth(self):
        """Auth for normal queries: uses and refreshes saved tokens, never starts a sign-in."""

        async def no_redirect(_url: str) -> None:
            raise NotConnected("Pulse MCP sign-in is needed")

        async def no_callback() -> tuple[str, str | None]:
            raise NotConnected("Pulse MCP sign-in is needed")

        return self._provider(no_redirect, no_callback)

    # ------------------------------------------------------------------ status

    @property
    def pending(self) -> bool:
        return self._flow is not None and not self._flow.done()

    def status(self) -> dict:
        return {
            "connected": self.storage.has_tokens() and not self.pending,
            "pending": self.pending,
            "server_url": self.server_url,
            "error": self.last_error,
        }

    def disconnect(self) -> None:
        if self.pending:
            self._flow.cancel()
        self.storage.clear()
        self.last_error = None

    # ------------------------------------------------------------------ sign-in flow

    async def start(self) -> dict:
        """Begin sign-in. Returns {"auth_url": ...}, or {"connected": True} if saved tokens still work."""
        if self.pending and self._auth_url is not None and self._auth_url.done():
            return {"auth_url": self._auth_url.result()}
        if self.pending:
            self._flow.cancel()

        loop = asyncio.get_running_loop()
        self._auth_url = loop.create_future()
        self._code = loop.create_future()
        self.last_error = None
        auth_url, code = self._auth_url, self._code

        async def redirect_handler(url: str) -> None:
            if not auth_url.done():
                auth_url.set_result(url)

        async def callback_handler() -> tuple[str, str | None]:
            return await code

        provider = self._provider(redirect_handler, callback_handler)
        self._flow = asyncio.create_task(self._handshake(provider))

        done, _ = await asyncio.wait({auth_url, self._flow}, timeout=25, return_when=asyncio.FIRST_COMPLETED)
        if auth_url in done:
            return {"auth_url": auth_url.result()}
        if self._flow in done:
            exc = self._flow.exception()
            if exc:
                raise exc
            return {"connected": True}
        self._flow.cancel()
        raise TimeoutError("Pulse MCP didn't answer the sign-in request in time")

    async def _handshake(self, provider) -> None:
        """Opens one MCP session with the interactive provider and checks ch_query is offered."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        try:
            async with streamablehttp_client(self.server_url, timeout=30, auth=provider) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if "ch_query" not in {t.name for t in tools.tools}:
                        raise RuntimeError("This Pulse MCP endpoint doesn't offer the ch_query tool. "
                                           "Use the main endpoint, e.g. https://ado-mcp.fieldassist.io/mcp")
        except Exception as exc:
            self.last_error = _short(exc)
            raise

    async def finish(self, code: str | None, state: str | None, error: str | None) -> dict:
        """Called by /api/mcp/callback with what the sign-in page sent back."""
        if not self.pending or self._code is None:
            self.last_error = "The sign-in link has expired. Please click Connect again."
            return self.status()
        if error or not code:
            self._flow.cancel()
            self.last_error = f"Sign-in was not completed ({error or 'no code returned'})."
            return self.status()
        if not self._code.done():
            self._code.set_result((code, state))
        try:
            await asyncio.wait_for(asyncio.shield(self._flow), timeout=40)
        except Exception:
            pass  # last_error was set by _handshake
        return self.status()


def _short(exc: BaseException) -> str:
    """Unwrap anyio exception groups to the first real message."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc) or type(exc).__name__
    return text[:300]


auth = PulseAuth()
