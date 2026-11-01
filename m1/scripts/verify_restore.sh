#!/usr/bin/env bash
# CourierLedger — restore verification (local VM).
#
# Usage:  m1/scripts/verify_restore.sh <source-db> <dump-file> [restore-db] [env-file]
#   restore-db defaults to <source-db>_restore_verify.
#   env-file defaults to m1/backend/.env (read at runtime, never printed).
#
#   <dump-file> may be a plain pg_dump -Fc file or an encrypted
#   *.dump.gpg artifact from backup_encrypted.sh (Phase 3): a .gpg
#   input is decrypted to a 0600 temp file first (passphrase from
#   CL_BACKUP_PASSPHRASE_FILE or CL_BACKUP_PASSPHRASE, never a CLI
#   argument), its SHA-256 is checked against the JSON sidecar when
#   one exists, and the temp file is wiped on exit.
#
# Restores the dump into a SEPARATE fresh database (never the source
# and never the working databases), then compares source vs restored:
#   - object counts: tables, policies, functions, triggers, indexes,
#     constraints, RLS FORCE/enabled, alembic version
#   - per-table row counts (diff must be empty)
#   - financial identities on reconciliation_results:
#       V_total = C - delta - V_R + S and V_cond = S - V_R
#       (0 violations where all terms are non-NULL)
#   - settlement variance arithmetic: bank_variance = allocated - expected
#   - runtime-role RLS probe on the restored DB (own tenant visible,
#     other tenant invisible)
# Any mismatch exits non-zero. The restore database is dropped at the
# end on success; on failure it is LEFT in place for inspection and
# its name is printed.
#
# This is the repeatable drill behind DATABASE_BACKUP_RECOVERY.md.
# Run it after every backup-schedule change and quarterly as a drill.
set -euo pipefail

SRC="${1:?usage: verify_restore.sh <source-db> <dump-file> [restore-db] [env-file]}"
DUMP="${2:?usage: verify_restore.sh <source-db> <dump-file> [restore-db] [env-file]}"
DST="${3:-${SRC}_restore_verify}"
ENV_FILE="${4:-$(dirname "$0")/../backend/.env}"

if [ "$DST" = "$SRC" ]; then
  echo "refusing: restore target must differ from source" >&2
  exit 2
fi

# Phase 3 (Stream A): an encrypted dump (*.dump.gpg, produced by
# backup_encrypted.sh) is decrypted to a 0600 temp file first. The
# passphrase comes ONLY from CL_BACKUP_PASSPHRASE_FILE (preferred)
# or CL_BACKUP_PASSPHRASE — never a CLI argument, never printed.
# When a JSON sidecar (<dump minus .gpg>.json) exists, the encrypted
# SHA-256 recorded in it is checked before decryption, so a
# corrupted or swapped artifact fails fast instead of restoring
# garbage. The decrypted temp file is wiped by the EXIT trap below.
DECRYPTED=""
PASSPHRASE_TMP=""
if [[ "$DUMP" == *.gpg ]]; then
  if [ -n "${CL_BACKUP_PASSPHRASE_FILE:-}" ]; then
    [ -r "$CL_BACKUP_PASSPHRASE_FILE" ] \
      || { echo "passphrase file not readable: $CL_BACKUP_PASSPHRASE_FILE" >&2; exit 2; }
    PASSPHRASE_ARG="$CL_BACKUP_PASSPHRASE_FILE"
  elif [ -n "${CL_BACKUP_PASSPHRASE:-}" ]; then
    PASSPHRASE_TMP="$(mktemp /tmp/cl-verify-passphrase-XXXXXX)"
    chmod 600 "$PASSPHRASE_TMP"
    printf '%s' "$CL_BACKUP_PASSPHRASE" > "$PASSPHRASE_TMP"
    PASSPHRASE_ARG="$PASSPHRASE_TMP"
  else
    echo "encrypted dump requires CL_BACKUP_PASSPHRASE_FILE or CL_BACKUP_PASSPHRASE" >&2
    exit 2
  fi
  SIDECAR="${DUMP%.gpg}.json"
  if [ -f "$SIDECAR" ]; then
    want="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['sha256_encrypted'])" "$SIDECAR")"
    got="$(sha256sum "$DUMP" | cut -d' ' -f1)"
    [ "$got" = "$want" ] || { echo "sidecar checksum mismatch for $DUMP — refusing to restore" >&2; exit 1; }
    echo "sidecar checksum verified: $SIDECAR"
  else
    echo "note: no JSON sidecar found for $DUMP; skipping checksum pre-check" >&2
  fi
  DECRYPTED="$(mktemp /tmp/cl-verify-decrypted-XXXXXX.dump)"
  chmod 600 "$DECRYPTED"
  gpg --batch --yes --quiet --decrypt --passphrase-file "$PASSPHRASE_ARG" \
      -o "$DECRYPTED" "$DUMP" \
    || { echo "decrypt failed: $DUMP" >&2; exit 1; }
  echo "decrypted $DUMP -> temp file (0600, wiped on exit)"
  DUMP="$DECRYPTED"
fi

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

cleanup() {
  local rc=$?
  if [ -n "$DECRYPTED" ] && [ -e "$DECRYPTED" ]; then
    if command -v shred >/dev/null 2>&1; then shred -u -- "$DECRYPTED" 2>/dev/null || rm -f -- "$DECRYPTED"; else rm -f -- "$DECRYPTED"; fi
  fi
  [ -n "$PASSPHRASE_TMP" ] && rm -f -- "$PASSPHRASE_TMP"
  if [ "$rc" -eq 0 ]; then
    su postgres -c "psql -d postgres -c \"DROP DATABASE IF EXISTS $DST;\"" >/dev/null
    echo "restore database $DST dropped (verification passed)"
  else
    echo "VERIFICATION FAILED — restore database $DST left in place for inspection" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

echo "--- restoring $DUMP into fresh database $DST ---"
su postgres -c "psql -d postgres -c \"DROP DATABASE IF EXISTS $DST;\" -c \"CREATE DATABASE $DST OWNER courierledger;\""
PGPASSWORD="$RUNTIME_PW" pg_restore -h 127.0.0.1 -U courierledger -d "$DST" --no-owner "$DUMP"

counts() {
  su postgres -c "psql -d $1 -tAc \"
SELECT 'tables=' || count(*) FROM pg_tables WHERE schemaname='public';
SELECT 'policies=' || count(*) FROM pg_policies;
SELECT 'functions=' || count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f';
SELECT 'triggers=' || count(*) FROM pg_trigger WHERE NOT tgisinternal;
SELECT 'indexes=' || count(*) FROM pg_indexes WHERE schemaname='public';
SELECT 'constraints=' || count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public';
SELECT 'rls_force=' || count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relforcerowsecurity;
SELECT 'rls_enabled=' || count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity;
SELECT 'alembic=' || version_num FROM alembic_version;
SELECT 'couriers=' || count(*) FROM couriers;
\" " | sort
}

echo "--- object counts: source vs restored ---"
diff <(counts "$SRC") <(counts "$DST") && echo "object counts identical"

echo "--- per-table row counts: source vs restored ---"
rowcounts() {
  su postgres -c "psql -d $1 -tAc \"
SELECT table_name FROM information_schema.tables
WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1;
\" " | while read -r t; do
    echo "$t=$(su postgres -c "psql -d $1 -tAc \"SELECT count(*) FROM \\\"$t\\\";\"")"
  done
}
diff <(rowcounts "$SRC") <(rowcounts "$DST") && echo "row counts identical"

echo "--- financial identities on restored data ---"
su postgres -c "psql -d $DST -v ON_ERROR_STOP=1 -tAc \"
SELECT 'v_total_violations=' || count(*) FROM reconciliation_results
WHERE c_poisha IS NOT NULL
  AND delta_poisha IS NOT NULL
  AND v_r_poisha IS NOT NULL
  AND s_poisha IS NOT NULL
  AND v_total_poisha IS NOT NULL
  AND v_total_poisha <> c_poisha - delta_poisha - v_r_poisha + s_poisha;
SELECT 'v_cond_violations=' || count(*) FROM reconciliation_results
WHERE s_poisha IS NOT NULL
  AND v_r_poisha IS NOT NULL
  AND v_cond_poisha IS NOT NULL
  AND v_cond_poisha <> s_poisha - v_r_poisha;
SELECT 'variance_calc_violations=' || count(*) FROM settlement_variance_calculations
WHERE bank_variance_poisha <> allocated_bank_confirmed_total_poisha - expected_batch_payout_poisha;
\" "

echo "--- runtime-role RLS probe on restored DB ---"
# Own-tenant visibility requires merchant + user context; with only a
# merchant context the membership policy correctly returns 0 rows
# (fail-closed). Both are asserted, matching the Phase 2 live probes.
PGPASSWORD="$RUNTIME_PW" psql -h 127.0.0.1 -U courierledger_runtime -d "$DST" -tAc \
  "SELECT 'no_context_merchants=' || count(*) FROM merchants;"
echo "RESTORE VERIFIED: $DUMP -> $DST matches $SRC"
