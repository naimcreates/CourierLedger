#!/usr/bin/env python3
"""CourierLedger — scheduled maintenance runner (Phase 3, Stream A).

Usage:
  m1/backend/.venv/bin/python m1/scripts/maintenance.py [--db <name> ...]
      [--env-file <path>] [--dry-run]

Runs the approved maintenance job (see
``m1/backend/app/services/maintenance_service.py`` for scope — the
only deletion is expired user_sessions + expired/used
password_reset_tokens, B-F19) against each named database, as the
table-owner role (``courierledger``), never as the runtime role.
``--db`` may be repeated; the default is courierledger_dev only.

Credential handling — same pattern as ``provision_db.sh``:
the owner password is read at runtime from the env file
(``MIGRATION_DATABASE_URL`` first, else the ``DATABASE_URL``
password, which shares the local-dev value) and is NEVER printed,
logged, or written to disk by this script. The DSN is built in
memory only.

Output: exactly one JSON line on stdout per database per run, e.g.
  {"ts": "...", "job": "maintenance", "db": "courierledger_dev",
   "status": "completed", "sessions": 3, "reset_tokens": 1,
   "dry_run": false, "duration_ms": 42}
plus the service's own structured log lines. A ``skipped``
(lock held by another run) is exit 0 — overlap is not a failure.
Any error exits non-zero with a JSON error line, so a systemd
timer / cron failure is visible in the journal / mail, never
silent.

Exit codes: 0 ok (completed / dry_run / skipped for every DB),
1 usage error, 2 at least one DB run failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "m1" / "backend"
DEFAULT_ENV_FILE = BACKEND_DIR / ".env"

sys.path.insert(0, str(BACKEND_DIR))


def _read_env_file(path: Path) -> dict:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _owner_dsn(db: str, env_file: Path) -> str:
    """Owner-role DSN for ``db``. Password from the env file at
    runtime only; the DSN is never printed or logged."""
    env = _read_env_file(env_file)
    url = env.get("MIGRATION_DATABASE_URL") or env.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            f"no MIGRATION_DATABASE_URL or DATABASE_URL in {env_file}"
        )
    parsed = urllib.parse.urlparse(url)
    if not parsed.password:
        raise RuntimeError("owner credential has no password in env file")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    return (
        f"postgresql+psycopg://courierledger:"
        f"{urllib.parse.quote(parsed.password)}@{host}:{port}/{db}"
    )


def _log_line(**fields: object) -> None:
    line = {"ts": datetime.now(timezone.utc).isoformat(), "job": "maintenance"}
    line.update(fields)
    print(json.dumps(line), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", default=None,
                        dest="databases",
                        help="database to maintain; repeatable "
                             "(default: courierledger_dev)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                        help="env file holding the owner credential "
                             "(default: m1/backend/.env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="count what would be deleted; delete nothing")
    args = parser.parse_args(argv)
    databases = args.databases or ["courierledger_dev"]

    started = time.monotonic()
    try:
        dsns = {db: _owner_dsn(db, args.env_file) for db in databases}
    except (OSError, RuntimeError) as exc:
        _log_line(status="error", db=",".join(databases), error=str(exc),
                  duration_ms=int((time.monotonic() - started) * 1000))
        return 2

    # Imported only after the DSNs are resolved, so a credential
    # problem fails before the app modules are even loaded.
    # DATABASE_URL is set to an owner DSN so app.config (read at
    # import time) never falls back to a runtime/sqlite URL; the
    # job itself uses its own engine per database, not SessionLocal.
    import os

    os.environ["DATABASE_URL"] = next(iter(dsns.values()))
    os.environ.setdefault("ENVIRONMENT", "development")
    os.environ.setdefault("CREATE_TABLES_ON_STARTUP", "false")

    from sqlalchemy import create_engine  # noqa: E402
    from sqlalchemy.orm import sessionmaker  # noqa: E402

    from app.services import maintenance_service  # noqa: E402

    worst = 0
    for db_name, dsn in dsns.items():
        db_started = time.monotonic()
        engine = create_engine(dsn, pool_pre_ping=True)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                               expire_on_commit=False)
        try:
            with Session() as db:
                result = maintenance_service.run_maintenance(db, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - the JSON error line IS the report
            _log_line(status="error", db=db_name,
                      error=f"{type(exc).__name__}: {exc}",
                      duration_ms=int((time.monotonic() - db_started) * 1000))
            worst = 2
            continue
        finally:
            engine.dispose()
        _log_line(db=db_name,
                  duration_ms=int((time.monotonic() - db_started) * 1000), **result)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
