"""Alembic environment for CourierLedger M1.

- Target metadata is ``app.db.models.Base.metadata`` (all 32 M1 tables).
- Database URL: ``MIGRATION_DATABASE_URL`` env var wins (owner role --
  the application connects as the runtime role and cannot run DDL,
  B-4 fix); otherwise ``DATABASE_URL``; otherwise the URL in
  ``alembic.ini``. PostgreSQL only.
- Offline mode (``alembic upgrade head --sql``) renders DDL without a
  live server; online mode connects as the migrator role (never the
  runtime role) and runs the migration.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Allow `from app.db.models import Base` when alembic runs from backend/.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return (
        os.environ.get("MIGRATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL generation, no connection)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live PostgreSQL, migrator role)."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
