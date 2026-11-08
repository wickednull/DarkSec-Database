"""Shared pytest fixtures.

Every test run uses a throwaway data directory so the operator's real
``fielddata/`` tree is never touched. The environment has to be set before
``darksec.config`` is imported, because paths are resolved at import time.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DATA = Path(tempfile.mkdtemp(prefix="darksec-pytest-"))
os.environ["DARKSEC_DATA"] = str(TEST_DATA)
os.environ["DARKSEC_NO_SYNC"] = "1"
os.environ["DARKSEC_NO_INDEX"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from darksec.server import COOKIE, create_app  # noqa: E402


@pytest.fixture(scope="session")
def app():
    return create_app(token="test-token", no_index=True)


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.cookies.set(COOKIE, "test-token")
        test_client.headers["X-DarkSec-CSRF"] = app.state.csrf
        yield test_client


@pytest.fixture()
def anon(app):
    """An unauthenticated client, for auth/CSRF checks."""
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
