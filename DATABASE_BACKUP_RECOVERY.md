# CourierLedger — Database Backup & Recovery

**Status:** Phase 2 (2026-11-01). The restore *procedure* was proven
once, end-to-end, on this VM. **No backup schedule exists, no
off-host copy exists, and no production host exists** — so nothing in
this document is an achieved capability yet. RPO/RTO figures below
are **targets until verified**, labelled as such.

Related: `DATABASE_OPERATIONS.md` (roles, provisioning, timeouts),
`INFRASTRUCTURE_DESIGN.md` (hosting decisions still open),
`MIGRATION_VERIFICATION.md` (migration tests A–D).

---

## 1. What exists today (verified 2026-11-01)

| Item | State |
|---|---|
| Scheduled backups | **None.** No script, cron job, timer, or doc existed before Phase 2 (repo-wide search: zero hits outside `.venv`). |
| WAL archiving | **Off.** `archive_mode = off`, `wal_level = replica`, no archive command. No PITR. |
| Base backups | None. |
| Backup scripts | **New in Phase 2:** `m1/scripts/backup_db.sh` and `m1/scripts/verify_restore.sh` (see §3). Proven once each. |
| Restore drills | **One proof run** (Phase 2, §4). No recurring drill scheduled. |
| Off-host copy | None. A dump on the same disk as the database is not a backup. |
| Globals backup | Not tested (`pg_dumpall --globals` covers roles/passwords; config files need separate copying). |

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
exception to the no-bypass rule, never the runtime role and never the
app's `DATABASE_URL` credential. `backup_db.sh` runs `pg_dump` as the
superuser for exactly this reason.

## 3. Scripts (Phase 2)

### `m1/scripts/backup_db.sh <db-name> [output-dir] [env-file]`

- Runs `pg_dump -Fc` (custom format, compressed) as the postgres
  superuser. Stages the dump in `/tmp` (the postgres OS user cannot
  write into the repo tree) and moves it into place.
- Writes `<db>-<YYYY-MM-DD-HHMM>.dump` plus a `.sha256` file.
- Default output dir is `m1/backups/` (gitignored — dumps are
  credential-bearing and must never be committed; verify with `git
  status` after every run).
- Dumps one database only. Cluster-level objects (roles, passwords,
  `pg_hba.conf`, `postgresql.conf`) are NOT included.

### `m1/scripts/verify_restore.sh <source-db> <dump-file> [restore-db] [env-file]`

- Restores into a **separate fresh database** (default
  `<source>_restore_verify`). Never the source, never the working
  databases. Refuses a target equal to the source.
- Compares source vs restored and exits non-zero on any mismatch:
  object counts (tables, policies, functions, triggers, indexes,
  constraints, RLS FORCE/enabled, alembic version, couriers);
  per-table row counts (`diff` must be empty); financial identities
  on `reconciliation_results` (`V_total = C − Δ − V_R + S`,
  `V_cond = S − V_R`, 0 violations where all terms are non-NULL);
  settlement variance arithmetic (`bank_variance = allocated −
  expected`); a runtime-role RLS probe (no context → 0 merchants).
- Drops the restore database on success; **leaves it in place on
  failure** for inspection (its name is printed).

## 4. Restore proof (actually performed, Phase 2)

Source: `courierledger_app` (244 synthetic rows, incl. 4
`reconciliation_results`, 2 settlement variance calculations).
`courierledger_dev` was checked first and is empty apart from 5
couriers, so it could not prove a restore.

| Measure | Value |
|---|---|
| Dump format | `pg_dump -Fc`, PostgreSQL 16.15 |
| Dump size | **246,370 bytes** (second run; the first proof run was 245,454 bytes at 244 rows — same shape, one migration later) |
| Dump SHA-256 | `3c8620522fc526f870960e80052ad8a311760d0c2c243c048784cb9732a5eac6` (the proof dump; the file was deleted after verification — dumps are credential-bearing) |
| Dump duration | < 1 s |
| Restore target | Fresh DB `courierledger_app_restore_verify` |
| Restore duration | < 1 s, exit 0 |
| Object counts | 33 / 46 / 15 / 22 / 132 / 264 — identical |
| RLS FORCE / enabled | 24 / 26 — identical |
| Alembic version | 0006 — identical |
| Per-table row counts | `diff` empty — identical (244 rows) |
| Financial identities | V_total violations **0**, V_cond violations **0**, variance-calc violations **0** |
| RLS probe on restored DB | No context → 0 merchants (fail-closed) |

Caveats, stated plainly: the database is tiny, so the durations
prove the *procedure*, not production-scale timing. `pg_dump -Fc`
does not include cluster roles/config. The proof dump was deleted
after verification; the SHA above is the record of what was verified.

## 5. Proposed targets (NOT achieved — labelled targets)

| Target | Value | What it requires |
|---|---|---|
| RPO | ≤ 1 hour of data loss | Daily full `pg_dump -Fc` **plus** WAL archiving (with dumps alone the RPO is the dump interval — hourly dumps would be needed to meet 1 h without archiving) |
| RTO | ≤ 2 hours from "database lost" to "app serving verified traffic" | Detection + provisioning a host/cluster + restoring roles/globals/dump + running the §3 comparison + re-pointing the app. The 2 h figure is a process budget, not a measurement (measured restore was < 1 s on 244 rows) |
| Retention | 30 daily dumps + 12 monthly dumps | Each dump stored with its SHA-256 alongside it; integrity verified by *restoring*, not just by checksum |
| Encryption | Dumps encrypted at rest | `age`/`gpg` with a key in the Secure Vault, or provider SSE with a customer-managed key. Dumps contain password hashes (`users.password_hash`, `user_sessions.token_hash`) — treat them as credential-bearing material |
| Access | Backup operator role / founder only | Dedicated BYPASSRLS backup role or host superuser; never the runtime role; no dump ever committed to git |
| Drill | Quarterly restore drill | `verify_restore.sh` against a scratch DB with the §3 comparison, before the backup is trusted |
| Alerts | Backup failure alerts | The backup job must alert on failure, not just on success — a backup that silently stops running is worse than no backup |

## 6. What is missing before any of this is real

1. **A host to run it on.** No production host exists yet.
2. **A schedule.** Cron or a systemd timer invoking `backup_db.sh`
   (daily) plus WAL archiving for PITR.
3. **An off-host copy.** A dump on the same disk as the database is
   not a backup.
4. **Globals + config backup.** `pg_dumpall --globals` (roles) plus
   `postgresql.conf` / `pg_hba.conf` copies — not tested in Phase 2.
5. **A recurring drill** with the results recorded (the Phase 2 proof
   was a one-off).
6. **A retention/encryption decision** by the founder (no provider
   chosen, no paid storage selected).

Until items 1–3 exist, the project has **no database recovery
capability** — only a proven procedure waiting for a host and a
schedule. That is the single largest infrastructure gap for any
production target, and it must be closed before real merchant data
is ever loaded.
