"""Tests for environment-based configuration."""

import pytest

from gtasks_mcp.config import ConfigError, Settings


def test_from_env_defaults():
    settings = Settings.from_env({"GTASKS_MCP_PUBLIC_URL": "https://tasks.example.com/mcp"})

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.allowed_client_ids == frozenset()
    assert settings.mcp_path == "/mcp"
    assert settings.resource_url == "https://tasks.example.com/mcp"
    assert settings.public_host == "tasks.example.com"
    assert settings.public_origin == "https://tasks.example.com"


def test_from_env_all_values():
    settings = Settings.from_env(
        {
            "GTASKS_MCP_PUBLIC_URL": "https://example.com:8443/gtasks/mcp/",
            "GTASKS_MCP_HOST": "0.0.0.0",
            "GTASKS_MCP_PORT": "9000",
            "GTASKS_MCP_ALLOWED_CLIENT_IDS": "a.apps.googleusercontent.com, b ,",
            "GTASKS_MCP_ALLOWED_ORIGINS": "https://confer.to",
            "GTASKS_MCP_LOG_LEVEL": "debug",
        }
    )

    assert settings.port == 9000
    assert settings.allowed_client_ids == {"a.apps.googleusercontent.com", "b"}
    assert settings.allowed_origins == {"https://confer.to"}
    assert settings.log_level == "DEBUG"
    assert settings.mcp_path == "/gtasks/mcp"
    assert settings.resource_url == "https://example.com:8443/gtasks/mcp"
    assert settings.public_host == "example.com:8443"


def test_bare_host_url_serves_default_path():
    assert Settings(public_url="https://tasks.example.com").mcp_path == "/mcp"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"GTASKS_MCP_PUBLIC_URL": "tasks.example.com/mcp"},
        {"GTASKS_MCP_PUBLIC_URL": "https://tasks.example.com/mcp", "GTASKS_MCP_PORT": "eighty"},
    ],
)
def test_invalid_config(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)
