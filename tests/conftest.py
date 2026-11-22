"""Shared pytest fixtures.

Every test gets its own temporary SQLite file so runs never leak between
tests; TestClient is entered as a context manager to trigger app startup.
The autouse fixture resets the tool fault switch and counters so an injected
failure (or a ``timeout:2`` budget) can never leak into the next test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import tools
from app.main import create_app


@pytest.fixture(autouse=True)
def _clean_tool_state():
    tools.FAULTS.clear()
    tools.reset_attempts()
    yield
    tools.FAULTS.clear()
    tools.reset_attempts()


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client
