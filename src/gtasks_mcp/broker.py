"""An OAuth authorization server for MCP clients that delegates sign-in to Google.

MCP clients such as confer.to expect to register themselves with the
authorization server (RFC 7591 dynamic client registration). Google does not
allow that, so this server acts as the authorization server MCP clients see,
and signs users in with its own Google OAuth "web application" client:

    MCP client --/register, /authorize--> gtasks-mcp --redirect--> Google sign-in
    MCP client <--code-- /oauth/google/callback <--code-- Google

Nothing is stored. Client registrations, sign-in state, authorization codes
and refresh tokens are all sealed (encrypted and authenticated) with a key
derived from the Google client secret, and handed to the client to keep. The
access token a client receives is Google's own access token for the Tasks
scope, which the resource server verifies with Google on every request.
Refresh tokens wrap Google's refresh token, bound to the client they were
issued to; refreshing them requires the Google client secret, so they are
useless outside this server.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx2
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    TokenVerifier,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from gtasks_mcp.auth import TASKS_SCOPE

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALLBACK_PATH = "/oauth/google/callback"

# How long a user has to finish Google sign-in, and a client to redeem its code.
SIGN_IN_TTL_SECONDS = 600
CODE_TTL_SECONDS = 300

logger = logging.getLogger(__name__)


class BrokerConfigError(ValueError):
    """Raised when the Google OAuth client file is missing or malformed."""


@dataclass(frozen=True)
class GoogleClient:
    """The Google OAuth client this server signs users in with."""

    client_id: str
    client_secret: str

    @classmethod
    def from_file(cls, path: Path) -> GoogleClient:
        """Load a client from Google's downloaded JSON (`{"web": {...}}`) or a flat object."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise BrokerConfigError(f"cannot read Google OAuth client file {path}: {exc}") from exc
        data = data.get("web") or data.get("installed") or data
        try:
            return cls(client_id=data["client_id"], client_secret=data["client_secret"])
        except (KeyError, TypeError):
            raise BrokerConfigError(
                f"{path} must contain client_id and client_secret (Google's downloaded client JSON works)"
            ) from None


class Sealer:
    """Encrypts and authenticates small JSON payloads, tagged by kind so one kind can't pose as another."""

    def __init__(self, secret: str) -> None:
        """Derive the sealing key from `secret`; changing the secret invalidates everything sealed."""
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"gtasks-mcp broker v1").derive(
            secret.encode()
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, kind: str, payload: dict[str, Any]) -> str:
        """Seal a payload as an opaque URL-safe string."""
        return self._fernet.encrypt(json.dumps({"kind": kind, "data": payload}).encode()).decode()

    def unseal(self, kind: str, token: str, ttl: int | None = None) -> dict[str, Any] | None:
        """Return the payload if `token` is a valid, unexpired seal of this kind, else None."""
        try:
            sealed = json.loads(self._fernet.decrypt(token.encode(), ttl=ttl))
        except (InvalidToken, ValueError):
            return None
        if not isinstance(sealed, dict) or sealed.get("kind") != kind:
            return None
        return sealed.get("data")


def _client_hash(client_id: str) -> str:
    """A short, fixed-size stand-in for a (long, sealed) client ID inside other seals."""
    return hashlib.sha256(client_id.encode()).hexdigest()[:32]


class BrokerAuthorizationCode(AuthorizationCode):
    """An authorization code carrying the Google tokens it will be exchanged for."""

    google_tokens: dict[str, Any]
    jti: str


class BrokerRefreshToken(RefreshToken):
    """A refresh token wrapping Google's refresh token."""

    google_refresh_token: str


class GoogleOAuthBroker:
    """MCP `OAuthAuthorizationServerProvider` that signs users in with Google."""

    def __init__(
        self,
        google: GoogleClient,
        public_origin: str,
        verifier: TokenVerifier,
        *,
        http_client: httpx2.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Sign in with `google`, receiving Google's redirect at `public_origin` + CALLBACK_PATH."""
        self._google = google
        self._sealer = Sealer(google.client_secret)
        self._verifier = verifier
        self._http_client = http_client or httpx2.AsyncClient(timeout=15.0)
        self._clock = clock
        self.callback_url = public_origin.rstrip("/") + CALLBACK_PATH
        # jti -> expiry of redeemed authorization codes, so each can be used once.
        self._redeemed_codes: dict[str, float] = {}

    # Client registration: the client ID *is* the sealed registration.

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Recover a registered client from its sealed client ID."""
        data = self._sealer.unseal("client", client_id)
        if data is None:
            return None
        return OAuthClientInformationFull.model_validate({**data, "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Replace the SDK's random client ID with a sealed copy of the registration.

        The SDK returns this same object to the registering client, so the
        client receives the sealed ID, and no registration needs storing.
        """
        registration = client_info.model_dump(mode="json", exclude={"client_id"}, exclude_none=True)
        client_info.client_id = self._sealer.seal("client", registration)
        logger.info("Registered OAuth client %r", client_info.client_name or "(unnamed)")

    # Authorization: send the user to Google, then back to the client with a code.

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Return the Google sign-in URL, carrying this request's parameters in sealed state."""
        if params.scopes and set(params.scopes) - {TASKS_SCOPE}:
            raise AuthorizeError("invalid_scope", f"Only the {TASKS_SCOPE} scope is available")
        state = self._sealer.seal(
            "sign-in",
            {
                "client": _client_hash(client.client_id),
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "resource": params.resource,
            },
        )
        query = {
            "client_id": self._google.client_id,
            "redirect_uri": self.callback_url,
            "response_type": "code",
            "scope": TASKS_SCOPE,
            # Offline access with forced consent, so Google always issues a refresh token.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(query)}"

    async def handle_google_callback(self, request: Request) -> Response:
        """Finish Google sign-in: trade Google's code for tokens and redirect to the client with our code."""
        sign_in = self._sealer.unseal(
            "sign-in", request.query_params.get("state", ""), ttl=SIGN_IN_TTL_SECONDS
        )
        if sign_in is None:
            return PlainTextResponse(
                "This sign-in link has expired or is invalid. Start again from your MCP client.",
                status_code=400,
            )

        def back_to_client(**params: str | None) -> Response:
            url = construct_redirect_uri(sign_in["redirect_uri"], state=sign_in["state"], **params)
            return RedirectResponse(url, status_code=302)

        if error := request.query_params.get("error"):
            return back_to_client(error="access_denied", error_description=f"Google sign-in failed: {error}")
        google_code = request.query_params.get("code")
        if not google_code:
            return back_to_client(error="invalid_request", error_description="Google returned no code")

        tokens = await self._google_token_request(
            {"grant_type": "authorization_code", "code": google_code, "redirect_uri": self.callback_url}
        )
        if tokens is None:
            return back_to_client(error="server_error", error_description="Could not complete Google sign-in")
        if TASKS_SCOPE not in str(tokens.get("scope", "")).split():
            return back_to_client(
                error="access_denied", error_description="Access to Google Tasks was not granted"
            )

        code = self._sealer.seal(
            "code",
            {
                "client": sign_in["client"],
                "redirect_uri": sign_in["redirect_uri"],
                "redirect_uri_provided_explicitly": sign_in["redirect_uri_provided_explicitly"],
                "code_challenge": sign_in["code_challenge"],
                "resource": sign_in["resource"],
                "jti": secrets.token_urlsafe(16),
                "google": {
                    "access_token": tokens["access_token"],
                    "expires_in": tokens.get("expires_in"),
                    "refresh_token": tokens.get("refresh_token"),
                },
            },
        )
        return back_to_client(code=code)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> BrokerAuthorizationCode | None:
        """Unseal a code issued to this client; the SDK then checks PKCE, redirect URI and expiry."""
        data = self._sealer.unseal("code", authorization_code, ttl=CODE_TTL_SECONDS)
        if data is None or data["client"] != _client_hash(client.client_id):
            return None
        return BrokerAuthorizationCode(
            code=authorization_code,
            scopes=[TASKS_SCOPE],
            expires_at=self._clock() + CODE_TTL_SECONDS,  # the seal's TTL already enforced expiry
            client_id=client.client_id,
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data["resource"],
            google_tokens=data["google"],
            jti=data["jti"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: BrokerAuthorizationCode
    ) -> OAuthToken:
        """Hand over Google's access token, plus a refresh token bound to this client."""
        now = self._clock()
        self._redeemed_codes = {jti: exp for jti, exp in self._redeemed_codes.items() if exp > now}
        if authorization_code.jti in self._redeemed_codes:
            raise TokenError("invalid_grant", "authorization code has already been used")
        self._redeemed_codes[authorization_code.jti] = now + CODE_TTL_SECONDS

        google = authorization_code.google_tokens
        return OAuthToken(
            access_token=google["access_token"],
            expires_in=google.get("expires_in"),
            scope=TASKS_SCOPE,
            refresh_token=self._seal_refresh_token(client, google.get("refresh_token")),
        )

    # Refresh.

    def _seal_refresh_token(
        self, client: OAuthClientInformationFull, google_refresh_token: str | None
    ) -> str | None:
        if not google_refresh_token:
            return None
        return self._sealer.seal(
            "refresh", {"client": _client_hash(client.client_id), "google": google_refresh_token}
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> BrokerRefreshToken | None:
        """Unseal a refresh token, accepting it only from the client it was issued to."""
        data = self._sealer.unseal("refresh", refresh_token)
        if data is None or data["client"] != _client_hash(client.client_id):
            return None
        return BrokerRefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=[TASKS_SCOPE],
            google_refresh_token=data["google"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: BrokerRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Get a fresh Google access token with the wrapped Google refresh token."""
        tokens = await self._google_token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token.google_refresh_token}
        )
        if tokens is None:
            raise TokenError("invalid_grant", "Google refused the refresh token; sign in again")
        rotated = tokens.get("refresh_token")
        return OAuthToken(
            access_token=tokens["access_token"],
            expires_in=tokens.get("expires_in"),
            scope=TASKS_SCOPE,
            refresh_token=self._seal_refresh_token(client, rotated) if rotated else refresh_token.token,
        )

    # Access tokens are Google's own.

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token with Google."""
        return await self._verifier.verify_token(token)

    async def revoke_token(self, token: AccessToken | BrokerRefreshToken) -> None:
        """Revoke the underlying Google grant (which ends both access and refresh tokens)."""
        google_token = token.google_refresh_token if isinstance(token, BrokerRefreshToken) else token.token
        try:
            await self._http_client.post(GOOGLE_REVOKE_URL, data={"token": google_token})
        except httpx2.HTTPError as exc:
            logger.warning("Google revoke request failed: %s", type(exc).__name__)

    async def _google_token_request(self, form: dict[str, str]) -> dict[str, Any] | None:
        """POST to Google's token endpoint with this server's client credentials."""
        form = {**form, "client_id": self._google.client_id, "client_secret": self._google.client_secret}
        try:
            response = await self._http_client.post(GOOGLE_TOKEN_URL, data=form)
        except httpx2.HTTPError as exc:
            logger.warning("Google token request failed: %s", type(exc).__name__)
            return None
        if response.status_code != 200:
            try:
                error = response.json().get("error")
            except ValueError:
                error = None
            logger.warning("Google token endpoint returned HTTP %d (%s)", response.status_code, error)
            return None
        tokens = response.json()
        return tokens if isinstance(tokens, dict) and tokens.get("access_token") else None
