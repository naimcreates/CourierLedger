# CourierLedger — Incident Response Runbook

**Owner:** Phase 3, Stream C (Production Operations). **Status:**
Phase 3, 2026-11-02. This runbook is for the operator on duty —
today that is Naim (the founder) — because there is no hosted
alerting, no on-call rotation and no production host yet. Every
procedure below assumes the **local VM layout** (native PostgreSQL
16 cluster `16 main` on 5432, app in `m1/backend`, scripts in
`m1/scripts`) unless it says "production host". Adapt paths when a
production host exists; do not invent one here.

## Status vocabulary (same as PRODUCTION_CONFIGURATION_GUIDE.md)

**Implemented and verified** / **Implemented but not verified** /
**Documented design** / **Blocked by environment** / **Requires
founder approval**. Nothing in this runbook is a claim that
production exists. The project is **NOT production-ready**.

## 0. First principles — read before any incident

1. **Money logic is append-only and synthetic-only.** Never edit,
   delete or "fix" a financial row by hand. If a number looks wrong,
   preserve the evidence and escalate — do not rewrite history.
   (Commitment: synthetic data only unless Naim permits real data.)
2. **Never log or paste a secret.** Passwords, tokens and session
   identifiers do not go in logs, chat messages, tickets or
   screenshots. The runtime-role DB password lives only in the
   gitignored `m1/backend/.env`; read it at runtime, never print it.
   If a secret ever lands in a log, rotate it and treat the log as
   compromised.
3. **Check before you restart.** A restart hides the cause. Capture
   the symptom first: the exact error, the request id
   (`X-Request-ID` response header), the last log lines, the Alembic
   revision, and whether `/api/ready` is 200 or 503.
4. **The database is the source of truth; the app is stateless.**
   Restarting the app loses nothing except in-memory rate-limit
   counters. Restarting the database drops every connection.
5. **There is no alerting.** Nothing pages anyone today. Detection
   is manual: `/api/ready`, the structured logs, and the backup /
   maintenance scripts' exit codes (see §0.1). Setting up alerting
   is a founder decision, not something this runbook can do.

### 0.1 Detection — what to watch, and how (all Documented design unless noted)

| Signal | How to check | Healthy | Unhealthy |
|---|---|---|---|
| App liveness | `curl -s http://127.0.0.1:8000/api/health` | 200, `database_ok: true` | non-200, or `database_ok: false` |
| App readiness | `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/ready` | 200 | 503 (database unreachable — see §1) |
| App logs | stdout of the uvicorn process (structured JSON, one object per line, `request_id` on every line inside a request) | `request_completed` lines, no `unhandled_exception` | `unhandled_exception`, `database_health_check_failed` |
| PostgreSQL up | `pg_lsclusters` | `16 main ... online` | anything else |
| Alembic head | `SELECT version_num FROM alembic_version;` as the owner role | matches the latest migration file in `m1/backend/alembic/versions/` | anything else — see §4 |
| Backup job | exit code of `m1/scripts/backup_db.sh` / `m1/scripts/backup_encrypted.sh` (Stream A owns these scripts — referenced here by name only) | exit 0 + a fresh `.dump` (+ `.sha256`) | non-zero exit, stale or missing dump — see §2 |
| Maintenance job | exit code + the single JSON line of `m1/scripts/maintenance.py` (Stream A owns it) | exit 0, `status: completed` (or `skipped` — a held lock is not a failure) | non-zero exit with a JSON error line |
| Slow queries | `log_min_duration_statement` + `pg_stat_statements` | — | **Not installed yet** (see PRODUCTION_CONFIGURATION_GUIDE.md §14). Until then, slow queries are invisible except through the 30s runtime statement timeout. |

### 0.2 Key locations (local VM)

- App: `m1/backend/` (venv `m1/backend/.venv`, `uvicorn app.main:app`)
- Config / secrets: `m1/backend/.env` (gitignored, 0600 — never print)
- PostgreSQL: cluster `16 main`, data `/var/lib/postgresql/16/main`,
  log `/var/log/postgresql/postgresql-16-main.log`,
  config `/etc/postgresql/16/main/postgresql.conf`
- Scripts: `m1/scripts/backup_db.sh`, `m1/scripts/backup_encrypted.sh`,
  `m1/scripts/verify_restore.sh`, `m1/scripts/maintenance.py`,
  `m1/scripts/provision_db.sh`
- Docs: `PRODUCTION_CONFIGURATION_GUIDE.md`,
  `DATABASE_BACKUP_RECOVERY.md`, `DATABASE_OPERATIONS.md`,
  `MIGRATION_VERIFICATION.md`

---

## 1. Database unavailable

**Symptoms.** `GET /api/ready` → 503 with `database_ok: false`;
`/api/health` → 200 with `database: unavailable`; logs show
`database_health_check_failed`; users see errors on every
database-backed page while the app process itself stays up (that
split is by design — liveness vs readiness).

**Do not** restart the app first — the app is not the problem, and a
restart loses the in-memory rate-limit state for nothing.

1. Confirm the scope. `pg_lsclusters` — is cluster `16 main`
   online? `curl -s http://127.0.0.1:8000/api/health` — what does
   `database` say? One database down vs the whole cluster down are
   different incidents.
2. If the cluster is down, read the tail of the PostgreSQL log
   (`/var/log/postgresql/postgresql-16-main.log`) for the last error
   before touching anything. Common local causes: the VM was
   replaced/restored (this has happened before — the cluster then
   needs re-provisioning, see `m1/LOCAL_VERIFICATION.md`), disk
   full (`df -h /var/lib/postgresql`), or an out-of-memory kill
   (`dmesg | tail`).
3. If the cluster is up but the app cannot connect: check the
   connection facts, not the code. Is the app's `DATABASE_URL`
   pointing at the right host/port/database/role? (It must be the
   **runtime** role — never "fix" a connection failure by switching
   the app to the owner role.) Is the runtime role's password still
   valid? Has the role been locked or its password rotated without
   updating `.env`? Check `pg_stat_activity` as superuser for a
   connection storm (pool is 15 per backend process; `max_connections`
   is 100 locally).
4. If the cluster is down and the log gives no other cause, restart
   it: `pg_ctlcluster 16 main start` (as root / postgres). Then
   re-check `/api/ready` — it must return 200 before you call the
   incident over.
5. If the cluster will not start: **stop and escalate to Naim.**
   Do not hand-edit data files, do not delete `postmaster.pid`, do
   not run `pg_resetwal`. The recovery path is the latest backup +
   `verify_restore.sh` into a *separate* database (§3) — never a
   restore over the live database until the restore has been
   verified.
6. After recovery: record what happened (time, symptom, log lines,
   what fixed it) in the goal's notes, and confirm the Alembic
   revision is unchanged (`SELECT version_num FROM
   alembic_version;`). If the revision changed, that is a §4
   incident, not a §1 incident.

**Escalation:** if the cluster is down for more than 30 minutes, or
the log shows corruption / `pg_resetwal`-level damage, escalate to
Naim immediately — this is a data-loss-risk decision, not an
operator judgement call.

---

## 2. Backup failure

**Symptoms.** `backup_db.sh` or `backup_encrypted.sh` exits
non-zero; no fresh `.dump` (and `.sha256`) appears in the output
directory; or the dump file is present but its SHA-256 does not
match the sidecar.

**Do not** delete old backups to "make room" for a retry, and do not
edit a dump file. Old backups are the only recovery material until
a new one succeeds.

1. Read the script's output first — both scripts are `set -euo
   pipefail`, so the last error line is the cause. Common causes:
   - `pg_dump: error: query failed: ERROR: query would be affected
     by row-level security policy` — the dump ran as the wrong role.
     Dumps **must** run as the postgres superuser (or, in
     production, a dedicated backup role with BYPASSRLS used only
     for dumps). Never "fix" this by running the dump as the runtime
     role or by disabling RLS.
   - Disk full in the output directory (`df -h`) — move older
     dumps to the off-host copy (if one exists) or to another disk;
     never delete the newest good dump.
   - `backup_encrypted.sh` refusing to run — no passphrase: set
     `CL_BACKUP_PASSPHRASE_FILE` (a 0600 file outside the repo) or
     `CL_BACKUP_PASSPHRASE`. Never pass the passphrase as a CLI
     argument and never store it in the repo.
   - Database unavailable — that is a §1 incident; fix the
     database first, then re-run the backup.
2. Re-run the same command once the cause is fixed. A successful run
   prints the dump path, its SHA-256 and its size — verify the
   `.sha256` file matches (`sha256sum -c <file>.sha256`).
3. If the dump succeeds but `verify_restore.sh` (§3) fails on it,
   the backup is **not** a good backup — keep the previous good one
   and treat the new dump as suspect.
4. If two consecutive scheduled runs fail, escalate to Naim: there
   is currently **no backup schedule and no failure alerting**, so a
   silent backup gap is the single largest infrastructure risk
   (see PRODUCTION_CONFIGURATION_GUIDE.md §15). Until a schedule
   and an off-host copy exist, do not load real merchant data.

**Escalation:** any backup failure that is not fixed by one re-run,
or any failure of the *newest good* backup to verify (§3).

---

## 3. Restore operation

**When this applies.** Disaster recovery (cluster lost), or a
scheduled restore drill. A restore is never done "to see if it
works" against the live database.

**Do not** restore over a live database. Ever. Restores always go
into a **separate, fresh** database, and the live database is only
touched after the restore has been verified and Naim has approved
the cutover.

1. Pick the backup: the newest dump whose SHA-256 verifies
   (`sha256sum -c`) **and** which has passed `verify_restore.sh`
   before. If none has, run the drill on the newest dump first and
   treat an unverified dump as suspect.
2. Run the drill (Stream A owns the script; this runbook only
   invokes it):
   `m1/scripts/verify_restore.sh <source-db> <dump-file> [restore-db]`
   It restores into `<source-db>_restore_verify` by default, then
   compares object counts, per-table row counts, the financial
   identities (`V_total = C - delta - V_R + S`, `V_cond = S - V_R`)
   and settlement variance arithmetic, plus a runtime-role RLS probe
   (own tenant visible, other tenant invisible).
3. Read the result:
   - Exit 0: the restore database is dropped automatically and the
     drill passed. Record the date, dump file and result.
   - Non-zero: the restore database is **left in place** for
     inspection and its name is printed. Do not drop it until the
     mismatch is understood. The commonest mismatch classes are
     object-count drift (a migration landed after the dump), row
     drift (the source kept changing during the dump — expected if
     the app was writing; re-run with the app stopped), and
     financial-identity violations (stop — that is a data-integrity
     question for Naim, not an operator fix).
4. Cutover (only after a passed drill **and** Naim's explicit
   approval): stop the app, take a final backup of the damaged
   database if it is at all readable, restore the verified dump
   into the production database name, run `alembic upgrade head`
   as the owner role if the dump predates a migration, re-apply
   `roles.sql` / `rls.sql` / `triggers.sql` only if the dump
   predates them (the dump contains them — check first), then start
   the app and confirm `/api/ready` 200 and the Alembic revision.
5. After any restore: run the restore drill again on a fresh backup
   taken from the restored database, so the new live database is
   itself known-good.

**Escalation:** any `verify_restore.sh` failure, any financial
identity violation, or any cutover decision — all go to Naim.

---

## 4. Migration failure

**Symptoms.** `alembic upgrade head` (or the one-shot `migrate`
service in `docker-compose.production.yml`) exits non-zero; or the
app starts but `alembic_version` does not match the latest file in
`m1/backend/alembic/versions/`; or `alembic check` reports drift.

**Do not** edit `alembic_version` by hand, do not run
`alembic stamp`, and do not delete a migration file. The version
table is the only record of what schema the data matches — forging
it turns a recoverable failure into silent corruption.

1. Capture the exact failure: the full Alembic output (it names the
   migration and the statement that failed) and the current
   revision (`SELECT version_num FROM alembic_version;` as owner).
   Alembic runs each migration in a transaction, so a failed
   migration normally leaves the database at the previous revision
   — verify that before doing anything else.
2. Read the failing migration. Common local causes: the migration
   was written against a different starting revision (another
   stream landed a migration first — check `git log` on
   `m1/backend/alembic/versions/`), a missing prerequisite (roles /
   RLS / triggers not yet applied — the provisioning order in
   `m1/README.md` matters), or a lock (another migration or a
   long-running transaction holding the schema — check
   `pg_stat_activity`).
3. If the cause is ordering (a newer migration file exists that the
   database has not reached, or vice versa): stop, and get the
   migration's author / Naim to decide. Do not re-order files.
4. If the cause is a transient lock: wait for the blocking
   transaction to finish (or, only with Naim's approval, terminate
   the blocker with `pg_terminate_backend` — never terminate
   blindly), then re-run `alembic upgrade head` once.
5. If the migration itself is wrong: fix the migration file, **add
   a test if the failure was a logic error**, and re-run. If the
   migration has already been applied anywhere else (another
   database, another branch), it must be fixed with a *new*
   migration, not by editing the applied one — historical migrations
   are never modified (see `MIGRATION_VERIFICATION.md`).
6. After a successful run: confirm `alembic_version` equals the
   latest file, confirm `/api/ready` is 200, and run the affected
   test file (never the whole suite from this runbook — the
   coordinator runs the full suite).

**Escalation:** any migration that fails twice, any `alembic check`
drift, or any case where the database revision and the files
disagree — all go to Naim. The `alembic check` drift decision is
already open (see `MIGRATION_VERIFICATION.md` §3).

---

## 5. Suspected tenant-isolation issue

**Symptoms.** A user sees another merchant's data (workspaces,
shipments, statements, runs, settlements); or a user sees *nothing*
they should see; or the RLS probe in `verify_restore.sh` fails; or
a log line shows `tenant_context_rejected_malformed_or_missing` /
`user_context_rejected_malformed_or_missing` at unusual volume.

**Treat every report as real until proven otherwise.** Do not ask
the reporter to "try again" and do not dismiss it as a UI quirk.
Tenant isolation is the product's core promise.

1. **Contain first.** If a user can see another tenant's data,
   stop the app (`docker compose stop backend`, or stop uvicorn)
   to prevent further exposure while you investigate. A stopped
   app loses no data. Tell Naim immediately — this is the one
   incident class where escalation comes before diagnosis.
2. Preserve evidence: the reporter's user id and workspace, the
   request id(s) from the response headers, the exact URLs and
   timestamps, and the relevant structured log lines. Do not delete
   or edit any row.
3. Check the cheap causes first:
   - Was the app started with the **runtime** role? (If it was
     started as the owner role, RLS still applies but the role has
     broader grants — check `DATABASE_URL` in the process
     environment.) The app must never run as the owner role.
   - Is `app.current_merchant_id` / `app.user_id` being set from
     server-side session state only? (Code fact: yes, in
     `app/db/session.py` + `app/api/deps.py` — a malformed value
     fails closed to NULL, which matches no rows.) A NULL context
     shows *nothing*, so "sees nothing" points here; "sees
     another tenant" does not.
   - Did a migration or `roles.sql` change land without the matching
     RLS policy? Check `git log` for recent changes to
     `m1/backend/app/db/rls.sql`, `roles.sql`, `triggers.sql` and
     the Alembic versions, and confirm they were applied to the
     database they are suspected on.
4. Reproduce on a scratch database, never on live data: provision a
   scratch DB with the same schema + RLS (`tests/live_pg.py`
   documents the order), create two synthetic tenants, and run the
   exact request that exposed data. The existing live tests
   (`tests/test_runtime_role_live_pg.py`,
   `tests/test_rls_roles_live_pg.py`) are the regression net — run
   the affected file and report the result.
5. If the reproduction confirms a leak: keep the app stopped, do
   not attempt a hot-fix in production, and escalate to Naim with
   the evidence (who saw what, when, and the reproduction). The
   fix is a code/RLS change with a proving test, reviewed before
   it ships — never a manual data edit.
6. If the reproduction shows no leak: record the investigation
   (report, evidence, reproduction result) and restart the app.
   "Sees nothing" with a valid session is usually a membership
   problem (revoked / never-added member) — check
   `merchant_memberships` for that user, do not touch RLS.

**Escalation:** immediately, on any confirmed or suspected
cross-tenant visibility, before diagnosis. This is a
stop-the-app incident.

---

## 6. Unexpected reconciliation variance

**Symptoms.** A run, settlement batch or report shows a variance
that does not match the operator's expectation — a `mismatch`,
`needs_review`, or a non-zero `V_total` / `V_cond` / `S` on data
that "should" reconcile; or the same import reconciles differently
on a re-run.

**Do not** "correct" a variance by editing a result row, a
statement line, or a shipment. An unexplained difference is never
automatically a confirmed loss, and a variance is evidence, not an
error to be cleaned up. (V2 rules: expected, courier-reported and
bank-confirmed amounts stay separate; missing/unverified is
"cannot calculate exactly", never an invented amount or implicit
zero; no match on amount alone.)

1. Identify the exact artefact: run id, batch id, settlement id,
   and the request id of the call that produced it. Read the
   stored findings list for that run — findings for `C`, `V_R`,
   `V_cond`, `S`, `V_total` are stored as a list and none hides
   another.
2. Decompose the variance with the definitions, not with instinct:
   - `C` — collection difference (expected collectible vs reported
     collected). A collection-only discrepancy leaves the
     contractual payout `matched` and the run `open` — that is
     correct behaviour, not a bug.
   - `V_R` — fee variance on the reported basis; `S` — the
     statement's own inconsistency (reported payout minus reported
     collected minus reported deductions). `S` is the statement
     disagreeing with itself; never force it to zero.
   - `V_total` is scenario-only and never triggers a contractual
     payout mismatch; only `V_cond` decides that.
   - `NULL` means "cannot calculate exactly" (missing fee line,
     unconfirmed contractual delivery fee `D_c`, unknown outcome) —
     never read a NULL as zero.
3. Check the inputs before the engine: is the courier rate
   merchant-confirmed with evidence, or assumed? Is `D_c`
   confirmed with an explicit flag **and** a non-empty evidence
   reference (a bare value is not confirmation)? Is the outcome
   confirmed (delivered / partial / return / cancel), or is the
   shipment still in-transit/unknown (expected collectible NULL)?
   Was the same tracking ID matched to more than one shipment
   (ambiguous — stays open for human confirmation)?
4. Check for a re-run difference honestly: runs are idempotent by
   hash (same inputs → same run, replay 200; different hash →
   409). If the inputs changed between runs (new statement file,
   corrected rate, new outcome), a different result is expected.
   Compare the input hashes, not just the outputs.
5. If the inputs are all confirmed and the arithmetic still does
   not follow the V2.2.1 rules: preserve the run, the import batch
   and the statement rows, write down the hand-calculated expected
   values with their working, and escalate to Naim. That is a
   possible engine bug and needs a failing test with synthetic data
   before any fix.
6. If the inputs are incomplete or unconfirmed: the correct outcome
   is `needs_review` / `open` / NULL — record what evidence is
   missing and who can provide it (the merchant), and leave the
   run as it is.

**Escalation:** any case where confirmed inputs produce arithmetic
that contradicts the documented rules (step 5), or any variance on
real merchant data — both go to Naim. Never edit financial rows to
make a variance disappear.

---

## Appendix: the app will not start — quick triage

If uvicorn exits immediately, read the last lines of its stderr
before anything else. The production startup validator
(`app/config.py: validate_production_settings`) fails fast and names
the variable — `DATABASE_URL`, `SECRET_KEY`, `SESSION_SECRET`,
`FRONTEND_URL`, `SESSION_COOKIE_SECURE`, `CREATE_TABLES_ON_STARTUP`
(see PRODUCTION_CONFIGURATION_GUIDE.md §6.2). In development/test
mode the validator does not run. A `RuntimeError` naming a variable
is a configuration fix, not a code incident.
