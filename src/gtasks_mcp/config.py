"""Server configuration, read from environment variables.

None of these settings are secret. The one secret the server can use, the
Google OAuth client for the built-in sign-in broker, is read from a separate
file (see `google_client_file`), normally a root-only systemd credential.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_PREFIX = "GTASKS_MCP_"


class ConfigError(ValueError):
    """Raised when the environment does not describe a usable configuration."""


def _split_list(value: str | None) -> frozenset[str]:
    """Parse a comma-separated environment value into a set of non-empty entries."""
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the gtasks-mcp HTTP server."""

    public_url: str
    """Externally visible URL of the MCP endpoint, e.g. 'https://tasks.example.com/mcp'."""

    host: str = "127.0.0.1"
    port: int = 8000
    allowed_client_ids: frozenset[str] = field(default_factory=frozenset)
    """Google OAuth client IDs whose tokens are accepted; empty accepts any client."""

    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    """Extra browser Origin values to accept, beyond the public URL's own origin."""

    log_level: str = "INFO"

    google_client_file: Path | None = None
    """Google OAuth web client JSON. When set, this server is also the OAuth
    authorization server MCP clients register with, signing users in with
    Google; when unset, clients must obtain Google tokens themselves."""

    def __post_init__(self) -> None:
        """Validate the public URL so misconfiguration fails at startup, not on first request."""
        parsed = urlparse(self.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(f"{_PREFIX}PUBLIC_URL must be an absolute http(s) URL, got {self.public_url!r}")

    @property
    def mcp_path(self) -> str:
        """Path the MCP endpoint is served on, taken from the public URL (default '/mcp')."""
        path = urlparse(self.public_url).path.rstrip("/")
        return path or "/mcp"

    @property
    def resource_url(self) -> str:
        """The public URL of the MCP endpoint, used as the OAuth protected-resource identifier."""
        parsed = urlparse(self.public_url)
        return f"{parsed.scheme}://{parsed.netloc}{self.mcp_path}"

    @property
    def public_host(self) -> str:
        """The host[:port] clients use to reach this server, for Host-header validation."""
        return urlparse(self.public_url).netloc

    @property
    def public_origin(self) -> str:
        """The scheme://host[:port] origin of the public URL."""
        parsed = urlparse(self.public_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from GTASKS_MCP_* environment variables."""
        env = os.environ if env is None else env
        public_url = env.get(f"{_PREFIX}PUBLIC_URL")
        if not public_url:
            raise ConfigError(
                f"{_PREFIX}PUBLIC_URL is required: the externally visible URL of the MCP endpoint, "
                "e.g. https://tasks.example.com/mcp"
            )
        port_text = env.get(f"{_PREFIX}PORT", "8000")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ConfigError(f"{_PREFIX}PORT must be an integer, got {port_text!r}") from exc
        return cls(
            public_url=public_url,
            host=env.get(f"{_PREFIX}HOST", "127.0.0.1"),
            port=port,
            allowed_client_ids=_split_list(env.get(f"{_PREFIX}ALLOWED_CLIENT_IDS")),
            allowed_origins=_split_list(env.get(f"{_PREFIX}ALLOWED_ORIGINS")),
            log_level=env.get(f"{_PREFIX}LOG_LEVEL", "INFO").upper(),
            google_client_file=_google_client_file(env),
        )


_CREDENTIAL_NAME = "google-client.json"


def _google_client_file(env: Mapping[str, str]) -> Path | None:
    """GTASKS_MCP_GOOGLE_CLIENT_FILE, else the systemd credential of that name if loaded."""
    if explicit := env.get(f"{_PREFIX}GOOGLE_CLIENT_FILE"):
        return Path(explicit)
    if credentials_dir := env.get("CREDENTIALS_DIRECTORY"):
        candidate = Path(credentials_dir) / _CREDENTIAL_NAME
        if candidate.exists():
            return candidate
    return None
