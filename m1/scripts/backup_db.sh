#!/usr/bin/env bash
# CourierLedger — logical database backup (local VM).
#
# Usage:  m1/scripts/backup_db.sh <db-name> [output-dir] [env-file]
#   output-dir defaults to ./backups under the repo's m1/ directory.
#   env-file defaults to m1/backend/.env (read at runtime, never printed).
#
# Writes <output-dir>/<db>-<YYYY-MM-DD-HHMM>.dump plus a .sha256 file.
# Format: pg_dump -Fc (custom format, compressed).
#
# WHY SUPERUSER: the dump runs as the postgres superuser, NOT as the
# table owner. FORCE RLS applies to the table owner, and pg_dump sets
# no tenant context, so an owner-role dump fails with:
#   pg_dump: error: query failed: ERROR: query would be affected by
#   row-level security policy for table "audit_logs"
# The backup credential must therefore bypass RLS — superuser here;
# in production, a dedicated backup role with BYPASSRLS used ONLY for
# dumps (a documented exception to the no-bypass rule), never the
# runtime role and never the app's DATABASE_URL credential.
#
# The dump contains password/token hashes (users.password_hash,
# user_sessions.token_hash) — treat dumps as credential-bearing
# material: encrypted at rest, readable only by the backup operator,
# never committed to git (see DATABASE_BACKUP_RECOVERY.md).
# This script backs up ONE database only. Cluster-level objects
# (roles, passwords, pg_hba.conf, postgresql.conf) are NOT included;
# see DATABASE_BACKUP_RECOVERY.md for the pg_dumpall --globals step.
set -euo pipefail

DB="${1:?usage: backup_db.sh <db-name> [output-dir] [env-file]}"
OUT_DIR="${2:-$(dirname "$0")/../backups}"
ENV_FILE="${3:-$(dirname "$0")/../backend/.env}"
mkdir -p "$OUT_DIR"

STAMP="$(date +%F-%H%M)"
FILE="$OUT_DIR/$DB-$STAMP.dump"
# pg_dump runs as the postgres OS user, which cannot write into the
# repo tree — stage the dump in /tmp (created by the postgres user
# so it can write it) and move it into place afterwards.
STAGE="$(su postgres -c "mktemp /tmp/${DB}-backup-XXXXXX.dump")"
su postgres -c "pg_dump -d $DB -Fc -f $STAGE"
mv "$STAGE" "$FILE"
sha256sum "$FILE" > "$FILE.sha256"
echo "backup: $FILE"
echo "sha256: $(cut -d' ' -f1 "$FILE.sha256")"
ls -l "$FILE"
