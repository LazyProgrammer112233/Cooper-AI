"""The saved sign-in must be refreshed, not thrown away, after the access token expires."""

import asyncio
import json
import time

from mcp.shared.auth import OAuthToken

from backend.app import mcp_auth


def _auth(tmp_path):
    a = mcp_auth.PulseAuth("https://pulse.example/mcp")
    a.storage = mcp_auth.FileTokenStorage(tmp_path / "pulse.json", a.server_url)
    return a


def test_expiry_is_saved_and_restored_into_the_provider(tmp_path):
    a = _auth(tmp_path)
    asyncio.run(a.storage.set_tokens(OAuthToken(access_token="x", expires_in=3600, refresh_token="r")))
    provider = a.query_auth()
    asyncio.run(provider._initialize())
    expiry = provider.context.token_expiry_time
    assert expiry is not None and abs(expiry - (time.time() + 3600 - mcp_auth.REFRESH_MARGIN)) < 5


def test_expired_token_with_refresh_token_is_refreshed_first(tmp_path):
    a = _auth(tmp_path)
    asyncio.run(a.storage.set_tokens(OAuthToken(access_token="x", expires_in=10, refresh_token="r")))
    data = json.loads(a.storage.path.read_text())
    data["expires_at"] = int(time.time()) - 100
    a.storage.path.write_text(json.dumps(data))
    provider = a.query_auth()
    asyncio.run(provider._initialize())
    assert not provider.context.is_token_valid() and provider.context.current_tokens.refresh_token == "r"


def test_files_saved_before_this_fix_still_get_an_expiry(tmp_path):
    a = _auth(tmp_path)
    a.storage.path.write_text(json.dumps({"server_url": a.server_url, "saved_at": int(time.time()) - 7200,
                                          "tokens": {"access_token": "x", "token_type": "Bearer",
                                                     "expires_in": 3600, "refresh_token": "r"}}))
    assert a.storage.expires_at() < time.time()
    assert a.status()["connected"] and a.status()["can_refresh"]


def test_status_reports_an_expired_sign_in(tmp_path):
    a = _auth(tmp_path)
    asyncio.run(a.storage.set_tokens(OAuthToken(access_token="x", expires_in=3600)))
    assert a.status()["connected"]
    a.mark_expired()
    assert not a.status()["connected"] and a.status()["expired"]
    a.mark_ok()
    assert a.status()["connected"]


def test_lapsed_token_without_refresh_token_shows_not_connected(tmp_path):
    a = _auth(tmp_path)
    a.storage.path.write_text(json.dumps({"server_url": a.server_url, "saved_at": int(time.time()) - 7200,
                                          "tokens": {"access_token": "x", "token_type": "Bearer", "expires_in": 3600}}))
    assert not a.status()["connected"] and a.status()["expired"]
