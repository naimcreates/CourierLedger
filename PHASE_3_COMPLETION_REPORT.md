# CourierLedger — Phase 3 Completion Report

**Phase:** Phase 3 — Production Operations, Recovery & Deployment Readiness
**Date:** 2026-11-01/02 (UTC 2026-11-01 18:00–19:00; Dhaka 2026-11-02 00:00–01:00).
**Remote HEAD after Phase 3:** `ecb6372` at push time (Phase 2 HEAD was
`b24d4bf9672ec5dbd3d28f614a98bb7a8ace7101`). A one-line hash-correction
commit follows it — see `git log` for the current HEAD.
**Verdict:** Phase 3 is complete on the evidence below. The project is **NOT
production-ready** — see §9. No deployment was made, no paid infrastructure
was selected, no merchant data was used.

Status labels used throughout this report (and in every Phase 3 deliverable):

- **Implemented and verified** — built and proven by an actual run on this VM.
- **Implemented but not verified** — built, but no run could prove it here.
- **Documented design** — a design/record, not a running behaviour.
- **Blocked by environment** — this VM cannot prove it.
- **Requires founder approval** — Naim must decide before it can proceed.

---

## 1. What Phase 3 set out to do

Phase 2 left five open items that Phase 3 was chartered to close: (1) no backup
schedule / off-host copy / WAL archiving; (2) the migrator-role privilege
decision; (3) the `alembic check` drift decision (17–18 ops); (4) no purge
scheduler for `purge_expired_auth_rows`; (5) no production compose file and no
dev/test/prod separation. Phase 3 also had to verify production configuration
behaviour live, attempt `pg_stat_statements`, and write the incident runbook.

Work was done in three parallel streams (A: backup + maintenance; B: migrator
role + drift; C: production config + Docker + observability), then synthesised
and re-verified by the coordinator with a full independent test run.

---

## 2. Changes made, by workstream

### Workstream 1 — Backup and disaster recovery (Stream A)

**Implemented and verified**

- `m1/scripts/backup_encrypted.sh` (new): canonical backup — `pg_dump -Fc`
  piped straight into `gpg` symmetric AES-256 (no plaintext on disk),
  `pg_dumpall --globals` alongside, JSON sidecar (timestamp, database,
  Alembic revision, PG version, plaintext + encrypted size/SHA-256,
  `verified` flag), per-run verification (SHA-256 both layers + decrypt +
  `pg_restore -l`), retention (keep 7 / 30 days, env-configurable), 0700
  directory / 0600 files, JSON log lines, non-zero exit on failure,
  `--dry-run` and `--check` modes, same-minute collision guard.
- `m1/scripts/verify_restore.sh` (extended, additive only): accepts
  encrypted `*.dump.gpg` — sidecar SHA-256 check, decrypt to a 0600 temp
  file, wipe on exit; all original comparisons untouched.
- `DATABASE_BACKUP_RECOVERY.md` rewritten with the five status buckets.
- `.gitignore`: added `m1/backups/` (the Phase 2 doc claimed it was
  ignored; the entry did not exist — fixed).

**Backup evidence (real runs on the live cluster)**

- Drill 1 (Alembic 0006, real synthetic rows): encrypted dump **263,286
  bytes** (plaintext 263,101), globals 2,015 bytes, ~2 s; restore drill
  ~5 s, exit 0 — object counts identical, row counts identical (8 users,
  8 reconciliation_results, 4 variance calcs, 56 audit_logs), **V_total 0,
  V_cond 0, variance-calc 0 violations**, RLS probe 0 merchants, scratch
  DB dropped. `--check` passed.
- Drill 2 (Alembic 0007, re-provisioned empty DB): encrypted dump
  **226,294 bytes**, same checks pass — identities vacuous (no business
  rows), stated as such in the doc.
- Retention proven: 10 fake old sets + 1 real run → age-pruned the
  >30-day set, count-pruned down to exactly 7 sets; same-minute re-run
  refused by the collision guard.

**WAL / PITR verdict — not implemented, limitation documented.**
`archive_mode=off`, `archive_command=(disabled)`, `wal_level=replica`; no
archive settings in `postgresql.conf`. PITR is supportable but not
implemented: it needs `archive_mode=on` + `archive_command` + a cluster
restart (postmaster parameter) + `pg_basebackup` + archive storage + a
recovery drill. It was not enabled because a restart would have disrupted
the other streams' live tests and same-disk archiving is not disaster
recovery. **RPO on daily dumps alone is up to 24 h.** RPO ≤ 1 h remains a
target, not an achievement.

**Still blocked / not verified (backup):** globals *restore* not run
(artifact created + checksummed only); off-host copy blocked (no host or
provider); failure alerts need a founder-chosen mailbox/webhook; monthly
retention tier not built; `postgresql.conf`/`pg_hba.conf` not backed up;
local backup storage is **not** off-site disaster recovery.

**Container-replacement incident during this work (~18:30 UTC).** The VM
container was replaced mid-task: PostgreSQL, both DBs, and `/etc` wiped.
Lost: the `courierledger_app` synthetic data and the only copy of the
backup passphrase (it lived in `/etc`), making the 18:27 encrypted backup
undecryptable — deleted, evidence retained in the doc. Recovered per
`m1/LOCAL_VERIFICATION.md` §1a (cluster recreated UTF8, PG 16.15; both
DBs re-provisioned at Alembic 0007 head). The new passphrase lives under
`~/.config` (home persists; `/etc` does not) and the full evidence chain
was re-run. One transient `password authentication failed` on the first
re-provision attempt did not recur; cause not established, recorded
honestly.

### Workstream 2 — Database migration role (Stream B)

**Implemented and verified — decision: Option 2.**
`courierledger` (LOGIN owner, not superuser, no BYPASSRLS, owns both DBs +
all 33 tables) is the live migrator; `courierledger_migrator` stays
reserved (NOLOGIN, 0 grants, 0 objects, no password verifier). Rationale
(recorded in `roles.sql` header + `DATABASE_OPERATIONS.md` §3.1): every
stated requirement is already met by `courierledger` as migrator; Option 1
(REASSIGN OWNED) would add a second LOGIN credential to store/rotate plus
changes to the provisioning script, live-test harness and both CI
workflows, for no live risk reduction on this single-VM local-dev setup
where all credentials live in one gitignored `.env`. The operative
boundary — app as runtime, migrations as owner — is enforced by
configuration and proven by tests. The reserved role is the hook for a
future production split (fresh vault password + `MIGRATION_DATABASE_URL` +
REASSIGN OWNED, no schema change).

**Live proof (rebuilt cluster, `courierledger_app`, via `pg_authid`):**
`courierledger` LOGIN / not superuser / no BYPASSRLS, owns 33 tables;
runtime owns 0, 74 privilege rows, 0 DELETE; migrator 0 privilege rows;
maintenance DELETE only on `idempotency_keys`; FORCE RLS 24 / enabled 26.
**Negative probes as runtime:** CREATE TABLE, ALTER TABLE, DROP TABLE,
DELETE FROM `idempotency_keys`, DISABLE RLS — all 42501. `SELECT
merchants` without context: 0 rows. `rolbypassrls=false`. Login as
migrator/maintenance fails. `.env` `DATABASE_URL` names
`courierledger_runtime`; only `alembic/env.py` reads
`MIGRATION_DATABASE_URL`.

**Two findings.**

- **The `pg_roles` trap — corrects a Phase 2 record.** Phase 2 said both
  NOLOGIN roles "carry password verifiers (verified live)". That check
  used `pg_roles`, whose `rolpassword` is a constant `'********'` mask for
  every role — it cannot establish a verifier exists. The correct source
  is `pg_authid` (superuser-only). `roles.sql` now sets `PASSWORD NULL` on
  both NOLOGIN roles on every application; post-Phase-3 `pg_authid` shows
  both NULL, both LOGIN roles hold SCRAM verifiers. Whether pre-Phase-3
  verifiers existed is unknowable retroactively and is recorded as
  unknown, not asserted.
- **`provision_db.sh` fresh-cluster bug found and fixed:** on a truly
  fresh cluster, `rls.sql` failed with `role "courierledger_runtime" does
  not exist` — step 1 only pre-created the NOLOGIN roles. Step 1 now also
  creates `courierledger_runtime` (LOGIN NOBYPASSRLS, **no password**
  until step 5).

**Local-dev DB password rotated during the rebuild.** A helper echoed a
prefix of the old value into a tool log, so both LOGIN roles now carry a
fresh random value stored only in gitignored `m1/backend/.env`; the old
value no longer authenticates. The coordinator updated the GitHub repo
secret `CI_POSTGRES_PASSWORD` to the new value (via `gh secret set`,
piped, never printed). No production credential was ever involved.

### Workstream 3 — Alembic schema drift (Stream B)

**Implemented and verified.** Baseline: `alembic check` exit 255,
**21 ops** on a correctly provisioned database at head 0006
(20 on an Alembic-only DB; the difference is the RLS-layer index). Each
op re-derived against `pg_constraint`/`pg_indexes`/`pg_get_constraintdef`:

| Ops | Classification |
|---|---|
| 1× `add_constraint uq_templates_id` | Representation — redundant UNIQUE on a PK column. Fixed metadata-only (removed from the model). |
| 3× `remove_index` (the 0006 indexes) | Migration/metadata gap. Fixed metadata-only (`index=True` on the 3 columns). |
| 3× `remove_constraint` (`users.email`, `user_sessions.token_hash`, `password_reset_tokens.token_hash`) | Representation — `unique=True`+`index=True` collapses to a unique index only in metadata, while 0001 created named unique constraints. Fixed metadata-only (explicit `UniqueConstraint`s with the live names). |
| 1× `add_constraint uq_memberships_merchant_id` | **The ONE genuine mismatch — fixed by migration 0007.** Declared in `auth_models.py`, omitted by 0001. |
| 12× `remove_fk`+`add_fk` (6 auth-table FKs) | Representation, **allow-listed**: live `confdeltype='a'` (NO ACTION, 0001 default) vs declared RESTRICT; identical behaviour for non-deferrable FKs. Recreating them would be DDL churn with exclusive locks for zero change — deliberately not migrated; model keeps RESTRICT per spec. |
| 1× `remove_index ix_merchant_memberships_user_active` | Manually managed RLS-layer index (`rls.sql`), **allow-listed** by design. |

**Correction applied:** migration **0007**
(`0007_phase3_missing_membership_unique.py`) adds only
`uq_memberships_merchant_id` (additive, always succeeds — `id` is the PK).
Model alignment is metadata-only (naming convention reproducing
PostgreSQL's auto-names, verified live before adoption; no DDL).
**Residual allow-list after Phase 3: exactly 13 ops provisioned (12 FK +
1 RLS index), 12 Alembic-only** — verified on an Alembic-only scratch DB,
both working DBs, and a freshly provisioned scratch DB. The allow-list is
pinned by a live test that fails on any other op, count, or a clean pass.
`alembic check` still exits 255 by design; no differences were
suppressed.

**Migration test outcomes (real PostgreSQL 16.15):** A (empty → head)
PASS — fresh DB via `provision_db.sh` → 0007; B (0006 → 0007) PASS — both
working DBs upgraded as the migrator; C (repeat no-op) PASS — `upgrade
head` re-run on app + dev, exit 0; second `provision_db.sh` run identical
counts; D (fresh provisioning order) PASS — scratch DB head 0007, 33
tables, 46 policies, 15 functions, 22 triggers, 5 couriers. (5)
RLS/triggers/indexes/constraints after correction: PASS — 33/46/15/22,
133 indexes (was 132), 265 constraints (was 264), all 3 0006 indexes,
RLS index, FORCE 24, 6 NO-ACTION FKs, 79 RESTRICT FKs. (6) No schema
change from app startup: PASS — app as runtime served health 200 /
ready 200 / register 201 / login 200 / workspace-create 201; `alembic
check` identical before vs after. 0007 downgrade (single `DROP
CONSTRAINT`) implemented but **not executed live** — no data hazard,
stated as not verified.

### Workstream 4 — Maintenance scheduling (Stream A)

**Implemented and verified.** `m1/backend/app/services/maintenance_service.py`
(new): wraps the real `purge_expired_auth_rows` (B-F19), adds an advisory
lock (key `20261102`), dry-run counts, PostgreSQL-only guard. Only
deletion is expired sessions + expired/used reset tokens; never touches
reconciliation/settlement/audit history; no idempotency purge (CL-19 has a
grant but no purge function exists). `m1/scripts/maintenance.py` (new
CLI): `--db` repeatable, `--dry-run`, one JSON line per DB, exit 0/2;
resolves the owner credential from `.env` at runtime, never prints it.
`m1/scripts/systemd/` (new): daily backup 02:30 UTC + hourly maintenance
timers, install docs, cron fallback documented only. Scheduler choice:
`cron`/`crond` do not exist on this VM and systemd is init, so systemd
timers are primary — justified in `systemd/README.md`. Both timers
**enabled**: backup next 02:30 UTC, maintenance hourly; `systemctl start`
on both proved the timer path (`Result=success`, exit 0, journal shows a
backup set created and maintenance `completed` 0/0).

**Maintenance evidence (live run on `courierledger_dev`, seeded 2 expired
+ 1 live session; expired + used + live tokens):** dry-run 2/2, real run
completed 2/2, second run 0/0 (idempotent), multi-DB run 0/0 both, unknown
DB → JSON error, exit 2; only live rows remained; synthetic user removed
afterwards (dev back to 0/0/0). **Tests: 4 passed, 0 failed**
(`test_maintenance_live_pg.py`, run twice).

### Workstream 5 — Production configuration (Stream C)

**Implemented and verified (live evidence for each item):**

- **(a) Startup validation — implemented and verified.** New
  `validate_production_settings()` in `config.py`, called from `main.py`
  when `ENVIRONMENT=production`. Fails fast with `RuntimeError` naming
  the variable: `DATABASE_URL` (must be `postgresql://`, no `CHANGE_ME`
  — an unset URL falls back to the compiled-in placeholder default, so
  "not set" and "placeholder" fail the same check), `SECRET_KEY` /
  `SESSION_SECRET` (not placeholder), `FRONTEND_URL` (not the dev
  default), `SESSION_COOKIE_SECURE=true`,
  `CREATE_TABLES_ON_STARTUP=false`. Verified by subprocess import probes
  (each case exits 1 naming the variable) and 8 unit tests. **Gap
  closed:** before this stream, production start with no `DATABASE_URL`
  exited 0 silently on the placeholder URL; now exits 1 naming
  `DATABASE_URL`. The validator is value-based (env or `.env` file, as
  `config.py` has always allowed), not process-environment-only.
- **(b) Secure cookie + CSRF — verified live.** Real production-mode
  server on the local cluster: `GET /api/auth/csrf` returned
  `Set-Cookie: cl_csrf=…; SameSite=lax; Secure` (no `HttpOnly`, correct
  for the CSRF cookie); session cookie is `HttpOnly` + same `Secure`
  flag in code; HSTS `max-age=31536000; includeSubDomains` present in
  production only; start with `SESSION_COOKIE_SECURE=false` exits 1.
- **(c) Proxy/HTTPS — documented design, code facts verified.** App
  assumes a TLS-terminating reverse proxy in front, uvicorn trusting
  `X-Forwarded-*` only from the proxy's address (`--proxy-headers
  --forwarded-allow-ips=${TRUSTED_PROXY_IPS:?}` in the production
  compose; **no** `--proxy-headers` in dev compose/Dockerfiles, no
  `TrustedHostMiddleware`, rate limiter keys on direct peer
  `request.client.host`). No proxy product or host chosen — founder
  decision. Rate limiter is process-local (multi-worker needs a shared
  store, e.g. Redis — founder decision).
- **(d) API docs disabled — verified live.** Production mode: `/docs`,
  `/redoc`, `/openapi.json` all → **404**, both with DB up and DB down.
- **(e) DB timeouts — verified live.** Runtime role 30s/60s/5s,
  maintenance 10min/10min/30s (read from live `SHOW` as runtime role +
  `pg_roles.rolconfig` as superuser); owner/migrator have none.
- **(f) Readiness/liveness — verified live.** DB up: `/api/health` 200
  `database: postgresql`, `/api/ready` 200 `ready`. DB down (DATABASE_URL
  pointed at dead port 127.0.0.1:5999 — cluster **not** stopped):
  `/api/health` 200 `database: unavailable, database_ok: false`,
  `/api/ready` **503** `not_ready`.
- **(g) Graceful shutdown — verified live.** SIGTERM → log sequence
  `Shutting down → Waiting for application shutdown → Application
  shutdown complete → Finished server process` in ~100 ms. Exit status
  is **-15**, not 0 — that is uvicorn 0.54's intended behaviour
  (`capture_signals` re-raises the signal after graceful shutdown;
  orchestrators see exit 143), not a crash. Test asserts the log
  sequence + termination, accepts 0 or -15.
- **(h) Migrations separated from startup — verified.** No Alembic call
  anywhere in `app/main.py`/`app/db/session.py` (grep); Alembic revision
  was 0006 before and after a production-mode run against
  `courierledger_app` (Stream C ran before migration 0007 landed; the
  coordinator re-verified the invariant — no Alembic call in the startup
  path — against the final tree); schema changes run only via the
  one-shot `migrate` service.

**Configuration files created:** `m1/.env.production.example`
(placeholders only) and `m1/docker-compose.production.yml` (standalone
production compose, **UNVERIFIED** — see Workstream 6). `.gitignore`
gained `!.env.production.example` so the template is committable.

### Workstream 6 — Docker and deployment verification (Stream C)

**Blocked by environment — not verified.** `which docker` → not found;
`docker version` → command not found. Binary absent (removed by
container replacement); before that 29.1.3 with `runc create failed:
setns`. **`docker compose up` has never run here.** Nothing is claimed
from YAML parsing alone (production file only checked with
`yaml.safe_load`: services `backend`/`db`/`migrate`, volume
`courierledger_pgdata_prod`, no published DB port).

Production compose is a **standalone** file (run with `-f` alone): `db`
(no published port), one-shot `migrate` (`alembic upgrade head`, owner
role, `profiles: ["migrate"]`), `backend` (`ENVIRONMENT=production`,
loopback-only 8000, `--proxy-headers`, healthcheck on `/api/ready`).
**No frontend service** — the containerised frontend still cannot proxy
`/api` (`vite preview`); frontend hosting is a founder decision. Backend
Dockerfile HEALTHCHECK now probes `/api/ready` (was `/api/health`).
Exact future-environment commands (build → provision → `run --rm
migrate` → `up -d` → readiness → persistence across restart/down-up →
role-separation checks → test suites) are written in
`PRODUCTION_CONFIGURATION_GUIDE.md` §13.3. **B-2 stays open** until they
run green.

### Workstream 7 — Observability and incident response (Stream C)

**`pg_stat_statements` — verdict: NOT enabled, deliberately.** Verified:
not installed (no extension row; `shared_preload_libraries` is empty),
but the library file **is present** on disk and `postgresql.conf` is
postgres-writable. Enabling needs a cluster restart, which would drop
every connection and interrupt the other Phase 3 streams' live work on
this shared VM — so it was **not** restarted. Exact steps for a
maintenance window / first production provisioning are in
`PRODUCTION_CONFIGURATION_GUIDE.md` §14 (add to
`shared_preload_libraries` + `log_min_duration_statement = 1000`,
restart, `CREATE EXTENSION`, verify). `log_min_duration_statement` is
currently -1 (disabled); setting it to 1000 needs only a reload.

**Logging audit — CLEAN.** All 36 `logger.*` calls in `app/` read. **No
passwords, tokens, session identifiers, customer information, or
financial amounts are logged anywhere** — extras carry only ids/counts/
statuses. Raw reset token never logged (only
`password_reset_token_issued` with `user_id`). One item to know:
`login_rate_limited` logs the normalised **email address** — the account
identifier of a security event, not a secret; the only user identifier
reaching the logs. No `print()` in `app/`. Request-ID propagation
verified live (client ids echoed only if they match
`[A-Za-z0-9._-]{1,128}`, else replaced with UUID4).

**Incident runbook — created.** `INCIDENT_RESPONSE_RUNBOOK.md`: six
procedures, each with symptoms, exact checks, steps, what-NOT-to-do, and
escalation: (1) Database unavailable (confirm scope → read PG log →
never "fix" by switching the app to the owner role → restart only if the
log gives no other cause; `pg_resetwal`/hand-editing data files
forbidden); (2) Backup failure (read script output, RLS-role cause,
disk-full, missing passphrase; never delete the newest good dump; 2
consecutive failures → escalate); (3) Restore operation (always into a
*separate* DB via `verify_restore.sh`; non-zero leaves the restore DB in
place; cutover only after a passed drill + Naim's approval); (4)
Migration failure (never hand-edit `alembic_version`/`stamp`/delete a
migration; check revision + output first); (5) Suspected tenant-isolation
issue (**stop the app first, escalate to Naim immediately, before
diagnosis**; reproduce only on a scratch DB); (6) Unexpected
reconciliation variance (never edit a financial row; decompose
C/V_R/S/V_total/V_cond per V2.2.1; NULL is "cannot calculate", never
zero). Detection table notes there is **no alerting** and no backup/
maintenance schedule yet — the timers in Workstream 4 now exist for the
local VM; alerting still needs a founder-chosen channel.

**Disclosure (Stream C, no secrets reproduced):** during probing, two
values appeared in tool outputs only (never written to any file; the
stream's files scan clean): a psycopg error message echoed the full
`DATABASE_URL` including the runtime-role password, and an environment
dump showed the VM egress-proxy credential URL. The runtime-role password
has since been rotated (Workstream 2 rebuild — see above); the egress
proxy credential belongs to the VM, not to CourierLedger, and is not
stored in the repo.

---

## 3. Test results (coordinator's independent full run)

| Suite | Result |
|---|---|
| Backend pytest, full live suite under `courierledger_runtime` | **439 passed, 4 skipped, 0 failed** in 270.05s (28 warnings, Starlette/httpx deprecation only). Baseline 410 + 33 new tests = 443 total; 439 + 4 = 443, reconciled exactly. The 4 skipped tests are the new maintenance live tests (`test_maintenance_live_pg.py`) — they skip by design in the runtime-role run because the purge needs DELETE on `user_sessions`/`password_reset_tokens`, which no application role holds; run separately with `CL_LIVE_PG_ROLE=courierledger` (owner password from `.env`) they pass: **4 passed**. A second full run confirmed 439/4/0 (357.45s). |
| Frontend production build | **pass** (2.92s) |
| Frontend Vitest | **50 passed, 0 failed** (8 files) |
| Playwright e2e | **3 passed, 0 failed** (48.6s, first try) |
| P0 prototype (read-only) | **47 passed, 0 failed** |

New tests added in Phase 3 (all pass):

| File | Tests |
|---|---|
| `tests/test_maintenance_live_pg.py` (new) | 4 live (maintenance job: dry-run counts, real run, idempotency, multi-DB) |
| `tests/test_migration_phase3_live_pg.py` (new) | 12 live (drift allow-list pin, migrator-role probes, migration A–D) |
| `tests/test_production_config.py` (new) | 15 (8 validator unit, 5 subprocess import, 1 DB-down readiness, 1 live production-mode) |
| `tests/test_migration_sql.py` (+2) | 2 new static tests |

Two pre-existing tests were updated for deliberate Phase 3 changes (head
assertion 0006→0007 in `test_infrastructure_phase2_live_pg.py` and
`test_runtime_role_live_pg.py` — both files say the assertion "must move
with the next migration"; behaviour under test unchanged). One Phase 1
fixture was changed (`test_security_phase1.py` `_prod_subprocess` now uses
a dead-port `postgresql://` URL + explicit
`FRONTEND_URL=https://app.example.invalid` because production validation
refuses SQLite and the dev-default origin; assertions unchanged). No test
removed, skipped, or weakened.

**Test-run notes the coordinator verified.**

- Stream A reported `test_security_phase11.py` showing 10 failed / 3
  errors in the dirty tree vs 5 failed / 8 passed at a clean `b24d4bf`
  worktree baseline. The coordinator's full-suite run is green (439/4/0),
  so those were artefacts of running test files standalone and/or
  concurrently with other streams' pytest processes: Stream C proved
  that two concurrent pytest processes in this backend directory share
  the fixed `/tmp/cl_m1_test.db` SQLite file and one process's
  `_fresh_db` fixture drops tables mid-test in the other (observed
  12-failure run with `no such table: user_sessions`). Full suites must
  be run with no other pytest process active. Stream B also noted a
  pre-existing flake: running two live-PG modules in one pytest
  invocation errors the first test of whichever module runs second at
  fixture setup (shared scratch DB name + port 8001 in `live_pg.py`);
  each file passes alone, and the full suite passes — recorded, not
  fixed in Phase 3.
- The full-suite result above is a clean solo run of the final tree.

**Not run:** GitHub Actions CI (billing lock — every run fails in ~3 s
with no runner and no logs; not a code failure); Docker build / `docker
compose up` (B-2 — no Docker binary on this VM); `npm audit` (egress
blocked).

---

## 4. Files changed

Commit history (logical, reviewable units — see §8 for hashes):

- **Backup + maintenance:** `m1/scripts/backup_encrypted.sh` (new),
  `m1/scripts/maintenance.py` (new),
  `m1/backend/app/services/maintenance_service.py` (new),
  `m1/backend/tests/test_maintenance_live_pg.py` (new),
  `m1/scripts/systemd/` (new: 2 services, 2 timers, README),
  `m1/scripts/verify_restore.sh` (encrypted-input support, additive),
  `DATABASE_BACKUP_RECOVERY.md` (rewritten), `.gitignore`
  (`m1/backups/`).
- **Migrator role + drift:** `m1/backend/alembic/versions/0007_phase3_missing_membership_unique.py`
  (new), `m1/backend/tests/test_migration_phase3_live_pg.py` (new),
  `m1/backend/app/db/roles.sql` (Option 2 record + `PASSWORD NULL`),
  `m1/backend/app/db/auth_models.py` (naming convention, explicit
  `UniqueConstraint`s), `m1/backend/app/db/models.py` (`index=True`,
  redundant constraint removed), `m1/scripts/provision_db.sh`
  (migrator naming, head 0007, fresh-cluster runtime pre-creation),
  `MIGRATION_VERIFICATION.md` (rewritten),
  `DATABASE_OPERATIONS.md` (§3–§3.5 role model + §0/§7 head-0007
  refresh by the coordinator),
  `m1/backend/tests/test_infrastructure_phase2_live_pg.py`,
  `m1/backend/tests/test_runtime_role_live_pg.py` (head 0006→0007),
  `m1/backend/tests/test_migration_sql.py` (+2).
- **Production config + Docker + observability:**
  `INCIDENT_RESPONSE_RUNBOOK.md` (new),
  `m1/docker-compose.production.yml` (new, UNVERIFIED),
  `m1/.env.production.example` (new, placeholders only),
  `m1/backend/tests/test_production_config.py` (new),
  `m1/backend/app/config.py` (`validate_production_settings()`),
  `m1/backend/app/main.py` (validator call in production),
  `m1/backend/Dockerfile` (HEALTHCHECK now `/api/ready`),
  `m1/backend/tests/test_security_phase1.py` (fixture only),
  `PRODUCTION_CONFIGURATION_GUIDE.md` (rewritten),
  `m1/README.md` (one line: healthcheck now `/api/ready`),
  `.gitignore` (`!.env.production.example`).
- **Coordinator synthesis:** `INFRASTRUCTURE_TEST_REPORT.md` (Phase 3
  section added), `PHASE_3_COMPLETION_REPORT.md` (this file),
  `m1/IMPLEMENTATION_CHECKLIST.md` (Phase 3 section), `DATABASE_OPERATIONS.md`
  (head-0007/count refresh). GitHub repo secret `CI_POSTGRES_PASSWORD`
  updated to the rotated local-dev value (via `gh secret set`, piped,
  never printed).

---

## 5. Backup and restore evidence (summary)

- Encrypted backup + restore drill passed twice on the live cluster
  (263,286 bytes with real synthetic rows; 226,294 bytes after the
  rebuild on an empty DB): object counts identical, row counts identical,
  **V_total 0, V_cond 0, variance-calc 0 violations**, RLS probe correct,
  scratch DBs dropped.
- Retention proven (10 fake old sets → exactly 7 remain).
- Globals dump created + checksummed; globals *restore* not run.
- No off-host copy, no WAL archiving, no PITR, no failure alerts —
  see §2 and §9.

---

## 6. Migration and privilege evidence (summary)

- Migration 0007 applied to both working DBs (head 0007, 33 tables, 46
  policies, 133 indexes, 265 constraints); A–D + (5) + (6) all PASS
  (see §2, Workstream 3).
- Migrator decision Option 2, proven live: runtime owns 0 tables, 0
  DELETE, `rolbypassrls=false`; negative probes as runtime all 42501;
  migrator role 0 grants / 0 objects / no password verifier
  (`pg_authid`-verified).
- Drift: 21 ops at 0006 → 1 genuine fix (0007) + 13-op pinned
  allow-list; `alembic check` still exits 255 by design.

---

## 7. Docker verification status

**Not verified.** No Docker binary on this VM; `docker compose up` has
never run here. B-2 remains open. Exact commands for a container-capable
environment are in `PRODUCTION_CONFIGURATION_GUIDE.md` §13.3.

---

## 8. Commit hashes and remote verification

- `80feda5` — Phase 3: encrypted scheduled backups + maintenance scheduling
- `909953e` — Phase 3: migrator role decision (Option 2) + Alembic drift + migration 0007
- `28a3bf4` — Phase 3: production configuration validation + production compose + incident runbook
- `ecb6372` — Phase 3: completion report + test report + checklist
  (coordinator synthesis).

Remote HEAD: `ecb6372` at push time (a one-line hash-correction commit
follows it — see `git log` for the current HEAD). Working tree clean
after push. No force-push, no history rewrite. The push triggers a
billing-locked canonical CI run (expected, recorded, not chased).

## 9. Remaining blockers

- **Backup/DR:** no off-host copy, no WAL archiving / PITR, no backup
  failure alerts, no monthly retention tier, globals restore untested,
  `postgresql.conf`/`pg_hba.conf` not backed up. RPO ≤ 1 h unachievable
  without WAL archiving.
- **Docker (B-2):** no Docker on this VM; production compose UNVERIFIED.
- **Observability:** `pg_stat_statements` not enabled (needs a restart);
  `log_min_duration_statement` still -1; no hosted error reporting,
  metrics, or tracing; no alerting.
- **Standing blockers (unchanged):** OD-12, OD-16, OD-17; ADR 1.0
  missing; GitHub billing lock (canonical CI never runs); password-reset
  provider not chosen (log-only reset must never ship);
  market-validation gates not run; no merchant-confirmed courier
  evidence; no production host; no reverse proxy; no frontend hosting
  decision; rate limiter is process-local (multi-worker needs a shared
  store).
- **Founder decisions required:** backup failure-alert channel
  (mailbox/webhook); off-host backup host/provider; WAL archiving /
  PITR investment; reverse proxy product + frontend hosting; email
  provider for password reset; shared rate-limit store if multi-worker;
  the stale queued self-hosted CI runs (cancel them or remove
  `ci-selfhosted.yml` — still pending from Phase 1.1).

## 10. Is Phase 3 complete?

**Yes — Phase 3 is complete on its own terms.** Every workstream
delivered what it could prove on this VM, and everything it could not
prove is labelled as such (not verified / blocked by environment /
requires founder approval) in the deliverables above. The project is
**NOT production-ready**; the remaining blockers in §9 are real and
unchanged by this phase. No deployment, no market validation, and no
Phase 4 work were started.
