"""Shared live-PostgreSQL harness for M1 tests that must run against a
real server (B-1 workspace flows, B-3 lease clock, B-4 runtime role,
M4 CSV import). NOT a test module itself (no test_ prefix collection).

What it does, per use:
  1. Creates a scratch database (dropped first if it exists).
  2. Runs `alembic upgrade head` as the owner role (migrations).
  3. Applies app/db/rls.sql + triggers.sql as the owner role, and
     app/db/roles.sql as the postgres superuser (roles.sql creates
     roles; the workspace SQL files are root-owned 640, so roles.sql
     is copied to /tmp and chmod 644 first -- see the checklist).
  4. Starts uvicorn as a subprocess with DATABASE_URL for the role
     named by CL_LIVE_PG_ROLE (default: courierledger, the owner role;
     set to courierledger_runtime to prove the runtime-role path),
     waits for /api/health, and yields an httpx client + connection
     details. Tears the server down and drops the scratch database.

Environment (all read at fixture time, never committed):
  CL_LIVE_PG_PASSWORD   required; without it, live modules skip.
                         In this local dev environment the owner and
                         runtime roles share the documented dev-only
                         placeholder password (see m1/README.md).
  CL_LIVE_PG_HOST       default 127.0.0.1
  CL_LIVE_PG_ROLE       default courierledger
  CL_LIVE_PG_PORT       uvicorn port, default 8001 (never 8000, so a
                         developer's dev server is not disturbed)

The scratch database is named courierledger_live_test and is owned by
courierledger. Tests must use synthetic data only.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_NAME = "courierledger_live_test"
UVICORN_PORT = int(os.environ.get("CL_LIVE_PG_PORT", "8001"))


def live_pg_enabled() -> bool:
    return bool(os.environ.get("CL_LIVE_PG_PASSWORD"))


def _pg_password() -> str:
    password = os.environ.get("CL_LIVE_PG_PASSWORD")
    if not password:
        raise RuntimeError("CL_LIVE_PG_PASSWORD is not set; live-PG tests must skip.")
    return password


def pg_host() -> str:
    return os.environ.get("CL_LIVE_PG_HOST", "127.0.0.1")


def app_role() -> str:
    return os.environ.get("CL_LIVE_PG_ROLE", "courierledger")


def owner_dsn(db: str = DB_NAME) -> str:
    return f"postgresql+psycopg://courierledger:{_pg_password()}@{pg_host()}:5432/{db}"


def app_dsn(db: str = DB_NAME) -> str:
    return f"postgresql+psycopg://{app_role()}:{_pg_password()}@{pg_host()}:5432/{db}"


def _run(cmd: list[str], *, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    return proc


def _psql_as_owner(db: str, sql_file: Path) -> None:
    env = dict(os.environ, PGPASSWORD=_pg_password())
    _run(
        ["psql", "-h", pg_host(), "-U", "courierledger", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-q", "-f", str(sql_file)],
        env=env,
    )


def setup_scratch_db() -> None:
    """Create the scratch DB and apply the full schema + RLS + roles."""
    _run(["su", "postgres", "-c",
          f"psql -v ON_ERROR_STOP=1 -c 'DROP DATABASE IF EXISTS {DB_NAME};' "
          f"-c 'CREATE DATABASE {DB_NAME} OWNER courierledger;'"])
    env = dict(os.environ, DATABASE_URL=owner_dsn())
    _run([".venv/bin/alembic", "upgrade", "head"], env=env, cwd=BACKEND_DIR)
    _psql_as_owner(DB_NAME, BACKEND_DIR / "app" / "db" / "rls.sql")
    _psql_as_owner(DB_NAME, BACKEND_DIR / "app" / "db" / "triggers.sql")
    # roles.sql needs a superuser (it creates roles); the workspace copy
    # is root-owned 640, so copy it somewhere postgres can read first.
    shutil.copy(BACKEND_DIR / "app" / "db" / "roles.sql", "/tmp/live_roles.sql")
    os.chmod("/tmp/live_roles.sql", 0o644)
    _run(["su", "postgres", "-c",
          f"psql -d {DB_NAME} -v ON_ERROR_STOP=1 -q -f /tmp/live_roles.sql"])
    seed = BACKEND_DIR / "app" / "db" / "seed_couriers.sql"
    if seed.exists():
        _psql_as_owner(DB_NAME, seed)


def drop_scratch_db() -> None:
    _run(["su", "postgres", "-c",
          f"psql -v ON_ERROR_STOP=1 -c 'DROP DATABASE IF EXISTS {DB_NAME};'"])


@contextmanager
def live_pg_server():
    """Set up the scratch DB, start the app against it, yield a client.

    Yields dict(base_url, client, db_name, app_role, owner_dsn, app_dsn).
    The httpx client is a fresh, cookie-persisting client; callers that
    need a second user must build a second httpx.Client with the same
    base_url (cookies are per-client).
    """
    setup_scratch_db()
    env = dict(os.environ)
    env["DATABASE_URL"] = app_dsn()
    env["ENVIRONMENT"] = "development"
    env["CREATE_TABLES_ON_STARTUP"] = "false"
    env["SECRET_KEY"] = secrets.token_urlsafe(48)
    env["SESSION_SECRET"] = secrets.token_urlsafe(48)
    log_path = Path("/tmp/cl_live_uvicorn.log")
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        [".venv/bin/uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(UVICORN_PORT)],
        cwd=BACKEND_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{UVICORN_PORT}"
    try:
        deadline = time.time() + 60
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as probe:
            while True:
                try:
                    resp = probe.get("/api/health")
                    if resp.status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                if time.time() > deadline or proc.poll() is not None:
                    tail = log_path.read_text()[-2000:] if log_path.exists() else ""
                    raise RuntimeError(f"uvicorn did not become healthy in time\n{tail}")
                time.sleep(0.5)
        with httpx.Client(base_url=base_url, timeout=10.0, trust_env=False) as client:
            yield {
                "base_url": base_url,
                "client": client,
                "db_name": DB_NAME,
                "app_role": app_role(),
                "owner_dsn": owner_dsn(),
                "app_dsn": app_dsn(),
            }
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log_file.close()
        drop_scratch_db()


# --- HTTP helpers (mirror backend/tests/conftest.py, over httpx) ---

def get_csrf(client: httpx.Client) -> str:
    resp = client.get("/api/auth/csrf")
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


def post(client: httpx.Client, path: str, payload: dict, csrf: bool = True):
    headers = {}
    if csrf:
        headers["X-CSRF-Token"] = get_csrf(client)
    return client.post(path, json=payload, headers=headers)


def register(client: httpx.Client, email: str, password: str = "correct-horse-battery-1"):
    resp = post(client, "/api/auth/register",
                {"email": email, "password": password, "display_name": "Live Test User"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def login(client: httpx.Client, email: str, password: str = "correct-horse-battery-1"):
    resp = post(client, "/api/auth/login", {"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def authed_client(base_url: str, email: str, password: str = "correct-horse-battery-1") -> httpx.Client:
    """A fresh cookie-persisting client, registered + logged in."""
    client = httpx.Client(base_url=base_url, timeout=10.0, trust_env=False)
    register(client, email, password)
    login(client, email, password)
    return client
