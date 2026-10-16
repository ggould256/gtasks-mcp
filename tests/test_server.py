"""End-to-end tests of the HTTP app: OAuth discovery, auth enforcement, and tool calls."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx2
import pytest
from fakes import fake_client, google_error
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from gtasks_mcp.auth import TASKS_SCOPE
from gtasks_mcp.config import Settings
from gtasks_mcp.server import build_app

BASE_URL = "https://tasks.example.com"
SETTINGS = Settings(public_url=f"{BASE_URL}/mcp")

GOOD_TOKEN = "good-token"
NO_SCOPE_TOKEN = "no-scope-token"

EXPECTED_TOOLS = {
    "list_task_lists",
    "get_task_list",
    "create_task_list",
    "rename_task_list",
    "delete_task_list",
    "list_tasks",
    "get_task",
    "create_task",
    "update_task",
    "complete_task",
    "uncomplete_task",
    "move_task",
    "delete_task",
    "clear_completed",
}


class StubVerifier:
    """Accepts two fixed tokens, one with and one without the Tasks scope."""

    async def verify_token(self, token: str) -> AccessToken | None:
        scopes = {GOOD_TOKEN: [TASKS_SCOPE], NO_SCOPE_TOKEN: ["openid"]}.get(token)
        if scopes is None:
            return None
        return AccessToken(token=token, client_id="client-1", scopes=scopes, subject="user-1")


class Harness:
    """The app under test plus a record of the Google calls its tools make."""

    def __init__(self, *google_responses):
        self.tokens_seen: list[str] = []
        self.client, self.http = fake_client(*google_responses)
        self.app = build_app(SETTINGS, token_verifier=StubVerifier(), client_factory=self._factory)

    def _factory(self, token: str):
        self.tokens_seen.append(token)
        return self.client

    @asynccontextmanager
    async def http_client(self, token: str | None = None):
        """An httpx2 client wired to the app in-process, with the app's lifespan running."""
        starlette_app = self.app
        while not hasattr(starlette_app, "router"):  # unwrap middleware to reach the lifespan
            starlette_app = starlette_app.app
        headers = {"authorization": f"Bearer {token}"} if token else {}
        async with starlette_app.router.lifespan_context(starlette_app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=self.app), base_url=BASE_URL, headers=headers
            ) as client:
                yield client

    @asynccontextmanager
    async def mcp_client(self, token: str = GOOD_TOKEN):
        """An MCP client session to the app over Streamable HTTP."""
        async with self.http_client(token) as http_client:
            transport = streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client)
            async with Client(transport) as client:
                yield client


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.mark.anyio
async def test_protected_resource_metadata_points_at_google():
    async with Harness().http_client() as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == f"{BASE_URL}/mcp"
    assert metadata["authorization_servers"] == ["https://accounts.google.com"]
    assert metadata["scopes_supported"] == [TASKS_SCOPE]


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "bogus-token"])
async def test_missing_or_invalid_token_gets_401_challenge(token):
    async with Harness().http_client(token) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource/mcp"' in challenge


@pytest.mark.anyio
async def test_token_without_tasks_scope_gets_403():
    async with Harness().http_client(NO_SCOPE_TOKEN) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 403
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


@pytest.mark.anyio
async def test_unknown_host_is_rejected():
    async with Harness().http_client(GOOD_TOKEN) as client:
        response = await client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "host": "evil.example.net"}
        )
    assert response.status_code == 421


@pytest.mark.anyio
async def test_lists_all_tools():
    async with Harness().mcp_client() as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
    by_name = {tool.name: tool for tool in tools.tools}
    assert by_name["list_tasks"].annotations.read_only_hint is True
    assert by_name["delete_task"].annotations.destructive_hint is True
    assert "ctx" not in by_name["list_tasks"].input_schema["properties"]


@pytest.mark.anyio
async def test_tool_call_uses_the_requests_token():
    harness = Harness((200, {"items": [{"id": "T1", "title": "Milk", "etag": "e"}]}))
    async with harness.mcp_client() as client:
        result = await client.call_tool("list_tasks", {"task_list_id": "@default"})

    assert not result.is_error
    assert result.structured_content == {"result": [{"id": "T1", "title": "Milk"}]}
    assert harness.tokens_seen == [GOOD_TOKEN]
    assert harness.http.requests[0].path == "/tasks/v1/lists/@default/tasks"


@pytest.mark.anyio
async def test_google_error_is_reported_as_tool_error():
    harness = Harness(google_error(404, "Task not found"))
    async with harness.mcp_client() as client:
        result = await client.call_tool("get_task", {"task_list_id": "L1", "task_id": "nope"})

    assert result.is_error
    assert "Not found" in result.content[0].text


@pytest.mark.anyio
async def test_invalid_argument_is_reported_as_tool_error():
    async with Harness().mcp_client() as client:
        result = await client.call_tool(
            "update_task", {"task_list_id": "L1", "task_id": "T1", "status": "finished"}
        )

    assert result.is_error
    assert "status must be one of" in result.content[0].text
