#!/usr/bin/env bash
# CourierLedger — provision one database from scratch (local VM).
#
# Usage:  m1/scripts/provision_db.sh <db-name> [env-file]
#   env-file defaults to m1/backend/.env (gitignored; passwords are
#   read from it at runtime and never printed).
#
# Encodes the fresh-cluster provisioning order proven in Phase 1.1
# re-verification and Phase 2 Test D:
#   1. create the NOLOGIN roles (inline DO block — roles.sql itself
#      cannot run first: it fails on the missing
#      app_current_merchant_id() function)
#   2. Alembic upgrade head (live migrator role: courierledger — the
#      LOGIN owner. Phase 3 decision: courierledger_migrator stays
#      reserved/NOLOGIN; see app/db/roles.sql header)
#   3. rls.sql + triggers.sql (owner role)
#   4. roles.sql (postgres superuser — also nulls the password
#      verifiers on the two NOLOGIN roles; Phase 3 decision)
#   5. runtime-role password (roles.sql deliberately sets none)
#   6. seed_couriers.sql (owner role)
#   7. verification: alembic head, object counts, seed rows
#
# Running rls.sql first fails with: role "courierledger_maintenance"
# does not exist. Running roles.sql first fails with: function
# public.app_current_merchant_id() does not exist. Do not reorder.
#
# This script targets the local apt-installed PostgreSQL cluster
# (peer auth via `su postgres`). On any other host, the superuser
# path differs — adapt the `su postgres` calls, not the order.
# The script is idempotent for re-provisioning an existing database:
# roles are created IF NOT EXISTS, Alembic is a no-op at head, the
# SQL files are written idempotently, and the seed is idempotent.
# It never drops a database. Never run it against a database holding
# data you cannot lose without a backup first.
set -euo pipefail

DB="${1:?usage: provision_db.sh <db-name> [env-file]}"
ENV_FILE="${2:-$(dirname "$0")/../backend/.env}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BACKEND="$REPO_ROOT/m1/backend"

# Passwords from .env at runtime only. Both roles share the local-dev
# password: prefer MIGRATION_DATABASE_URL (owner), fall back to
# DATABASE_URL (runtime) when it is absent.
OWNER_PW="$(python3 - "$ENV_FILE" <<'EOF'
import sys, urllib.parse
env = {}
for line in open(sys.argv[1]):
    if '=' in line and not line.lstrip().startswith('#'):
        k, v = line.strip().split('=', 1)
        env[k] = v
url = env.get('MIGRATION_DATABASE_URL') or env['DATABASE_URL']
print(urllib.parse.urlparse(url).password)
EOF
)"
export PGPASSWORD="$OWNER_PW"

psql_super() {
  # Run psql as the postgres OS user (peer auth). Arguments are
  # re-quoted with %q so SQL containing spaces survives the extra
  # shell layer intact. Usage: psql_super -d <db> -c "<sql>" / -f <file>.
  su postgres -c "psql -v ON_ERROR_STOP=1 $(printf '%q ' "$@")"
}

# 1. Database + roles first. The NOLOGIN roles AND the runtime role
# must exist before rls.sql runs: rls.sql grants EXECUTE on the
# context helpers to courierledger_runtime (line ~186) and to
# courierledger_maintenance, so on a brand-new cluster rls.sql
# fails with `role "courierledger_runtime" does not exist` if the
# runtime role is only created later by roles.sql (Phase 3, found
# live on a fresh cluster after a container replacement). The
# runtime role is created here LOGIN NOBYPASSRLS but WITHOUT a
# password (it cannot authenticate until step 5 sets one from
# .env); roles.sql (step 4) re-asserts the role idempotently.
if ! psql_super -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB'" | grep -qx 1; then
  psql_super -d postgres -c "CREATE DATABASE $DB OWNER courierledger;"
fi
psql_super -d postgres <<'EOSQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'courierledger_migrator') THEN
    CREATE ROLE courierledger_migrator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'courierledger_maintenance') THEN
    CREATE ROLE courierledger_maintenance NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'courierledger_runtime') THEN
    CREATE ROLE courierledger_runtime LOGIN NOBYPASSRLS;
  END IF;
END
$$;
EOSQL

# 2. Alembic as the live migrator role (courierledger, the LOGIN
# owner). Alembic resolves its script location
# relative to the working directory, so run it from m1/backend.
(
  cd "$BACKEND"
  MIGRATION_DATABASE_URL="postgresql+psycopg://courierledger:${OWNER_PW}@127.0.0.1:5432/$DB" \
    .venv/bin/alembic upgrade head
)

# 3. RLS + triggers as the owner role.
psql -h 127.0.0.1 -U courierledger -d "$DB" -v ON_ERROR_STOP=1 -f "$BACKEND/app/db/rls.sql"
psql -h 127.0.0.1 -U courierledger -d "$DB" -v ON_ERROR_STOP=1 -f "$BACKEND/app/db/triggers.sql"

# 4. roles.sql as the superuser. The postgres OS user must be able
# to read the file (root's umask makes the repo copy unreadable),
# so copy it to /tmp first — the same pattern ci-selfhosted.yml uses.
cp "$BACKEND/app/db/roles.sql" /tmp/provision_roles.sql
chmod 644 /tmp/provision_roles.sql
psql_super -d "$DB" -f /tmp/provision_roles.sql
rm -f /tmp/provision_roles.sql

# 5. Runtime-role password (roles.sql deliberately sets none).
RUNTIME_PW="$(python3 - "$ENV_FILE" <<'EOF'
import sys, urllib.parse
env = {}
for line in open(sys.argv[1]):
    if '=' in line and not line.lstrip().startswith('#'):
        k, v = line.strip().split('=', 1)
        env[k] = v
print(urllib.parse.urlparse(env['DATABASE_URL']).password)
EOF
)"
psql_super -d postgres -c "ALTER ROLE courierledger_runtime WITH PASSWORD '$RUNTIME_PW';"

# 6. Seed the 5 courier codes (idempotent; no rates/templates).
psql -h 127.0.0.1 -U courierledger -d "$DB" -v ON_ERROR_STOP=1 -f "$BACKEND/app/db/seed_couriers.sql"

# 7. Verification (expected: 0007, 33 tables, 46 policies, 15
#    functions, 22 triggers, 5 couriers).
echo "--- verification: $DB ---"
(
  cd "$BACKEND"
  MIGRATION_DATABASE_URL="postgresql+psycopg://courierledger:${OWNER_PW}@127.0.0.1:5432/$DB" \
    .venv/bin/alembic current
)
psql_super -d "$DB" -tAc "
SELECT 'tables=' || count(*) FROM pg_tables WHERE schemaname='public';
SELECT 'policies=' || count(*) FROM pg_policies;
SELECT 'functions=' || count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='public' AND p.prokind='f';
SELECT 'triggers=' || count(*) FROM pg_trigger WHERE NOT tgisinternal;
SELECT 'couriers=' || count(*) FROM couriers;
"
echo "provisioned: $DB"
