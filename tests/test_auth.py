"""Tests for GoogleTokenVerifier against a mocked tokeninfo endpoint."""

from __future__ import annotations

import logging

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from gtasks_mcp.auth import TASKS_SCOPE, TOKENINFO_URL, GoogleTokenVerifier, bearer_token_from_headers

NOW = 1_800_000_000
TOKEN = "ya29.secret-token-value"


class TokenInfo:
    """A mock tokeninfo endpoint that counts calls and returns a configurable response."""

    def __init__(self, status: int = 200, **fields):
        self.status = status
        self.fields = {
            "azp": "client-1",
            "aud": "client-1",
            "scope": TASKS_SCOPE,
            "exp": str(NOW + 3600),
            "sub": "user-1",
            **fields,
        }
        self.calls: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": "invalid_token"})
        return httpx2.Response(200, json=self.fields)


def make_verifier(info: TokenInfo, clock=lambda: NOW, **kwargs) -> GoogleTokenVerifier:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(info.handler))
    return GoogleTokenVerifier(http_client=http_client, clock=clock, **kwargs)


@pytest.mark.anyio
async def test_valid_token():
    info = TokenInfo()
    token = await make_verifier(info).verify_token(TOKEN)

    assert token is not None
    assert token.token == TOKEN
    assert token.client_id == "client-1"
    assert token.scopes == [TASKS_SCOPE]
    assert token.expires_at == NOW + 3600
    assert token.subject == "user-1"
    # The token travels in the POST body, never in the URL.
    request = info.calls[0]
    assert str(request.url) == TOKENINFO_URL
    assert request.method == "POST"
    assert TOKEN not in str(request.url)


@pytest.mark.anyio
async def test_rejected_by_google():
    assert await make_verifier(TokenInfo(status=400)).verify_token(TOKEN) is None


@pytest.mark.anyio
async def test_expired_token():
    assert await make_verifier(TokenInfo(exp=str(NOW - 1))).verify_token(TOKEN) is None


@pytest.mark.anyio
async def test_expires_in_used_when_exp_missing():
    info = TokenInfo(expires_in="120")
    del info.fields["exp"]
    token = await make_verifier(info).verify_token(TOKEN)
    assert token is not None and token.expires_at == NOW + 120


@pytest.mark.anyio
async def test_scope_is_reported_not_enforced_here():
    # Scope enforcement belongs to the MCP auth middleware, which answers 403 insufficient_scope.
    token = await make_verifier(TokenInfo(scope="openid email")).verify_token(TOKEN)
    assert token is not None and token.scopes == ["openid", "email"]


@pytest.mark.anyio
async def test_client_id_allowlist():
    info = TokenInfo(azp="someone-else", aud="someone-else")
    assert await make_verifier(info, allowed_client_ids={"client-1"}).verify_token(TOKEN) is None
    assert await make_verifier(TokenInfo(), allowed_client_ids={"client-1"}).verify_token(TOKEN) is not None


@pytest.mark.anyio
async def test_network_failure_is_rejection_and_does_not_log_token(caplog):
    def explode(request):
        raise httpx2.ConnectError(f"cannot reach {request.url} body={request.content!r}")

    verifier = GoogleTokenVerifier(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(explode)))
    with caplog.at_level(logging.DEBUG):
        assert await verifier.verify_token(TOKEN) is None
    assert TOKEN not in caplog.text
    assert "ConnectError" in caplog.text


@pytest.mark.anyio
async def test_cache_hit_within_ttl_and_refetch_after():
    now = [NOW]
    info = TokenInfo()
    verifier = make_verifier(info, clock=lambda: now[0], cache_ttl_seconds=60)

    assert await verifier.verify_token(TOKEN) is not None
    now[0] += 30
    assert await verifier.verify_token(TOKEN) is not None
    assert len(info.calls) == 1

    now[0] += 31
    assert await verifier.verify_token(TOKEN) is not None
    assert len(info.calls) == 2


@pytest.mark.anyio
async def test_cache_never_outlives_token_expiry():
    now = [NOW]
    info = TokenInfo(exp=str(NOW + 10))
    verifier = make_verifier(info, clock=lambda: now[0], cache_ttl_seconds=60)

    assert await verifier.verify_token(TOKEN) is not None
    now[0] += 11
    info.status = 400
    assert await verifier.verify_token(TOKEN) is None
    assert len(info.calls) == 2


@pytest.mark.anyio
async def test_failures_are_not_cached():
    info = TokenInfo(status=400)
    verifier = make_verifier(info)
    assert await verifier.verify_token(TOKEN) is None
    info.status = 200
    assert await verifier.verify_token(TOKEN) is not None


def test_bearer_token_from_headers():
    assert bearer_token_from_headers({"authorization": "Bearer abc"}) == "abc"
    assert bearer_token_from_headers({"Authorization": "bearer  abc "}) == "abc"
    for headers in (None, {}, {"authorization": "Basic abc"}, {"authorization": "Bearer "}):
        with pytest.raises(ToolError):
            bearer_token_from_headers(headers)
