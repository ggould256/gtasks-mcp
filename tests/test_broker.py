"""End-to-end tests of the built-in OAuth broker, with Google's endpoints mocked."""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fakes import fake_client
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from gtasks_mcp.auth import TASKS_SCOPE, TOKENINFO_URL
from gtasks_mcp.broker import (
    CALLBACK_PATH,
    GOOGLE_AUTH_URL,
    GOOGLE_REVOKE_URL,
    GOOGLE_TOKEN_URL,
    BrokerConfigError,
    GoogleClient,
    Sealer,
)
from gtasks_mcp.config import Settings
from gtasks_mcp.server import build_app

BASE_URL = "https://tasks.example.com"
CLIENT_ORIGIN = "https://confer.example"
REDIRECT_URI = f"{CLIENT_ORIGIN}/oauth/callback"
GOOGLE_CLIENT_ID = "broker-client.apps.googleusercontent.com"
VERIFIER = "a-sufficiently-long-pkce-code-verifier-0123456789abcdef"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class FakeGoogle:
    """Google's token, tokeninfo and revoke endpoints."""

    def __init__(self) -> None:
        self.scope = TASKS_SCOPE
        self.token_requests: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.refresh_ok = True

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        url = str(request.url)
        if url == GOOGLE_TOKEN_URL:
            self.token_requests.append(form)
            if form["grant_type"] == "authorization_code":
                return httpx2.Response(
                    200,
                    json={
                        "access_token": "ya29.first",
                        "refresh_token": "1//google-refresh",
                        "expires_in": 3599,
                        "scope": self.scope,
                        "token_type": "Bearer",
                    },
                )
            if not self.refresh_ok:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(
                200,
                json={
                    "access_token": "ya29.second",
                    "expires_in": 3599,
                    "scope": self.scope,
                    "token_type": "Bearer",
                },
            )
        if url == TOKENINFO_URL:
            if form["access_token"].startswith("ya29."):
                return httpx2.Response(
                    200,
                    json={
                        "azp": GOOGLE_CLIENT_ID,
                        "aud": GOOGLE_CLIENT_ID,
                        "scope": TASKS_SCOPE,
                        "exp": "4000000000",
                        "sub": "user-1",
                    },
                )
            return httpx2.Response(400, json={"error": "invalid_token"})
        if url == GOOGLE_REVOKE_URL:
            self.revoked.append(form["token"])
            return httpx2.Response(200)
        raise AssertionError(f"unexpected Google request {url}")


@pytest.fixture
def google_client_file(tmp_path):
    path = tmp_path / "google-client.json"
    path.write_text(json.dumps({"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": "s3cret"}}))
    return path


class Harness:
    def __init__(self, google_client_file, *google_api_responses):
        self.google = FakeGoogle()
        self.tasks_client, self.tasks_http = fake_client(*google_api_responses)
        self.tokens_seen: list[str] = []
        settings = Settings(
            public_url=f"{BASE_URL}/mcp",
            allowed_client_ids=frozenset({"some-other-client"}),
            allowed_origins=frozenset({CLIENT_ORIGIN}),
            google_client_file=google_client_file,
        )
        google_http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.google.handler))
        self.app = build_app(settings, client_factory=self._factory, http_client=google_http)

    def _factory(self, token):
        self.tokens_seen.append(token)
        return self.tasks_client

    @asynccontextmanager
    async def lifespan(self):
        """Run the app's lifespan (which can only happen once per app)."""
        starlette_app = self.app
        while not hasattr(starlette_app, "router"):
            starlette_app = starlette_app.app
        async with starlette_app.router.lifespan_context(starlette_app):
            yield

    def client(self, **headers):
        """An httpx2 client wired to the app in-process; use inside lifespan()."""
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url=BASE_URL, headers=headers
        )

    @asynccontextmanager
    async def http(self, **headers):
        async with self.lifespan(), self.client(**headers) as client:
            yield client


async def register(client, **overrides):
    body = {
        "redirect_uris": [REDIRECT_URI],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Test client",
        **overrides,
    }
    response = await client.post("/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def authorize(client, client_id):
    response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "client-state",
            "scope": TASKS_SCOPE,
        },
    )
    assert response.status_code == 302, response.text
    return response.headers["location"]


async def sign_in(client, client_id):
    """Run /authorize and Google's callback; return the code redirected to the client."""
    google_url = urlparse(await authorize(client, client_id))
    google_state = parse_qs(google_url.query)["state"][0]
    response = await client.get(CALLBACK_PATH, params={"code": "google-code", "state": google_state})
    assert response.status_code == 302, response.text
    back = urlparse(response.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT_URI
    return parse_qs(back.query)


async def redeem(client, client_id, code):
    return await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )


@pytest.mark.anyio
async def test_metadata_points_clients_at_this_server(google_client_file):
    async with Harness(google_client_file).http() as client:
        resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
        server = (await client.get("/.well-known/oauth-authorization-server")).json()

    assert resource["authorization_servers"] == [BASE_URL]
    assert server["issuer"] == BASE_URL
    assert server["registration_endpoint"] == f"{BASE_URL}/register"
    assert server["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert server["token_endpoint"] == f"{BASE_URL}/token"
    assert "S256" in server["code_challenge_methods_supported"]


@pytest.mark.anyio
async def test_full_sign_in_flow_then_tool_call(google_client_file):
    harness = Harness(google_client_file, (200, {"items": [{"id": "T1", "title": "Milk"}]}))
    async with harness.lifespan(), harness.client() as client:
        registration = await register(client)
        client_id = registration["client_id"]
        assert "client_secret" not in registration  # public client

        google_url = urlparse(await authorize(client, client_id))
        assert f"{google_url.scheme}://{google_url.netloc}{google_url.path}" == GOOGLE_AUTH_URL
        google_params = parse_qs(google_url.query)
        assert google_params["client_id"] == [GOOGLE_CLIENT_ID]
        assert google_params["redirect_uri"] == [f"{BASE_URL}{CALLBACK_PATH}"]
        assert google_params["scope"] == [TASKS_SCOPE]
        assert google_params["access_type"] == ["offline"]

        callback = await client.get(
            CALLBACK_PATH, params={"code": "google-code", "state": google_params["state"][0]}
        )
        back = parse_qs(urlparse(callback.headers["location"]).query)
        assert back["state"] == ["client-state"]

        token_response = await redeem(client, client_id, back["code"][0])
        assert token_response.status_code == 200, token_response.text
        tokens = token_response.json()

        assert tokens["access_token"] == "ya29.first"  # Google's own token, passed through
        assert tokens["scope"] == TASKS_SCOPE
        assert tokens["refresh_token"] and "google-refresh" not in tokens["refresh_token"]  # sealed
        exchange = harness.google.token_requests[0]
        assert exchange["client_secret"] == "s3cret"
        assert exchange["redirect_uri"] == f"{BASE_URL}{CALLBACK_PATH}"

        async with harness.client(authorization=f"Bearer {tokens['access_token']}") as http_client:
            transport = streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client)
            async with Client(transport) as mcp_client:
                result = await mcp_client.call_tool("list_tasks", {"task_list_id": "@default"})
    assert not result.is_error
    assert harness.tokens_seen == ["ya29.first"]


@pytest.mark.anyio
async def test_authorization_code_is_single_use_and_needs_pkce(google_client_file):
    async with Harness(google_client_file).http() as client:
        client_id = (await register(client))["client_id"]
        code = (await sign_in(client, client_id))["code"][0]

        wrong_verifier = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": "x" * 50,
            },
        )
        assert wrong_verifier.status_code == 400

        assert (await redeem(client, client_id, code)).status_code == 200
        replay = await redeem(client, client_id, code)
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"


@pytest.mark.anyio
async def test_code_and_refresh_token_are_bound_to_their_client(google_client_file):
    async with Harness(google_client_file).http() as client:
        alice = (await register(client))["client_id"]
        mallory = (await register(client, client_name="Other"))["client_id"]
        code = (await sign_in(client, alice))["code"][0]

        assert (await redeem(client, mallory, code)).status_code == 400
        refresh = (await redeem(client, alice, code)).json()["refresh_token"]

        stolen = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": mallory}
        )
        assert stolen.status_code == 400


@pytest.mark.anyio
async def test_refresh(google_client_file):
    harness = Harness(google_client_file)
    async with harness.http() as client:
        client_id = (await register(client))["client_id"]
        code = (await sign_in(client, client_id))["code"][0]
        refresh = (await redeem(client, client_id, code)).json()["refresh_token"]

        response = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
        )
        assert response.status_code == 200, response.text
        assert response.json()["access_token"] == "ya29.second"
        assert response.json()["refresh_token"] == refresh  # Google didn't rotate, so neither do we
        assert harness.google.token_requests[-1]["refresh_token"] == "1//google-refresh"

        harness.google.refresh_ok = False
        refused = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
        )
        assert refused.status_code == 400
        assert refused.json()["error"] == "invalid_grant"


@pytest.mark.anyio
async def test_registration_survives_restart_without_storage(google_client_file):
    async with Harness(google_client_file).http() as client:
        client_id = (await register(client))["client_id"]
    # A brand-new server instance (same Google secret) still recognises the client.
    async with Harness(google_client_file).http() as client:
        assert (await authorize(client, client_id)).startswith(GOOGLE_AUTH_URL)


@pytest.mark.anyio
async def test_tampered_client_id_is_rejected(google_client_file):
    async with Harness(google_client_file).http() as client:
        client_id = (await register(client))["client_id"]
        response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id[:-4] + "AAAA",
                "redirect_uri": REDIRECT_URI,
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "state": "s",
            },
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_unregistered_redirect_uri_is_rejected(google_client_file):
    async with Harness(google_client_file).http() as client:
        client_id = (await register(client))["client_id"]
        response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://evil.example/cb",
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "state": "s",
            },
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_google_denial_and_missing_scope_reach_the_client_as_errors(google_client_file):
    harness = Harness(google_client_file)
    async with harness.http() as client:
        client_id = (await register(client))["client_id"]

        state = parse_qs(urlparse(await authorize(client, client_id)).query)["state"][0]
        denied = await client.get(CALLBACK_PATH, params={"error": "access_denied", "state": state})
        assert parse_qs(urlparse(denied.headers["location"]).query)["error"] == ["access_denied"]

        harness.google.scope = "openid"  # user unticked the Tasks checkbox
        unscoped = await sign_in(client, client_id)
        assert unscoped["error"] == ["access_denied"]
        assert "code" not in unscoped


@pytest.mark.anyio
async def test_bad_callback_state_is_refused(google_client_file):
    async with Harness(google_client_file).http() as client:
        response = await client.get(CALLBACK_PATH, params={"code": "c", "state": "forged"})
    assert response.status_code == 400


@pytest.mark.anyio
async def test_revoke_revokes_the_google_grant(google_client_file):
    harness = Harness(google_client_file)
    async with harness.http() as client:
        client_id = (await register(client))["client_id"]
        code = (await sign_in(client, client_id))["code"][0]
        refresh = (await redeem(client, client_id, code)).json()["refresh_token"]
        # The SDK's revocation model requires client_secret even for public clients.
        response = await client.post(
            "/revoke",
            data={
                "token": refresh,
                "client_id": client_id,
                "client_secret": "",
                "token_type_hint": "refresh_token",
            },
        )
    assert response.status_code == 200
    assert harness.google.revoked == ["1//google-refresh"]


@pytest.mark.anyio
async def test_cors_on_mcp_endpoint(google_client_file):
    async with Harness(google_client_file).http() as client:
        preflight = await client.options(
            "/mcp",
            headers={
                "origin": CLIENT_ORIGIN,
                "access-control-request-method": "POST",
                "access-control-request-headers": "authorization, content-type, mcp-protocol-version",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == CLIENT_ORIGIN

        unauthenticated = await client.post("/mcp", json={}, headers={"origin": CLIENT_ORIGIN})
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["access-control-allow-origin"] == CLIENT_ORIGIN
        assert "www-authenticate" in unauthenticated.headers["access-control-expose-headers"].lower()

        foreign = await client.options(
            "/mcp",
            headers={
                "origin": "https://evil.example",
                "access-control-request-method": "POST",
            },
        )
        assert foreign.status_code == 400


def test_sealer_keeps_kinds_apart():
    sealer = Sealer("secret")
    token = sealer.seal("client", {"a": 1})
    assert sealer.unseal("client", token) == {"a": 1}
    assert sealer.unseal("refresh", token) is None
    assert Sealer("other-secret").unseal("client", token) is None
    assert sealer.unseal("client", "garbage") is None


def test_google_client_file_formats(tmp_path):
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"client_id": "id", "client_secret": "secret"}))
    assert GoogleClient.from_file(flat) == GoogleClient("id", "secret")

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"web": {"client_id": "id"}}))
    with pytest.raises(BrokerConfigError):
        GoogleClient.from_file(broken)
    with pytest.raises(BrokerConfigError):
        GoogleClient.from_file(tmp_path / "missing.json")
