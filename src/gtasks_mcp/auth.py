"""Verification of client-supplied Google OAuth access tokens.

The client agent performs the OAuth2 flow with Google itself and sends the
resulting access token as an `Authorization: Bearer` header on every request.
This server only checks that token with Google and uses it for the duration of
that request; it never persists or logs it.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Collection, Mapping

import httpx2
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
GOOGLE_ISSUER = "https://accounts.google.com"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

# Upper bound on how long a successful verification is reused before asking
# Google again, so a revoked token stops working within this window.
DEFAULT_CACHE_TTL_SECONDS = 60
_MAX_CACHE_ENTRIES = 1024

logger = logging.getLogger(__name__)


class GoogleTokenVerifier:
    """MCP `TokenVerifier` that validates Google access tokens via Google's tokeninfo endpoint."""

    def __init__(
        self,
        allowed_client_ids: Collection[str] = (),
        *,
        http_client: httpx2.AsyncClient | None = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Accept tokens issued to any of `allowed_client_ids` (or to any client, if empty)."""
        self._allowed_client_ids = frozenset(allowed_client_ids)
        self._http_client = http_client or httpx2.AsyncClient(timeout=10.0)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        # sha256(token) -> (cache-until timestamp, verified token). In memory only.
        self._cache: dict[str, tuple[float, AccessToken]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return access info for a valid Google token, or None if Google does not accept it.

        Scope checking is left to the MCP auth middleware, which answers a token
        lacking the Tasks scope with a 403 `insufficient_scope` challenge.
        """
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        info = await self._fetch_tokeninfo(token)
        if info is None:
            return None
        access_token = self._access_token_from_info(token, info, now)
        if access_token is None:
            return None

        self._remember(key, access_token, now)
        return access_token

    async def _fetch_tokeninfo(self, token: str) -> Mapping[str, object] | None:
        """Ask Google about `token`; returns the tokeninfo payload, or None if it is not valid."""
        try:
            # POST keeps the token out of the URL, and therefore out of any request logging.
            response = await self._http_client.post(TOKENINFO_URL, data={"access_token": token})
        except httpx2.HTTPError as exc:
            # Exception text can include request details; log only the type.
            logger.warning("Google tokeninfo request failed: %s", type(exc).__name__)
            return None
        if response.status_code != 200:
            logger.info("Google rejected a bearer token (tokeninfo HTTP %d)", response.status_code)
            return None
        try:
            info = response.json()
        except ValueError:
            logger.warning("Google tokeninfo returned a non-JSON response")
            return None
        return info if isinstance(info, dict) else None

    def _access_token_from_info(
        self, token: str, info: Mapping[str, object], now: float
    ) -> AccessToken | None:
        """Convert a tokeninfo payload to an AccessToken, applying the client-ID allowlist."""
        client_id = str(info.get("azp") or info.get("aud") or "")
        if self._allowed_client_ids and client_id not in self._allowed_client_ids:
            logger.info("Rejected a bearer token issued to a client not in the allowlist")
            return None

        try:
            expires_at = int(str(info["exp"]))
        except (KeyError, ValueError):
            try:
                expires_at = int(now) + int(str(info["expires_in"]))
            except (KeyError, ValueError):
                expires_at = None
        if expires_at is not None and expires_at <= now:
            return None

        scope = info.get("scope")
        scopes = str(scope).split() if scope else []
        subject = info.get("sub")
        return AccessToken(
            token=token,
            client_id=client_id or "unknown",
            scopes=scopes,
            expires_at=expires_at,
            subject=str(subject) if subject else None,
            claims={"iss": GOOGLE_ISSUER},
        )

    def _remember(self, key: str, access_token: AccessToken, now: float) -> None:
        """Cache a successful verification until the TTL or the token's expiry, whichever is first."""
        cache_until = now + self._cache_ttl_seconds
        if access_token.expires_at is not None:
            cache_until = min(cache_until, access_token.expires_at)
        if len(self._cache) >= _MAX_CACHE_ENTRIES:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if len(self._cache) >= _MAX_CACHE_ENTRIES:
                self._cache.clear()
        self._cache[key] = (cache_until, access_token)


def bearer_token_from_headers(headers: Mapping[str, str] | None) -> str:
    """Extract the bearer token from the current HTTP request's headers.

    The auth middleware has already verified this token before any tool runs;
    reading it from this request's headers (rather than from the session) means
    a client that refreshes its token mid-session is always served with the
    token it most recently sent.
    """
    if headers:
        for name, value in headers.items():
            if name.lower() == "authorization" and value[:7].lower() == "bearer ":
                token = value[7:].strip()
                if token:
                    return token
    raise ToolError("No Google access token on this request: send 'Authorization: Bearer <token>'.")
