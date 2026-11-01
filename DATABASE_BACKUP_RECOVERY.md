# CourierLedger — Database Backup & Recovery

**Status:** Phase 3, Stream A (2026-11-01). This document is owned
by Stream A (Backup & Disaster Recovery + Maintenance Scheduling).
Other streams own `DATABASE_OPERATIONS.md` (roles, provisioning,
timeouts), `MIGRATION_VERIFICATION.md` (migration tests),
`PRODUCTION_CONFIGURATION_GUIDE.md`, `INFRASTRUCTURE_TEST_REPORT.md`,
and `INCIDENT_RESPONSE_RUNBOOK.md` — those are cross-referenced by
name only and were not edited here.

**Headline, stated plainly:** the backup *workflow* now exists and
is scheduled on this VM (daily encrypted backups + hourly
maintenance, both proven end-to-end). The project still has **no
off-site disaster recovery**: backups live on the same disk as the
database, there is no WAL archiving / PITR, and there is no
production host. RPO/RTO figures below are **targets until
verified**, labelled as such.

---

## 1. Status board

Every claim in this document falls into exactly one of these
buckets. If a line does not say which bucket it is in, treat it
as *documented design*.

| Item | Bucket | Evidence / note |
|---|---|---|
| Encrypted scheduled backups (`backup_encrypted.sh`) | **Implemented and verified** | §4, §5 — real runs on the live cluster, twice (before and after a container replacement) |
| Restore drill (`verify_restore.sh`, incl. `.gpg` input) | **Implemented and verified** | §5 — two drills, both exit 0, identities 0 |
| Backup integrity check (`--check` mode) | **Implemented and verified** | §4 — `check_passed`, exit 0 |
| Retention (keep 7 / 30 days) | **Implemented and verified** | §4 — proven with 10 synthetic old sets |
| Maintenance scheduling (hourly purge) | **Implemented and verified** | §7 — CLI runs + systemd timers enabled + `systemctl start` proven |
| Maintenance job logic (tests) | **Implemented and verified** | §7 — 4 live-PostgreSQL tests, 4 passed |
| Globals backup (`pg_dumpall --globals`) | **Implemented and verified** | §4 — encrypted alongside every dump, verified on restore of the dump (globals restore itself not yet run — see below) |
| WAL archiving / PITR | **Documented design** | §6 — not enabled; exact limitation + required config recorded |
| Off-host copy | **Blocked by environment** | §8 — no host, no provider, no paid storage selected |
| Failure alerts (email/webhook) | **Requires founder approval** | §9 — journal + non-zero exit exist; no mailbox/webhook chosen |
| RPO ≤ 1 h / RTO ≤ 2 h | **Targets, not achievements** | §10 — RPO ≤ 1 h is unachievable without WAL archiving |
| `courierledger_dev` scheduled backup | **Documented design** | Only `courierledger_app` is scheduled; dev holds no data worth scheduling (§4) |

**"Implemented but not verified" items:** the globals artifact
is *created and checksummed* on every run, but a globals *restore*
(restoring roles from the `.globals.gpg` into a fresh cluster) has
not been run yet — the restore drill verifies the per-database
dump only. The cron fallback lines in
`m1/scripts/systemd/README.md` are documented, not installed,
because cron does not exist on this VM.

---

## 2. The one fact that shapes the whole design

**An owner-role dump fails.** Dumping as `courierledger` (the table
owner) fails with:

```
pg_dump: error: query failed: ERROR: query would be affected by
row-level security policy for table "audit_logs"
```

FORCE RLS applies to the table owner, and `pg_dump` sets no tenant
context. The backup credential must therefore **bypass RLS** — the
postgres superuser on this VM, or in production a dedicated backup
role with `BYPASSRLS` used ONLY for dumps. That is a documented
exception to the no-bypass rule, never the runtime role and never
the app's `DATABASE_URL` credential. `backup_encrypted.sh` runs
`pg_dump` as the superuser for exactly this reason (via
`su postgres`, peer auth — the same pattern `provision_db.sh`
uses).

---

## 3. What exists today (verified 2026-11-01, Phase 3)

| Item | State |
|---|---|
| Scheduled backups | **Daily 02:30 UTC** systemd timer (`courierledger-backup.timer`), enabled on this VM. Script: `m1/scripts/backup_encrypted.sh`. |
| WAL archiving | **Off.** `archive_mode = off`, `wal_level = replica`, no archive command. No PITR (§6). |
| Base backups | None (no `pg_basebackup`; PITR needs one — §6). |
| Backup scripts | `m1/scripts/backup_encrypted.sh` (new, Phase 3 — canonical) and `m1/scripts/backup_db.sh` (Phase 2, kept as-is; unencrypted, no sidecar, no retention — superseded, not deleted). |
| Restore drills | **Two proof runs** (Phase 3, §5). No recurring drill scheduled beyond the daily backup's built-in verification. |
| Off-host copy | None. A dump on the same disk as the database is not a backup (§8). |
| Globals backup | `pg_dumpall --globals` encrypted alongside every dump (§4). Restore of globals not yet run. |
| Encryption | **gpg symmetric AES-256**, passphrase from a file outside the repo (§4). |
| Maintenance schedule | **Hourly** systemd timer (`courierledger-maintenance.timer`), enabled on this VM (§7). |

---

## 4. The backup workflow (`m1/scripts/backup_encrypted.sh`)

### 4.1 What one run does

```
m1/scripts/backup_encrypted.sh <db-name> [output-dir] [env-file]
m1/scripts/backup_encrypted.sh --check [output-dir] [db-name]
m1/scripts/backup_encrypted.sh --dry-run <db-name> [output-dir]
```

1. **`pg_dump -Fc` as the postgres superuser**, streamed through
   a pipe straight into `gpg` — no plaintext dump file ever sits
   on disk. (Phase 2 staged the dump in `/tmp`; the pipe removes
   that exposure entirely.)
2. **`pg_dumpall --globals`** (roles only — the cluster-level
   objects a per-database dump does not contain), encrypted the
   same way. The globals artifact contains role password
   verifiers: it is encrypted and 0600 like everything else.
3. **Encryption: `gpg --symmetric --cipher-algo AES256`**
   (s2k-mode 3, 65,011,712 iterations, no extra compression —
   `pg_dump -Fc` is already compressed). `age` was considered and
   rejected: it is not installed on this VM; gpg 2.4.4 is.
4. **A JSON sidecar per artifact** (`<db>-<stamp>.dump.json`,
   `<db>-<stamp>.globals.json`): timestamp (UTC), database,
   Alembic revision, PostgreSQL version, plaintext size +
   SHA-256, encrypted size + SHA-256, retention settings,
   duration, `verified` flag. Example (2026-11-01 18:33 run):
   dump 226,294 bytes encrypted / 226,119 plaintext,
   SHA-256 recorded for both, `alembic_revision: "0007"`,
   `postgresql_version: "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)"`,
   `verified: true`.
5. **Verification, every run, before the artifacts are trusted:**
   the encrypted SHA-256 is re-checked against the sidecar; the
   artifact is decrypted to a 0600 temp file and its SHA-256 is
   compared with the recorded plaintext SHA-256; `pg_restore -l`
   reads the custom-format header (proves a valid, restorable
   `-Fc` stream). A failed check **deletes the new artifacts**
   and exits non-zero — an unverified backup is never left in
   place pretending to be good. `--check` re-runs this
   verification against the newest backup without taking a new one.
6. **Retention** (env-configurable, defaults `CL_BACKUP_KEEP_COUNT=7`,
   `CL_BACKUP_KEEP_DAYS=30`): keep the 7 newest backup sets and
   delete sets older than 30 days. A set is one `.dump.gpg` + its
   `.json` (+ the globals pair, pruned with its set). Prune events
   are logged as JSON lines to stderr.
7. **Access:** output directory 0700, artifacts 0600, owned by
   the operator running the script (root on this VM — the systemd
   timer runs as root). A same-minute re-run is **refused**
   (filename collision guard) rather than overwriting a verified
   backup.
8. **Output:** one JSON line per event on stdout
   (`backup_started`, `backup_completed`, `backup_failed`,
   `check_passed`, `check_failed`). Any failure exits non-zero.

Dumps contain password/token hashes (`users.password_hash`,
`user_sessions.token_hash`) — treat artifacts as
credential-bearing material: encrypted at rest, readable only by
the backup operator, never committed to git. **`m1/backups/` is
gitignored** — the Phase 2 document claimed this, but the
gitignore entry did not actually exist; it was added in Phase 3
(`.gitignore`, `m1/backups/`). Verify with `git status` after
every run.

### 4.2 Key handling — and the lesson from 2026-11-01

The passphrase comes ONLY from `CL_BACKUP_PASSPHRASE_FILE` (a
0600 file outside the repo) or, failing that, `CL_BACKUP_PASSPHRASE`
in the environment. **Never** from a CLI argument, never from a
file in the repo, never printed or logged. If neither is set the
script refuses to run.

The passphrase file lives at
`/home/hatch/.config/courierledger/backup.passphrase` (0600,
root-owned) — under the **home directory, NOT under `/etc`**.
That placement is deliberate and was learned the hard way (see
§9): this VM's container is replaced repeatedly and `/etc` does
**not** survive a replacement, while the home directory does. A
passphrase kept only in `/etc` was lost in the 2026-11-01
container replacement, orphaning an encrypted backup (§9). The
systemd service reads the path from
`/etc/courierledger/backup.env` (one line, 0600); that env file
is a pointer, not the secret — if `/etc` is wiped, the env file
is recreated in one command and the passphrase itself survives
in `~/.config`. Rotation = generate a new file value; old
backups encrypted under the old passphrase become
undecryptable, so rotation must be paired with a fresh backup
and a restore drill under the new passphrase.

### 4.3 Retention proof (actually performed, 2026-11-01)

10 synthetic old backup sets (2026-10-01…10) plus one real run
with `KEEP_COUNT=7`, `KEEP_DAYS=30`:

- Age prune removed the 2026-10-01 set (31 days old, `> 30d`).
- Count prune removed 2026-10-02, -03, -04 (exceeding keep 7).
- Exactly 7 sets remained (2026-10-05…10 + the new one), each
  with its `.json` sidecar removed alongside its `.gpg`.

A second run in the same minute was refused by the collision
guard (verified: `backup_failed`, non-zero exit) — the guard
works as designed.

### 4.4 What is scheduled, and what is not

Only `courierledger_app` is scheduled (daily 02:30 UTC = 08:30
Dhaka). `courierledger_dev` holds no data worth scheduling — it
is empty apart from the 5 seeded couriers. If it ever holds
data, add a second `ExecStart` line (or a second timer) for it;
the script already takes the database name as an argument.

---

## 5. Restore proof (actually performed, Phase 3)

`verify_restore.sh` restores into a **separate fresh database**
(default `<source>_restore_verify`), never the source and never
the working databases, and refuses a target equal to the source.
Phase 3 extended it to accept encrypted `*.dump.gpg` artifacts:
it checks the encrypted SHA-256 against the JSON sidecar, decrypts
to a 0600 temp file (passphrase from `CL_BACKUP_PASSPHRASE_FILE`
or `CL_BACKUP_PASSPHRASE`), restores that, and wipes the temp file
on exit (shred if available). The comparison is unchanged from
Phase 2: object counts (tables, policies, functions, triggers,
indexes, constraints, RLS FORCE/enabled, alembic version,
couriers); per-table row counts (`diff` must be empty); financial
identities on `reconciliation_results` (`V_total = C − Δ − V_R + S`,
`V_cond = S − V_R`, 0 violations where all terms are non-NULL);
settlement variance arithmetic (`bank_variance = allocated −
expected`); a runtime-role RLS probe (no context → 0 merchants).
It drops the restore database on success and **leaves it in place
on failure** for inspection.

Two drills were run in Phase 3 (the container replacement between
them is documented in §9):

| Measure | Drill 1 (2026-11-01 18:27) | Drill 2 (2026-11-01 18:33) |
|---|---|---|
| Source | `courierledger_app`, Alembic **0006** | `courierledger_app` re-provisioned, Alembic **0007** |
| Source rows | 8 users, 8 `reconciliation_results`, 4 settlement variance calculations, 56 `audit_logs`, 33 tables | 0 users / 0 results / 0 audit (empty apart from 5 couriers), 33 tables |
| Encrypted dump size | **263,286 bytes** (plaintext 263,101) | **226,294 bytes** (plaintext 226,119) |
| Globals artifact | 2,015 bytes | 2,015 bytes |
| Backup duration | ~2 s | ~3 s (incl. verification) |
| Sidecar checksum | verified | verified |
| Restore target | Fresh DB `courierledger_app_restore_verify` | Same |
| Restore duration | ~5 s total drill | ~9 s total drill |
| Object counts | identical | identical |
| Per-table row counts | `diff` empty — identical | `diff` empty — identical |
| Financial identities | V_total **0**, V_cond **0**, variance-calc **0** | V_total **0**, V_cond **0**, variance-calc **0** |
| RLS probe on restored DB | No context → 0 merchants (fail-closed) | Same |
| Restore DB afterwards | dropped (verification passed) | dropped (verification passed) |

Caveats, stated plainly: Drill 1 is the stronger proof — it had
real (synthetic) business rows, so the identity checks meant
something. Drill 2's identity checks are vacuous (no business
rows to violate anything); it proves the *procedure* on the fresh
schema (Alembic 0007) and the new passphrase, not the data. The
database is tiny in both cases, so the durations prove the
procedure, not production-scale timing. `pg_dump -Fc` does not
include cluster roles/config — the globals artifact covers roles
only; `postgresql.conf` / `pg_hba.conf` are not backed up by
either script yet (see §10).

---

## 6. WAL archiving / PITR — verdict: NOT implemented

Checked on the live cluster (2026-11-01, before and after the
container replacement — identical in both):

| Setting | Value | Source |
|---|---|---|
| `archive_mode` | `off` | `SHOW archive_mode` |
| `archive_command` | `(disabled)` | `SHOW archive_command` |
| `wal_level` | `replica` | `SHOW wal_level` |
| `postgresql.conf` | no `archive_mode` / `wal_level` / `archive_command` lines at all (all defaults) | `/etc/postgresql/16/main/postgresql.conf` |

**Verdict:** PITR is *supportable* on this cluster but **not
implemented**. `wal_level = replica` is already sufficient for
archiving — that part needs no change. What is missing, exactly:

1. `archive_mode = on` in `postgresql.conf` (`archive_mode` is a
   **postmaster parameter: changing it requires a cluster
   restart**).
2. An `archive_command` that copies each WAL segment somewhere
   durable, e.g.:
   `archive_command = 'test ! -f /var/lib/postgresql/16/wal-archive/%f && cp %p /var/lib/postgresql/16/wal-archive/%f'`
   (local path shown only as an example — a same-disk archive
   directory is not disaster recovery, see below).
3. A base backup (`pg_basebackup`) taken *after* archiving is on
   — PITR replays WAL on top of a base backup; without one there
   is nothing to recover to.
4. Archive storage with retention and monitoring (a filled
   archive disk stops the database — `archive_command` failing
   makes WAL accumulate in `pg_wal` until the disk fills).
5. A tested recovery procedure: restore base backup +
   `recovery_target_time`, then run the §5 comparison.

**Why it was not enabled in Phase 3:** enabling it needs a
cluster restart on a shared dev cluster while other streams are
running live tests against it, and the only archive storage
available on this VM is the same disk as the database — which
would let the project *claim* PITR while still having no
off-disk recovery at all. That would be pretending. So PITR is
recorded here as **documented design**, not implemented. The
founder decision is: choose archive storage (off-host, with a
retention policy) and a maintenance window for the restart, then
enable it and run a point-in-time recovery drill. Until then the
RPO is the backup interval: with daily dumps, up to **24 hours**
of data loss — the RPO ≤ 1 h target in §10 is unachievable on
dumps alone (hourly dumps would be needed to meet 1 h without
archiving).

---

## 7. Maintenance scheduling (Workstream 4)

### 7.1 What the job does — and what it deliberately does not

The job is `m1/scripts/maintenance.py` (CLI) + the service
`m1/backend/app/services/maintenance_service.py`. It wraps the
**one purge function the codebase already provides**,
`auth_service.purge_expired_auth_rows` (Phase 1.1, B-F19), and
adds the three things a scheduled job needs that the function
alone does not: a PostgreSQL advisory lock, a `--dry-run` count
path, and a structured result. The old docs mislabelled this as
a "procedure"; the actual implementation is the Python function
in `m1/backend/app/services/auth_service.py` (line ~241) — the
scheduler is built around that real code, not the doc label.

The ONLY deletion the job performs:

- `user_sessions` rows whose `expires_at` has passed (expired,
  whether revoked or not — a revoked-but-unexpired session is
  KEPT: purge is by expiry, not revocation);
- `password_reset_tokens` rows that are expired OR already used.

What it NEVER does:

- never deletes active records (the two predicates above are the
  whole deletion set — there is no other DELETE in the module);
- never touches immutable reconciliation history
  (`reconciliation_results` / `reconciliation_runs` / fee lines /
  findings), settlement history, or `audit_logs` (append-only,
  CL-03/CL-10/CL-16) — those tables appear in no statement here;
- never runs the idempotency-key purge (CL-19): the maintenance
  role holds a DELETE grant on `idempotency_keys` in `roles.sql`,
  but **no purge function for it exists in the codebase**, so
  there is nothing approved to run. Adding one is a reviewed
  change, not a scheduler decision. Likewise the roles.sql note
  suggesting a DELETE grant on the auth tables for the
  maintenance role was **not** acted on — the job runs as the
  table-owner role instead, exactly as the purge function's
  docstring prescribes (see §7.2).

### 7.2 Role, lock, frequency

- **Role:** the job MUST run as the table-owner role
  (`courierledger`), never the runtime role. `roles.sql` grants
  NO DELETE on `user_sessions` / `password_reset_tokens` to
  either application role by design, so the runtime role would
  fail with `InsufficientPrivilege` and delete nothing. The CLI
  resolves the owner credential the same way
  `provision_db.sh` does (`MIGRATION_DATABASE_URL`, falling back
  to the `DATABASE_URL` password — the local-dev shared value —
  read from `m1/backend/.env` at runtime, never printed).
  PostgreSQL only: the service refuses any non-PostgreSQL
  session (the SQLite test fallback is not the M1 data model).
- **Concurrency:** a PostgreSQL advisory lock (fixed key
  `20261102`, session-level, held for the whole run). A second
  overlapping run reports `{"status": "skipped", "reason":
  "lock_held"}` and exits 0 — overlap is not a failure and never
  a double-delete. The lock is released in a `finally` block
  (and by session close if the process dies).
- **Frequency: hourly** (`courierledger-maintenance.timer`,
  `OnCalendar=hourly`, `Persistent=true`). Justification: the
  purge is cheap (~1 s on the current database), idempotent, and
  lock-guarded, so hourly costs nothing and bounds the lifetime
  of stale rows to about an hour — matching the 1-hour
  password-reset token expiry. A daily cadence would also be
  defensible; hourly was chosen because the reset-token table
  would otherwise accumulate a full day of used tokens between
  runs. The timer covers both working databases
  (`--db courierledger_app --db courierledger_dev`).
- **Failure visibility:** one JSON line per database on stdout
  (`completed` / `dry_run` / `skipped` / `error`), exit 0 on
  success-or-skipped, exit 2 on failure (e.g. unknown database →
  JSON error line, exit 2 — verified). The service's own
  structured log lines go to the journal with it.

### 7.3 Scheduler choice — systemd timer, not cron

`cron` / `crond` is **not installed on this VM** (verified
2026-11-01: `which cron crond` returns nothing) and no cron
daemon runs. `systemd` **is** the init system (`systemctl
is-system-running` → `running`). The mechanism that genuinely
works here is therefore a systemd timer; a crontab entry would
never fire. Timers also give `Persistent=true` (a missed run
after downtime is replayed — cron silently skips it),
journal-integrated logs, and `RandomizedDelaySec`. The units
live in `m1/scripts/systemd/` (services + timers + `README.md`
with install steps and the cron fallback lines, documented only).
Both timers are **enabled on this VM** (backup next 02:34 UTC,
maintenance next 19:00 UTC at install time) and the timer path
was proven with `systemctl start` on both services
(`Result=success`, exit 0; backup produced a new verified set,
maintenance logged `completed` 0/0 for both DBs in the journal).

### 7.4 Maintenance evidence (actually performed, 2026-11-01)

Live run against `courierledger_dev` with seeded rows (2 expired
sessions + 1 live session; 1 expired token + 1 used token + 1
live token; synthetic user `maint-cli-test@example.com`, removed
afterwards — dev DB back to 0 sessions / 0 tokens / 0 users):

| Run | Result |
|---|---|
| `--dry-run` | `{"status": "dry_run", "sessions": 2, "reset_tokens": 2}`, exit 0, nothing deleted |
| Real run | `{"status": "completed", "sessions": 2, "reset_tokens": 2}`, exit 0 |
| Second run | `{"status": "completed", "sessions": 0, "reset_tokens": 0}`, exit 0 (idempotent) |
| Multi-DB run (`--db dev --db app`) | both `completed` 0/0, exit 0 |
| Unknown DB | JSON `error` line, exit 2 |
| After all runs | only `cli-live` session + `cli-live-token` remain — the live rows were kept |

Automated tests: `m1/backend/tests/test_maintenance_live_pg.py`
(live PostgreSQL, owner role — never SQLite for these):

- purge deletes only expired/used rows, keeps live +
  revoked-but-unexpired, and an append-only `audit_logs` row
  survives untouched (seeded under a merchant context — see the
  test's note on `INSERT ... RETURNING` vs the audit SELECT
  policy);
- second run deletes nothing (idempotent);
- dry-run deletes nothing, and a following real run removes
  exactly the counted rows;
- a concurrent run holding the advisory lock is `skipped`.

**Result: 4 passed, 0 failed** (run twice: before and after the
container replacement in §9). The pre-existing purge test in
`tests/test_security_phase11.py` was not modified.

---

## 8. Off-host copy — blocked by environment

There is no off-host copy and no way to make one on this VM
that would be honest: the only storage available is the VM's
own disk. A dump on the same disk as the database is not a
backup — a disk/container loss takes both. What is needed
(founder decision, no paid commitment made here): a storage
destination (object storage, or a second host), credentials for
it in the Secure Vault, and a copy step after verification
(only verified artifacts are copied; the copy is itself
checksum-checked on arrival). Until that exists, the local
backups in `m1/backups/` are correctly labelled **local
storage**, not disaster recovery.

---

## 9. Container-replacement incident, 2026-11-01 (~18:30 UTC)

While Phase 3 work was running, the VM's container was replaced
(the same failure mode as in Phase 1.1): uptime reset to 0, the
`postgres` OS user and the cluster vanished, `/etc` was wiped,
and both working databases were lost with it. What happened:

- **Lost:** both databases as they stood (`courierledger_app`
  with 8 users, 8 reconciliation results, 4 variance
  calculations, 56 audit rows — all synthetic dev data) and the
  backup passphrase, which lived only in
  `/etc/courierledger/backup.passphrase`.
- **Consequence:** the 18:27 encrypted backup
  (`courierledger_app-2026-11-01-1827.dump.gpg`, 263,286 bytes)
  became **undecryptable and was deleted** — an encrypted backup
  without its passphrase is not a backup. Its drill evidence in
  §5 stands as procedure proof; the artifact itself is gone.
- **Recovered:** PostgreSQL 16.15 reinstalled from the apt
  cache path documented in `m1/LOCAL_VERIFICATION.md` §1a
  (cluster recreated UTF8, online on 5432); the `courierledger`
  owner role recreated with the `.env` password; both databases
  re-provisioned with `provision_db.sh` (Alembic **0007** head —
  another stream's migration landed in the tree meanwhile — 33
  tables, 46 policies, 5 couriers, empty of business data).
  A transient `password authentication failed` on the first
  re-provision attempt did not recur on retry (cause not
  established; DSN auth with the same credential verified
  working immediately after — recorded, not explained away).
- **Lesson applied:** the new passphrase lives in
  `/home/hatch/.config/courierledger/backup.passphrase` (home
  persists across replacements; `/etc` does not). The full
  evidence chain was re-run on the fresh cluster (§5 Drill 2,
  §7.4) rather than trusting pre-replacement results.

This incident is also why "no off-host copy" (§8) is the
single largest remaining gap: everything above survived only
because the home directory persisted, which is luck, not design.

---

## 10. Proposed targets (NOT achieved — labelled targets)

| Target | Value | What it requires |
|---|---|---|
| RPO | ≤ 1 hour of data loss | Daily full `pg_dump -Fc` **plus** WAL archiving (§6). With dumps alone the RPO is the dump interval — hourly dumps would be needed to meet 1 h without archiving; daily dumps give ≤ 24 h. |
| RTO | ≤ 2 hours from "database lost" to "app serving verified traffic" | Detection + provisioning a host/cluster + restoring roles/globals/dump + running the §5 comparison + re-pointing the app. The 2 h figure is a process budget, not a measurement (measured restore was ~5–9 s on a tiny database). |
| Retention | 30 daily dumps + 12 monthly dumps | Implemented as keep-7/30-days for now (§4.1); monthly tier needs a second retention class — not built. |
| Encryption | Dumps encrypted at rest | **Done locally** (gpg AES-256, §4). Provider SSE / customer-managed key applies only once an off-host destination exists. |
| Access | Backup operator role / founder only | Dedicated BYPASSRLS backup role or host superuser; never the runtime role; no dump ever committed to git. |
| Drill | Quarterly restore drill | `verify_restore.sh` against a scratch DB with the §5 comparison, before the backup is trusted. The daily backup's built-in verification is not a substitute for a drill. |
| Alerts | Backup failure alerts | The job exits non-zero with a `backup_failed` JSON line and the timer shows `failed` in the journal — but nobody is *notified*. Needs a mailbox or webhook (founder decision). |
| Config backup | `postgresql.conf` / `pg_hba.conf` copies | Not implemented — neither script copies them. |

## 11. What is missing before any of this is real

1. **A host to run it on.** No production host exists yet.
2. **An off-host copy.** A dump on the same disk as the database
   is not a backup (§8).
3. **WAL archiving / PITR** (§6) — needs the founder's storage
   choice and a restart window.
4. **Globals + config restore test.** The globals artifact is
   created and checksummed, but restoring roles from it into a
   fresh cluster has not been run; config files are not backed up
   at all.
5. **A recurring drill** with the results recorded (the Phase 3
   drills were one-offs tied to this work).
6. **Failure alerts** — a mailbox or webhook for `backup_failed`
   (founder decision).

Until items 1–3 exist, the project has **no database recovery
capability** — only a proven, scheduled procedure waiting for a
host, a schedule that survives the host, and an off-site copy.
That is the single largest infrastructure gap for any production
target, and it must be closed before real merchant data is ever
loaded.
