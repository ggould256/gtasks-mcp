"""Shared pytest configuration."""

import pytest


@pytest.fixture
def anyio_backend():
    """Run async tests on asyncio only."""
    return "asyncio"
