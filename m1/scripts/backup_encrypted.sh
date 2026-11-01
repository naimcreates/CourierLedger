#!/usr/bin/env bash
# CourierLedger — encrypted, schedulable database backup (Phase 3, Stream A).
#
# Usage:
#   m1/scripts/backup_encrypted.sh <db-name> [output-dir] [env-file]
#   m1/scripts/backup_encrypted.sh --check [output-dir] [db-name]
#   m1/scripts/backup_encrypted.sh --dry-run <db-name> [output-dir]
#
# What one run does (see DATABASE_BACKUP_RECOVERY.md for the design):
#   1. pg_dump -Fc of <db-name> as the postgres superuser (required:
#      an owner-role dump fails under FORCE RLS on audit_logs — see
#      DATABASE_BACKUP_RECOVERY.md §2). The dump is streamed through
#      a pipe into gpg, so no plaintext dump file ever sits on disk.
#   2. pg_dumpall --globals (roles only — the cluster-level objects a
#      per-database dump does not contain) encrypted the same way.
#   3. Encryption: gpg symmetric AES-256. Passphrase ONLY from
#      CL_BACKUP_PASSPHRASE_FILE (a 0600 file outside the repo, e.g.
#      /home/hatch/.config/courierledger/backup.passphrase — under
#      the home directory, NOT /etc, because this VM's container
#      is replaced repeatedly and /etc does not survive it) or,
#      failing that, CL_BACKUP_PASSPHRASE in the environment. NEVER
#      from a CLI argument, NEVER from a file in the repo, NEVER
#      printed or logged. If neither is set the script refuses to
#      run.
#      `age` was considered and rejected: it is not installed on
#      this VM; gpg 2.4.4 is.
#   4. A JSON sidecar per artifact: timestamp (UTC), database,
#      Alembic revision, PostgreSQL version, plaintext size + SHA-256,
#      encrypted size + SHA-256, retention settings, duration.
#   5. Verification, every run, before the artifacts are trusted:
#      SHA-256 of the encrypted file is re-checked against the
#      sidecar; the artifact is decrypted to a 0600 temp file and
#      its SHA-256 is compared with the recorded plaintext SHA-256;
#      `pg_restore -l` reads the custom-format header (proves the
#      dump is a valid, restorable pg_dump -Fc stream). A failed
#      check deletes the new artifacts and exits non-zero.
#   6. Retention: keep the CL_BACKUP_KEEP_COUNT (default 7) newest
#      backup sets and delete sets older than CL_BACKUP_KEEP_DAYS
#      (default 30) days. A "set" is one .dump.gpg + its .json
#      sidecar (+ the globals pair); pruning removes both together.
#   7. Access: output directory 0700, artifacts 0600, owned by the
#      operator running the script (root on this VM — the systemd
#      timer runs as root; see m1/scripts/systemd/).
#
# Modes:
#   --check [output-dir] [db-name]  verify the newest backup set in
#      the directory (checksum + decrypt + pg_restore -l) without
#      taking a new backup. Exit 0 = verified, non-zero = failed.
#   --dry-run <db-name> [output-dir]  print the plan (commands,
#      filenames, retention) and exit 0 without dumping anything.
#
# Output: one JSON line per event on stdout (backup_started,
# backup_completed, backup_failed, check_passed, check_failed).
# Retention-prune lines go to stderr, so the pruned count captured
# by the script stays a plain number and backup_completed stays
# valid single-line JSON. Any failure exits non-zero. Dumps contain
# password/token hashes — treat artifacts as credential-bearing:
# encrypted at rest, never committed to git (m1/backups/ is
# gitignored; verify with `git status` after every run).
#
# This script backs up ONE database plus cluster globals. It does
# NOT do WAL archiving / PITR (archive_mode=off on this cluster —
# see DATABASE_BACKUP_RECOVERY.md) and local storage is labelled
# honestly as LOCAL storage, not off-site disaster recovery.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

KEEP_COUNT="${CL_BACKUP_KEEP_COUNT:-7}"
KEEP_DAYS="${CL_BACKUP_KEEP_DAYS:-30}"
case "$KEEP_COUNT$KEEP_DAYS" in
    *[!0-9]*) echo "CL_BACKUP_KEEP_COUNT / CL_BACKUP_KEEP_DAYS must be non-negative integers" >&2; exit 2 ;;
esac
OUT_DIR_DEFAULT="$REPO_ROOT/m1/backups"

log_json() { # log_json <event> <key=value>...
    local event="$1"; shift
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    local json="{\"ts\":\"$ts\",\"event\":\"$event\""
    local kv
    for kv in "$@"; do
        json="$json,\"${kv%%=*}\":\"${kv#*=}\""
    done
    printf '%s}\n' "$json"
}

fail() { # fail <db> <reason>
    log_json backup_failed "db=$1" "error=$2"
    exit 1
}

# --- passphrase -------------------------------------------------------------
# Resolved once, into a 0600 temp file that gpg reads with
# --passphrase-file (so the passphrase never appears in a process
# listing). Removed by the EXIT trap.
PASSPHRASE_FILE=""
resolve_passphrase() {
    if [ -n "${CL_BACKUP_PASSPHRASE_FILE:-}" ]; then
        [ -r "$CL_BACKUP_PASSPHRASE_FILE" ] \
            || { echo "passphrase file not readable: $CL_BACKUP_PASSPHRASE_FILE" >&2; exit 2; }
        PASSPHRASE_FILE="$CL_BACKUP_PASSPHRASE_FILE"
    elif [ -n "${CL_BACKUP_PASSPHRASE:-}" ]; then
        PASSPHRASE_FILE="$(mktemp /tmp/cl-backup-passphrase-XXXXXX)"
        chmod 600 "$PASSPHRASE_FILE"
        printf '%s' "$CL_BACKUP_PASSPHRASE" > "$PASSPHRASE_FILE"
    else
        echo "no backup passphrase: set CL_BACKUP_PASSPHRASE_FILE (preferred) or CL_BACKUP_PASSPHRASE" >&2
        echo "The passphrase lives outside the repo (e.g. /etc/courierledger/backup.passphrase, mode 0600)." >&2
        exit 2
    fi
}

TMP_FILES=()
cleanup_tmp() {
    local f
    for f in "${TMP_FILES[@]}"; do
        [ -e "$f" ] || continue
        if command -v shred >/dev/null 2>&1; then shred -u -- "$f" 2>/dev/null || rm -f -- "$f"; else rm -f -- "$f"; fi
    done
    # The env-var passphrase temp file is ours; a referenced
    # CL_BACKUP_PASSPHRASE_FILE is NOT deleted (it is the operator's).
    if [ -n "${CL_BACKUP_PASSPHRASE:-}" ] && [ -n "$PASSPHRASE_FILE" ] \
       && [ "$PASSPHRASE_FILE" != "${CL_BACKUP_PASSPHRASE_FILE:-}" ]; then
        rm -f -- "$PASSPHRASE_FILE"
    fi
}
trap cleanup_tmp EXIT

gpg_encrypt_stream() { # gpg_encrypt_stream <output-file> — reads plaintext on stdin
    gpg --batch --yes --quiet --symmetric --cipher-algo AES256 \
        --s2k-mode 3 --s2k-count 65011712 --compress-algo none \
        --passphrase-file "$PASSPHRASE_FILE" -o "$1"
}

# --- verification -----------------------------------------------------------
# verify_artifact <encrypted-file> <sidecar.json>
# Re-checks the encrypted SHA-256, decrypts to a 0600 temp file,
# compares the plaintext SHA-256 with the sidecar, and runs
# `pg_restore -l` over the decrypted stream (header check only —
# a full restore is verify_restore.sh's job). Returns 0 on success.
verify_artifact() {
    local enc="$1" sidecar="$2"
    local want_enc want_plain plain_size
    want_enc="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['sha256_encrypted'])" "$sidecar")"
    want_plain="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['sha256_plaintext'])" "$sidecar")"
    plain_size="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['size_plaintext_bytes'])" "$sidecar")"
    local got_enc
    got_enc="$(sha256sum "$enc" | cut -d' ' -f1)"
    [ "$got_enc" = "$want_enc" ] || { echo "checksum mismatch (encrypted): $enc" >&2; return 1; }
    local tmp_plain
    tmp_plain="$(mktemp /tmp/cl-backup-verify-XXXXXX)"
    chmod 600 "$tmp_plain"
    TMP_FILES+=("$tmp_plain")
    gpg --batch --yes --quiet --decrypt --passphrase-file "$PASSPHRASE_FILE" \
        -o "$tmp_plain" "$enc" || { echo "decrypt failed: $enc" >&2; return 1; }
    local got_plain got_size
    got_plain="$(sha256sum "$tmp_plain" | cut -d' ' -f1)"
    got_size="$(stat -c%s "$tmp_plain")"
    [ "$got_plain" = "$want_plain" ] || { echo "checksum mismatch (plaintext): $enc" >&2; return 1; }
    # The sidecar's plaintext size is filled in by finalise_sidecar
    # AFTER verification (verification is what measures it), so a
    # null/None value here means "not yet known", not a mismatch.
    if [ "$plain_size" != "None" ] && [ "$plain_size" != "null" ]; then
        [ "$got_size" = "$plain_size" ] || { echo "size mismatch (plaintext): $enc" >&2; return 1; }
    fi
    if [ "${3:-dump}" = "dump" ]; then
        pg_restore -l "$tmp_plain" >/dev/null \
            || { echo "pg_restore -l header check failed: $enc" >&2; return 1; }
    fi
    return 0
}

# --- one artifact: dump -> encrypt -> sidecar -> verify ----------------------
# backup_stream <kind: dump|globals> <db> <stamp> <out-dir>
# <kind> dump: pg_dump -Fc <db>; globals: pg_dumpall --globals.
# Writes <base>.gpg + <base>.json; echoes the .gpg path.
backup_stream() {
    local kind="$1" db="$2" stamp="$3" out_dir="$4"
    local base suffix
    if [ "$kind" = "dump" ]; then suffix="dump"; else suffix="globals"; fi
    base="$out_dir/$db-$stamp.$suffix"
    local enc="$base.gpg" sidecar="$base.json"
    local started ended plain_sha_file
    started="$(date +%s)"
    plain_sha_file="$(mktemp /tmp/cl-backup-plainsha-XXXXXX)"
    TMP_FILES+=("$plain_sha_file")
    # The plaintext SHA is captured from the pipe (tee) so the dump
    # itself never touches disk unencrypted.
    if [ "$kind" = "dump" ]; then
        su postgres -c "pg_dump -d $db -Fc" \
            | tee >(sha256sum | cut -d' ' -f1 > "$plain_sha_file") \
            | gpg_encrypt_stream "$enc"
    else
        su postgres -c "pg_dumpall --globals" \
            | tee >(sha256sum | cut -d' ' -f1 > "$plain_sha_file") \
            | gpg_encrypt_stream "$enc"
    fi
    # tee's process substitution is async: wait for the sha file to
    # be populated before reading it.
    local waited=0
    while [ ! -s "$plain_sha_file" ] && [ "$waited" -lt 50 ]; do sleep 0.1; waited=$((waited + 1)); done
    [ -s "$plain_sha_file" ] || { echo "plaintext checksum capture failed: $enc" >&2; return 1; }
    ended="$(date +%s)"
    local alembic pg_version
    alembic="$(su postgres -c "psql -d $db -tAc 'SELECT version_num FROM alembic_version;'" 2>/dev/null || echo unknown)"
    pg_version="$(su postgres -c "psql -d $db -tAc 'SHOW server_version;'" 2>/dev/null || echo unknown)"
    chmod 600 "$enc"
    python3 - "$enc" "$sidecar" <<EOF
import hashlib, json, os, sys
enc, sidecar = sys.argv[1], sys.argv[2]
h = hashlib.sha256()
size_enc = 0
with open(enc, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
        size_enc += len(chunk)
plain_sha = open("$plain_sha_file").read().strip()
# Plaintext size is measured at verification time (decrypted temp
# file); record it now as unknown and let verify fill it in.
meta = {
    "artifact": os.path.basename(enc),
    "kind": "$kind",
    "database": "$db",
    "created_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "alembic_revision": "$alembic",
    "postgresql_version": "$pg_version",
    "pg_dump_format": "custom (-Fc)" if "$kind" == "dump" else "plain SQL (pg_dumpall --globals)",
    "encryption": "gpg symmetric AES-256 (s2k-mode 3)",
    "sha256_plaintext": plain_sha,
    "size_plaintext_bytes": None,
    "sha256_encrypted": h.hexdigest(),
    "size_encrypted_bytes": size_enc,
    "duration_seconds": $((ended - started)),
    "retention": {"keep_count": $KEEP_COUNT, "keep_days": $KEEP_DAYS},
    "verified": False,
}
json.dump(meta, open(sidecar, "w"), indent=2)
EOF
    chmod 600 "$sidecar"
}

# After verification, fill the plaintext size into the sidecar and
# mark it verified (verification decrypts, so it knows the size).
finalise_sidecar() { # finalise_sidecar <enc> <sidecar>
    local enc="$1" sidecar="$2"
    local tmp_plain
    tmp_plain="$(mktemp /tmp/cl-backup-verify-XXXXXX)"
    chmod 600 "$tmp_plain"
    TMP_FILES+=("$tmp_plain")
    gpg --batch --yes --quiet --decrypt --passphrase-file "$PASSPHRASE_FILE" \
        -o "$tmp_plain" "$enc"
    local size
    size="$(stat -c%s "$tmp_plain")"
    python3 - "$sidecar" "$size" <<'EOF'
import json, sys
sidecar, size = sys.argv[1], int(sys.argv[2])
meta = json.load(open(sidecar))
meta["size_plaintext_bytes"] = size
meta["verified"] = True
json.dump(meta, open(sidecar, "w"), indent=2)
EOF
}

# --- retention --------------------------------------------------------------
# prune_retention <out-dir> <db> — keep KEEP_COUNT newest sets, drop
# sets older than KEEP_DAYS days. A set = one .dump.gpg + .json.
prune_retention() {
    local out_dir="$1" db="$2"
    local pruned=0
    # Age-based prune first (find -mtime, .dump.gpg only — the .json
    # is removed alongside).
    while IFS= read -r -d '' f; do
        rm -f -- "$f" "${f%.gpg}.json"
        pruned=$((pruned + 1))
        log_json retention_pruned "db=$db" "file=$(basename "$f")" "reason=older_than_${KEEP_DAYS}d" >&2
    done < <(find "$out_dir" -maxdepth 1 -name "$db-*.dump.gpg" -mtime "+$KEEP_DAYS" -print0 2>/dev/null)
    # Count-based prune: newest KEEP_COUNT sets survive.
    local total
    total="$(find "$out_dir" -maxdepth 1 -name "$db-*.dump.gpg" | wc -l)"
    if [ "$total" -gt "$KEEP_COUNT" ]; then
        local excess=$((total - KEEP_COUNT))
        while IFS= read -r -d '' f; do
            rm -f -- "$f" "${f%.gpg}.json"
            pruned=$((pruned + 1))
            log_json retention_pruned "db=$db" "file=$(basename "$f")" "reason=exceeds_keep_count_${KEEP_COUNT}" >&2
        done < <(find "$out_dir" -maxdepth 1 -name "$db-*.dump.gpg" -printf '%T@ %p\0' 2>/dev/null \
                 | sort -zn | head -zn "$excess" | cut -z -d' ' -f2-)
    fi
    # Globals pairs follow the same stamp: prune orphaned globals
    # artifacts whose dump set is gone.
    while IFS= read -r -d '' f; do
        local stem="${f%.globals.gpg}"
        if [ ! -e "$stem.dump.gpg" ]; then
            rm -f -- "$f" "${f%.gpg}.json"
            log_json retention_pruned "db=$db" "file=$(basename "$f")" "reason=orphaned_globals" >&2
        fi
    done < <(find "$out_dir" -maxdepth 1 -name "$db-*.globals.gpg" -print0 2>/dev/null)
    echo "$pruned"
}

# --- modes ------------------------------------------------------------------
MODE="backup"
if [ "${1:-}" = "--check" ]; then MODE="check"; shift; fi
if [ "${1:-}" = "--dry-run" ]; then MODE="dry-run"; shift; fi

if [ "$MODE" = "check" ]; then
    OUT_DIR="${1:-$OUT_DIR_DEFAULT}"
    DB="${2:-}"
    [ -d "$OUT_DIR" ] || { echo "backup directory not found: $OUT_DIR" >&2; exit 2; }
    resolve_passphrase
    latest="$(ls -t "$OUT_DIR"/${DB:+$DB-}*.dump.gpg 2>/dev/null | head -n 1 || true)"
    [ -n "$latest" ] || { echo "no backup found in $OUT_DIR" >&2; exit 1; }
    sidecar="${latest%.gpg}.json"
    [ -f "$sidecar" ] || { echo "sidecar missing for $latest" >&2; exit 1; }
    if verify_artifact "$latest" "$sidecar" dump; then
        log_json check_passed "file=$(basename "$latest")"
        exit 0
    else
        log_json check_failed "file=$(basename "$latest")"
        exit 1
    fi
fi

DB="${1:?usage: backup_encrypted.sh <db-name> [output-dir] [env-file]}"
OUT_DIR="${2:-$OUT_DIR_DEFAULT}"
ENV_FILE="${3:-$SCRIPT_DIR/../backend/.env}"
[ -f "$ENV_FILE" ] || echo "note: env file not found ($ENV_FILE) — proceeding; no DB credential is needed for a superuser dump" >&2

STAMP="$(date +%F-%H%M)"
DUMP_ENC="$OUT_DIR/$DB-$STAMP.dump.gpg"
GLOB_ENC="$OUT_DIR/$DB-$STAMP.globals.gpg"

if [ "$MODE" = "dry-run" ]; then
    cat <<EOF
dry-run: no backup will be taken.
  database:        $DB
  output dir:      $OUT_DIR (mode 0700)
  dump artifact:   $(basename "$DUMP_ENC")
  globals artifact: $(basename "$GLOB_ENC")
  sidecars:        $(basename "$DUMP_ENC" .gpg).json, $(basename "$GLOB_ENC" .gpg).json
  encryption:      gpg symmetric AES-256 (passphrase from CL_BACKUP_PASSPHRASE_FILE or CL_BACKUP_PASSPHRASE)
  retention:       keep newest $KEEP_COUNT sets, prune older than $KEEP_DAYS days
  verification:    SHA-256 (encrypted + plaintext) + decrypt + pg_restore -l, every run
EOF
    exit 0
fi

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
resolve_passphrase
log_json backup_started "db=$DB" "out_dir=$OUT_DIR"

# A same-minute re-run would collide on filenames: refuse rather
# than silently overwrite a verified backup.
if [ -e "$DUMP_ENC" ] || [ -e "$GLOB_ENC" ]; then
    fail "$DB" "backup artifacts for stamp $STAMP already exist; wait a minute and retry"
fi

# su postgres must be able to see the DB; fail fast with a clear
# message instead of a bare pg_dump error.
if ! su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname = '$DB'\"" | grep -qx 1; then
    fail "$DB" "database does not exist on this cluster"
fi

if ! backup_stream dump "$DB" "$STAMP" "$OUT_DIR"; then
    rm -f -- "$DUMP_ENC" "$OUT_DIR/$DB-$STAMP.dump.json"
    fail "$DB" "pg_dump/encrypt failed"
fi
if ! backup_stream globals "$DB" "$STAMP" "$OUT_DIR"; then
    rm -f -- "$GLOB_ENC" "$OUT_DIR/$DB-$STAMP.globals.json"
    fail "$DB" "pg_dumpall --globals/encrypt failed"
fi

DUMP_JSON="$OUT_DIR/$DB-$STAMP.dump.json"
GLOB_JSON="$OUT_DIR/$DB-$STAMP.globals.json"
if ! verify_artifact "$DUMP_ENC" "$DUMP_JSON" dump; then
    rm -f -- "$DUMP_ENC" "$DUMP_JSON" "$GLOB_ENC" "$GLOB_JSON"
    fail "$DB" "verification failed for dump artifact; artifacts removed"
fi
if ! verify_artifact "$GLOB_ENC" "$GLOB_JSON" globals; then
    rm -f -- "$DUMP_ENC" "$DUMP_JSON" "$GLOB_ENC" "$GLOB_JSON"
    fail "$DB" "verification failed for globals artifact; artifacts removed"
fi
finalise_sidecar "$DUMP_ENC" "$DUMP_JSON"
finalise_sidecar "$GLOB_ENC" "$GLOB_JSON"

PRUNED="$(prune_retention "$OUT_DIR" "$DB")"
DUMP_SIZE="$(stat -c%s "$DUMP_ENC")"
GLOB_SIZE="$(stat -c%s "$GLOB_ENC")"
log_json backup_completed "db=$DB" \
    "dump_file=$(basename "$DUMP_ENC")" "dump_size_bytes=$DUMP_SIZE" \
    "globals_file=$(basename "$GLOB_ENC")" "globals_size_bytes=$GLOB_SIZE" \
    "retention_pruned=$PRUNED"
echo "backup: $DUMP_ENC ($DUMP_SIZE bytes) + $GLOB_ENC ($GLOB_SIZE bytes)"
echo "sidecars: $DUMP_JSON $GLOB_JSON"
