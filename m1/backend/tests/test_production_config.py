"""Production configuration tests (Phase 3, Stream C).

Two layers:
1. Unit tests of ``validate_production_settings`` — each failure must
   name the variable that has to be fixed.
2. Subprocess tests that import / run the real app in a child process
   with a controlled environment (no .env file in the child's cwd, so
   an unset variable falls back to the compiled-in default, which the
   validator refuses):
   - production import fails fast when a required variable is missing
     or a placeholder, and succeeds when the environment is complete;
   - a production server started against an unreachable database
     still serves /api/health (200, database_ok false) while
     /api/ready is 503 and /docs stays 404, and shuts down cleanly
     on SIGTERM (uvicorn logs "Application shutdown complete" and
     then re-raises SIGTERM, so the exit status is -15 — that is
     uvicorn's documented behaviour, not a crash);
   - a production server against the live dev database (only when
     CL_LIVE_PG_PASSWORD is set, mirroring the live-PG harness)
     serves no docs, sends HSTS + Secure cookies, reports ready,
     and does not change the Alembic revision (migrations never run
     on app startup).

No test here writes to a database: the live check only performs GETs
against the working dev database (courierledger_app).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg
import pytest

from app.config import Settings, validate_production_settings
from tests.live_pg import live_pg_enabled, pg_host

BACKEND_DIR = Path(__file__).resolve().parent.parent

# A complete, valid production environment (values are placeholders
# for the *shape* — the DATABASE_URL points at a dead port on purpose
# so the import/startup tests never touch a real database).
GOOD_ENV = {
    "ENVIRONMENT": "production",
    "DATABASE_URL": "postgresql+psycopg://courierledger_runtime:CHANGE_ME_NOT_USED@127.0.0.1:5999/courierledger_app",
    "SECRET_KEY": "test-secret-key-that-is-long-enough-for-tests-0123456789",
    "SESSION_SECRET": "test-session-secret-that-is-long-enough-for-tests-0123456789",
    "FRONTEND_URL": "https://app.example.invalid",
    "SESSION_COOKIE_SECURE": "true",
    "CREATE_TABLES_ON_STARTUP": "false",
}

PLACEHOLDER = "CHANGE_ME_REPLACE_WITH_A_LONG_RANDOM_SECRET"


def _good_settings(**overrides):
    kwargs = dict(
        database_url=GOOD_ENV["DATABASE_URL"].replace("CHANGE_ME_NOT_USED", "x" * 16),
        secret_key=GOOD_ENV["SECRET_KEY"],
        session_secret=GOOD_ENV["SESSION_SECRET"],
        environment="production",
        frontend_url=GOOD_ENV["FRONTEND_URL"],
        session_cookie_secure=True,
        create_tables_on_startup=False,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


# --- 1. Unit tests of the validator --------------------------------
# The validator is value-based (a value may come from the process
# environment or a .env file — what matters is that it is not a
# development default or a placeholder), so these tests build Settings
# objects with the offending value and assert the error names it.


def test_validate_production_happy_path():
    validate_production_settings(_good_settings())  # must not raise


def test_validate_production_placeholder_database_password_named():
    # An unset DATABASE_URL falls back to the compiled-in default,
    # which contains CHANGE_ME — the same check catches both.
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_production_settings(_good_settings(
            database_url="postgresql+psycopg://courierledger_runtime:CHANGE_ME@127.0.0.1:5432/courierledger"
        ))


def test_validate_production_sqlite_url_rejected():
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_production_settings(_good_settings(
            database_url="sqlite:////tmp/never-prod.db"
        ))


@pytest.mark.parametrize("variable", ["secret_key", "session_secret"])
def test_validate_production_placeholder_secret_named(variable):
    with pytest.raises(RuntimeError, match=variable.upper()):
        validate_production_settings(_good_settings(**{variable: PLACEHOLDER}))


def test_validate_production_dev_default_frontend_url_named():
    with pytest.raises(RuntimeError, match="FRONTEND_URL"):
        validate_production_settings(_good_settings(
            frontend_url="http://127.0.0.1:5173"
        ))


def test_validate_production_insecure_cookie_named():
    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        validate_production_settings(_good_settings(session_cookie_secure=False))


def test_validate_production_create_tables_named():
    with pytest.raises(RuntimeError, match="CREATE_TABLES_ON_STARTUP"):
        validate_production_settings(_good_settings(create_tables_on_startup=True))


# --- 2. Subprocess import tests (real app, controlled environment) ---


def _child_env(tmp_path, **overrides):
    """Minimal child environment: only PATH/HOME plus the case vars.

    The child runs with cwd=tmp_path (no .env file), so a variable
    that is absent here falls back to the compiled-in default — which
    is exactly what the production validator refuses.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(GOOD_ENV)
    env["DATABASE_URL"] = env["DATABASE_URL"].replace("CHANGE_ME_NOT_USED", "x" * 16)
    env.update(overrides)
    return env


def _import_app(tmp_path, **overrides):
    env = _child_env(tmp_path, **overrides)
    for name in list(overrides):
        if overrides[name] is None:
            env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )


def test_production_import_succeeds_with_complete_environment(tmp_path):
    proc = _import_app(tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_production_import_fails_without_database_url(tmp_path):
    proc = _import_app(tmp_path, DATABASE_URL=None)
    assert proc.returncode != 0
    assert "DATABASE_URL" in proc.stderr


def test_production_import_fails_with_placeholder_secret(tmp_path):
    proc = _import_app(tmp_path, SECRET_KEY=PLACEHOLDER)
    assert proc.returncode != 0
    assert "SECRET_KEY" in proc.stderr


def test_production_import_fails_with_insecure_cookie(tmp_path):
    proc = _import_app(tmp_path, SESSION_COOKIE_SECURE="false")
    assert proc.returncode != 0
    assert "SESSION_COOKIE_SECURE" in proc.stderr


def test_development_import_does_not_require_production_environment(tmp_path):
    # Same child, but ENVIRONMENT=development and no production vars:
    # the validator must not run, so the import succeeds.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite:////tmp/cl_prod_cfg_dev.db",
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


# --- 3. Real server: production mode, database unreachable ----------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_uvicorn(env, port, log_path):
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc, log


def _wait_for_health(port, timeout=30):
    deadline = time.time() + timeout
    with httpx.Client(timeout=5.0, trust_env=False) as client:
        while time.time() < deadline:
            try:
                if client.get(f"http://127.0.0.1:{port}/api/health").status_code == 200:
                    return True
            except httpx.ConnectError:
                pass
            time.sleep(0.5)
    return False


def _stop_gracefully(proc, log, log_path):
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        rc = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait(timeout=10)
    log.close()
    return rc, Path(log_path).read_text()


def test_production_server_db_down_ready_503_health_200_docs_404(tmp_path):
    port = _free_port()
    env = dict(os.environ)
    env.update(GOOD_ENV)
    env["DATABASE_URL"] = env["DATABASE_URL"].replace("CHANGE_ME_NOT_USED", "x" * 16)
    env["CREATE_TABLES_ON_STARTUP"] = "false"
    log_path = tmp_path / "uvicorn-db-down.log"
    proc, log = _start_uvicorn(env, port, log_path)
    try:
        assert _wait_for_health(port), "uvicorn did not start"
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            health = client.get(f"http://127.0.0.1:{port}/api/health")
            assert health.status_code == 200
            assert health.json()["database_ok"] is False
            assert health.json()["database"] == "unavailable"
            ready = client.get(f"http://127.0.0.1:{port}/api/ready")
            assert ready.status_code == 503
            assert ready.json()["status"] == "not_ready"
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert client.get(f"http://127.0.0.1:{port}{path}").status_code == 404
    finally:
        rc, text = _stop_gracefully(proc, log, log_path)
        # uvicorn re-raises SIGTERM after a graceful shutdown, so the
        # exit status is -15; the graceful part is what this asserts.
        assert rc in (0, -signal.SIGTERM)
        assert "Application shutdown complete." in text


# --- 4. Real server: production mode against the live dev database --

@pytest.mark.skipif(not live_pg_enabled(), reason="needs CL_LIVE_PG_PASSWORD")
def test_production_server_live_db_docs_disabled_hsts_secure_cookie(tmp_path):
    port = _free_port()
    password = os.environ["CL_LIVE_PG_PASSWORD"]
    env = dict(os.environ)
    env.update({
        "ENVIRONMENT": "production",
        "DATABASE_URL": f"postgresql+psycopg://courierledger_runtime:{password}@{pg_host()}:5432/courierledger_app",
        "SECRET_KEY": GOOD_ENV["SECRET_KEY"],
        "SESSION_SECRET": GOOD_ENV["SESSION_SECRET"],
        # Production validation refuses the development-default
        # FRONTEND_URL, so the live probe uses an explicit origin.
        "FRONTEND_URL": "https://app.example.invalid",
        "SESSION_COOKIE_SECURE": "true",
        "CREATE_TABLES_ON_STARTUP": "false",
    })

    def _owner_conn():
        return psycopg.connect(
            f"host={pg_host()} dbname=courierledger_app "
            f"user=courierledger password={password}"
        )

    with _owner_conn() as conn:
        before = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    log_path = tmp_path / "uvicorn-prod-live.log"
    proc, log = _start_uvicorn(env, port, log_path)
    try:
        assert _wait_for_health(port), "uvicorn did not start"
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert client.get(f"http://127.0.0.1:{port}{path}").status_code == 404
            # The server is up when /api/health first answers, but the
            # first database round-trip can transiently fail while the
            # pool warms up (or while another stream's live work
            # contends for the shared local cluster) — poll briefly
            # for the steady state instead of asserting one read.
            deadline = time.time() + 15
            while True:
                health = client.get(f"http://127.0.0.1:{port}/api/health")
                assert health.status_code == 200
                if health.json().get("database_ok") is True:
                    break
                if time.time() > deadline:
                    raise AssertionError(
                        f"database never became ok: {health.json()}"
                    )
                time.sleep(0.5)
            assert health.json()["database"] == "postgresql"
            ready = client.get(f"http://127.0.0.1:{port}/api/ready")
            assert ready.status_code == 200
            assert ready.json()["status"] == "ready"
            assert health.headers.get("strict-transport-security", "").startswith("max-age=31536000")
            assert "x-request-id" in ready.headers
            csrf = client.get(f"http://127.0.0.1:{port}/api/auth/csrf")
            assert csrf.status_code == 200
            set_cookie = csrf.headers.get("set-cookie", "")
            assert "Secure" in set_cookie, set_cookie
            assert "SameSite=lax" in set_cookie
            assert "HttpOnly" not in set_cookie  # CSRF cookie must be JS-readable
    finally:
        rc, text = _stop_gracefully(proc, log, log_path)
        assert rc in (0, -signal.SIGTERM)
        assert "Application shutdown complete." in text

    with _owner_conn() as conn:
        after = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    # The property under test is "unchanged across app startup", not
    # the revision itself — another stream may land a migration.
    assert after == before, "app startup must not run migrations"
