"""Pytest configuration for the M1 backend.

TEST-ONLY SQLite fallback: PostgreSQL is the M1 target, but no server
is available in this environment yet, so the suite runs against a
temporary SQLite file. This fallback is test-only — it exercises the
auth/workspace logic, not the production data model (see README.md).
The DATABASE_URL must be set BEFORE importing the app, because the
engine is created at import time.
"""

import os

os.environ["DATABASE_URL"] = "sqlite:////tmp/cl_m1_test.db"
os.environ["ENVIRONMENT"] = "test"
os.environ["CREATE_TABLES_ON_STARTUP"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db.auth_models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import rate_limit  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    rate_limit.reset_for_tests()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client():
    return TestClient(app)


def get_csrf(client: TestClient) -> str:
    resp = client.get("/api/auth/csrf")
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


def post(client: TestClient, path: str, payload: dict, csrf: bool = True):
    headers = {}
    if csrf:
        headers["X-CSRF-Token"] = get_csrf(client)
    return client.post(path, json=payload, headers=headers)


def register(client: TestClient, email: str, password: str = "correct-horse-battery-1", display_name: str = "Test User"):
    resp = post(client, "/api/auth/register", {"email": email, "password": password, "display_name": display_name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def login(client: TestClient, email: str, password: str = "correct-horse-battery-1"):
    resp = post(client, "/api/auth/login", {"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def authed_client(client: TestClient, email: str, password: str = "correct-horse-battery-1") -> TestClient:
    register(client, email, password)
    login(client, email, password)
    return client
