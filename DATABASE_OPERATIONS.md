# CourierLedger — Database Operations

**Status:** Phase 2 (2026-11-01). Local development build; **NOT
production-ready**. This document describes how the PostgreSQL
database is operated today on the local VM, what is verified live,
and what is still a recommendation. Nothing here claims a production
deployment exists — there is no production host yet.

Related: `DATABASE_BACKUP_RECOVERY.md` (backups, restore drills,
RPO/RTO), `INFRASTRUCTURE_DESIGN.md` (architecture + Docker),
`MIGRATION_VERIFICATION.md` (migrations A–D, drift),
`PRODUCTION_CONFIGURATION_GUIDE.md` (production config checklist),
`INFRASTRUCTURE_TEST_REPORT.md` (actual test evidence).

---

## 1. Inventory (verified live 2026-11-01)

| Item | Value (live) |
|---|---|
| PostgreSQL | 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1), cluster `16 main`, port 5432, online |
| Encoding / locale | UTF8 / C.UTF-8 on every database (required — see §2) |
| Timezone | Etc/UTC (server); app stores TIMESTAMPTZ everywhere |
| Databases | `courierledger_app` (working), `courierledger_dev` (working), `postgres` (system) |
| Alembic head | **0007** (Phase 3: 0007 adds `uq_memberships_merchant_id`; was 0006 after Phase 2) |
| Tables | 33 per database (32 + `alembic_version`) |
| RLS policies | 46 per database |
| Functions | 15 (4 context helpers + `create_workspace` + 10 `fn_*` trigger functions) |
| Triggers | 22 (17 `CREATE TRIGGER` + 5 deferred `CONSTRAINT TRIGGER`: CL-01/CL-13/CL-16/CL-10) |
| Indexes | 133 per database (132 before migration 0007) |
| Constraints | 265 per database (public-schema `pg_constraint`: 94 CHECK, 85 FK, 33 PK, 5 trigger, 48 UNIQUE) |
| RLS | Enabled on 26 tables; **FORCE on 24**; NO FORCE on `merchants` + `merchant_memberships` only (B-1 design) |
| Seed | 5 courier codes (`couriers`) |
| Rows (app DB) | 244 (synthetic dev data only) |

## 2. Encoding and locale — the rule that must not be broken

Every database **must** be created `--encoding=UTF8
--locale=C.utf8`. A cluster recreated with the default SQL_ASCII
encoding breaks psycopg (it returns `bytes`) and Alembic — this
happened in M10 and is the single most-repeated environment trap in
this project. The container/VM has been replaced at least five times;
each replacement wiped PostgreSQL entirely and it was rebuilt from
apt packages. Verify after any rebuild:

```sql
SELECT datname, pg_encoding_to_char(encoding), datcollate, datctype
FROM pg_database WHERE datistemplate = false;
-- expect: UTF8, C.UTF-8, C.UTF-8 on every row
```

## 3. Roles (verified live, not just read from `roles.sql`)

| Role | Login | Super | BypassRLS | Owns | Grants (live) |
|---|---|---|---|---|---|
| `postgres` | yes | yes | yes | `postgres` DB | superuser |
| `courierledger` | yes | no | no | both app DBs + all 33 tables in each | owner |
| `courierledger_runtime` | yes | no | **no** | nothing | 74 privilege rows; SELECT/INSERT/UPDATE per `roles.sql`; **0 DELETE**; column UPDATE only on `reconciliation_runs(status, completed_at)` |
| `courierledger_migrator` | **no** | no | no | nothing | **0 grants** — reserved, not in service (see §3.1); **no password verifier** (Phase 3, see §3.2) |
| `courierledger_maintenance` | **no** | no | no | nothing | SELECT on all 33 tables; DELETE only on `idempotency_keys`; narrow UPDATEs; no financial-history mutation; **no password verifier** (Phase 3, see §3.2) |

### 3.1 The migrator question — DECIDED Phase 3 (2026-11-02): Option 2

The design once said `courierledger_migrator` owns the schema and
runs Alembic. Live reality, verified: it owns 0 objects and holds
0 grants; live migrations run as `courierledger` (the LOGIN
owner). **Decided: keep it that way — Option 2. `courierledger`
is the live migrator; `courierledger_migrator` stays reserved
(NOLOGIN, no password verifier, no grants).** Full evaluation
in `app/db/roles.sql` (header) and `MIGRATION_VERIFICATION.md`
§5; the short version:

- Every stated requirement is already met by `courierledger` as
  migrator: runtime holds no migration privilege, owns nothing,
  cannot BYPASSRLS; FORCE RLS stays enabled (24 tables);
  migration privilege is sufficient (owner); maintenance stays
  restricted; no app connection uses a superuser.
- Option 1 (REASSIGN OWNED to the migrator) would add a second
  LOGIN credential to store/rotate (vault, `.env`, CI secret)
  and force REASSIGN OWNED on every database plus changes to the
  provisioning script, the live-test harness and both CI
  workflows, while buying no live risk reduction on this
  single-VM local-dev setup, where all credentials already live
  in one gitignored `.env` held by one person. The operative
  boundary — app connects as runtime, migrations run as owner —
  is enforced by configuration (`DATABASE_URL` names the runtime
  role; `alembic/env.py` reads `MIGRATION_DATABASE_URL` only)
  and is proven by the Phase 3 live tests, not by the role's
  name.
- The reserved role is the hook for a future production split:
  that split is a deployment change (fresh vault password +
  `MIGRATION_DATABASE_URL` + REASSIGN OWNED), needs no schema
  change, and must set a fresh password at the time — never a
  value that was nulled under §3.2. Revisit when a production
  host with a real vault exists.

Until then, `roles.sql` is the specification of the intended
posture plus the live grants, and the migrator role is reserved.

### 3.2 NOLOGIN-role passwords — DECIDED Phase 3: nulled (and a correction)

`courierledger_migrator` and `courierledger_maintenance` are set
`PASSWORD NULL` by `roles.sql` on every application (verified
live in `pg_authid` after the Phase 3 application: both NULL;
the two LOGIN roles hold SCRAM verifiers). Break-glass access
goes through the postgres superuser (`su postgres`), which can
SET ROLE or grant LOGIN temporarily under a reviewed change; no
standing password exists to leak, rotate, or forget. Do not set
a password on either role and do not flip LOGIN on either role
without a reviewed change.

**Correction to the Phase 2 record:** Phase 2 said both roles
"carry password verifiers despite NOLOGIN (verified live)".
That check used `pg_roles`, whose `rolpassword` column is a
constant `'********'` mask for every role (verified live
2026-11-02 from the view definition) — it cannot distinguish
"has a verifier" from "has none". The correct source is
`pg_authid` (superuser-only). Whether the pre-Phase-3 verifiers
existed cannot be established retroactively (the nulling
already ran); it is recorded as unknown, not asserted either
way. The check to use from now on is
`SELECT rolname, rolpassword IS NULL FROM pg_authid WHERE
rolname LIKE 'courierledger%';` as the postgres superuser.

### 3.3 Default privileges

`pg_default_acl` is **empty** — fail-closed. A new table gets no
grants by default; every new table needs an explicit grant added to
`roles.sql` in the same change. Add this to the migration review
checklist for every future migration.

### 3.4 Negative probes (live, as `courierledger_runtime`)

Phase 2 probes (rolled-back transaction, row counts re-verified
unchanged afterwards):

| Probe | Outcome |
|---|---|
| No context: `SELECT count(*) FROM merchants/orders` | 0 rows (fail-closed) |
| Tenant A context: own orders | 1 (only A's) |
| Tenant A context: B's orders | 0 |
| Cross-tenant INSERT into `orders` | ERROR 42501 (RLS violation) |
| UPDATE `audit_logs`, DELETE `idempotency_keys`, UPDATE `merchants` | ERROR 42501 (permission denied) |
| INSERT into `merchants` (grant exists, no INSERT policy) | ERROR 42501 (default deny) |
| UPDATE non-granted column of `reconciliation_runs` | ERROR 42501 |
| UPDATE granted columns (`status`) with no matching row | OK, 0 rows |
| `SELECT count(*) FROM users` | 4 (identity tables readable by design, no RLS — v0.1 C.1) |

Phase 3 probes (Workstream 2; re-run on the rebuilt cluster
2026-11-02, pinned by
`tests/test_migration_phase3_live_pg.py`):

| Probe | Outcome |
|---|---|
| `CREATE TABLE` as runtime | ERROR 42501 (permission denied for schema public) |
| `ALTER TABLE merchants ADD COLUMN` as runtime | ERROR 42501 (must be owner) |
| `DROP TABLE merchants` as runtime | ERROR 42501 (must be owner) |
| `DELETE FROM idempotency_keys` as runtime | ERROR 42501 (permission denied) |
| `ALTER TABLE merchants DISABLE ROW LEVEL SECURITY` as runtime | ERROR 42501 (must be owner) |
| `GRANT DELETE ON merchants TO courierledger_runtime` + `REVOKE SELECT …` as runtime | No error, no change — PostgreSQL answers a non-owner GRANT/REVOKE with a WARNING and leaves the ACL untouched; runtime privilege rows 74 → 74, still no grant option |
| Login as `courierledger_migrator` / `courierledger_maintenance` | `FATAL: password authentication failed` (NOLOGIN, no verifier) |
| `current_user` / `rolbypassrls` as runtime | `courierledger_runtime`, false |

### 3.5 Local-dev password rotation (Phase 3, 2026-11-02)

During the container-replacement rebuild, a helper echoed a
prefix of the then-current shared local-dev DB password into a
tool log. The password was rotated the same day: both LOGIN
roles (`courierledger`, `courierledger_runtime`) now carry a
fresh random value, stored only in gitignored
`m1/backend/.env` (`DATABASE_URL`); the old value no longer
authenticates either role. **Consequence for the coordinator:**
live-test runs must read the password from `.env` at runtime
(`CL_LIVE_PG_PASSWORD`); the `CI_POSTGRES_PASSWORD` repo secret
still holds the old value and must be updated if CI is ever
unblocked. This rotation is local-dev only — no production
credential was ever involved, and the NOLOGIN roles hold no
password at all (§3.2).

## 4. Connection pooling

No external pooler is configured. The app uses SQLAlchemy defaults
made explicit in Phase 2 (`app/db/session.py`): `pool_size=5`,
`max_overflow=10`, `pool_timeout=30`, `pool_recycle=1800`,
`pool_pre_ping=True` — at most 15 connections per backend process.
Tenant/user context is set transaction-local only, and SQLAlchemy
rolls the transaction back when a connection returns to the pool, so
a pooled connection cannot carry a previous tenant's context (proven
live by `test_pooled_connection_context_does_not_leak`). If a pooler
is ever added it **must be PgBouncer in transaction mode** (v0.1
§E.5); session-mode pooling is forbidden.

## 5. Timeouts (Phase 2 — applied live, recorded in `roles.sql`)

| Role | statement_timeout | idle_in_transaction_session_timeout | lock_timeout |
|---|---|---|---|
| `courierledger_runtime` | 30s | 60s | 5s |
| `courierledger_maintenance` | 10min | 10min | 30s |

Applied with `ALTER ROLE ... SET` (no restart needed) and verified
via `pg_roles.rolconfig`; the same statements are now in `roles.sql`
so a fresh provisioning gets them. The 30s runtime bound was chosen
after the 50k-row CSV import and run creation were observed to
complete well inside it. If a legitimate operation ever exceeds 30s,
raise the timeout for that path only (`SET LOCAL` in that
transaction), never globally. Verified by the new live test
`test_infrastructure_phase2_live_pg.py::test_runtime_role_timeouts_set`.

## 6. Provisioning (the one correct order)

Fresh database provisioning is a single script now:
`m1/scripts/provision_db.sh <db-name> [env-file]`. It encodes the
order proven in Phase 1.1 re-verification and Phase 2 Test D:

1. Create the database + the NOLOGIN roles (inline DO block).
2. Alembic `upgrade head` as the owner role.
3. `rls.sql` + `triggers.sql` as the owner role.
4. `roles.sql` as the postgres superuser.
5. Runtime-role password (roles.sql deliberately sets none).
6. `seed_couriers.sql` as the owner role.
7. Verification: alembic head, 33 tables, 46 policies, 15
   functions, 22 triggers, 5 couriers.

**Do not reorder.** `rls.sql` first fails with `role
"courierledger_maintenance" does not exist`; `roles.sql` first fails
with `function public.app_current_merchant_id() does not exist`.
Both failure modes were proven live in Phase 2. The script is
idempotent for re-provisioning (roles IF NOT EXISTS, Alembic no-op
at head, SQL files idempotent, seed idempotent) and never drops a
database. Passwords are read from `.env` at runtime, never printed.
Verified end-to-end on a scratch database in Phase 2 (head 0006,
counts identical to the working DBs), and re-running it is a clean
no-op. The same order is now fixed in both CI workflows (the two
NOLOGIN roles are pre-created before `rls.sql` runs).

## 7. What the database does NOT do yet

- **No backup exists** (see `DATABASE_BACKUP_RECOVERY.md`): no
  scheduled backup, no WAL archiving (`archive_mode = off`), no PITR,
  no base backup. The restore *procedure* was proven once (Phase 2),
  but no schedule, no off-host copy, and no globals backup exist.
  This is the single largest infrastructure gap for any production
  target.
- **No `pg_stat_statements`** (available, not installed; needs a
  restart + `CREATE EXTENSION`). Slow-query visibility is
  documentation-only until then.
- **Scheduler for `purge_expired_auth_rows` (Phase 3):** systemd timers now
  invoke it — `courierledger-maintenance.timer` (hourly) via
  `m1/scripts/maintenance.py`, plus `courierledger-backup.timer` (daily
  02:30 UTC) for encrypted backups; see `DATABASE_BACKUP_RECOVERY.md`
  and `m1/scripts/systemd/README.md`. Note the name is still
  misleading: it is an **app-side Python function**
  (`auth_service.py`), not a database procedure (Phase 1.1 docs called
  it a procedure; corrected in Phase 2).
- **Drift gate (Phase 3):** `alembic check` still exits 255 by design,
  but the residual is now a pinned allow-list of **13 ops** (12 FK
  representation differences + 1 RLS-layer index) — the one genuine
  mismatch was fixed by migration 0007. See
  `MIGRATION_VERIFICATION.md` §3.
- **Downgrades:** all five revisions have downgrades, but only the
  0002 downgrade has ever been proven, and it can fail on data
  containing a specific row shape (S non-NULL, V_R NULL, V_cond
  non-NULL). Production stance: no downgrades in production —
  restore from backup instead.

## 8. Routine checks (run these, in this order)

```bash
# 1. Cluster up, UTF8, version
pg_lsclusters; psql -d courierledger_app -tAc "SELECT version();"
# 2. Migration head + object counts (expect 0007 / 33 / 46 / 15 / 22 / 5 couriers)
cd m1/backend && MIGRATION_DATABASE_URL="<owner-url>" .venv/bin/alembic current
# 3. Role timeouts present
psql -d postgres -tAc "SELECT rolname, rolconfig FROM pg_roles WHERE rolname LIKE 'courierledger%';"
# 4. App connectivity (liveness vs readiness are different probes)
curl -s http://127.0.0.1:8000/api/health   # always 200 when the process is up
curl -s http://127.0.0.1:8000/api/ready    # 200 only with the DB up, else 503
# 5. Backup drill (quarterly at minimum)
m1/scripts/backup_db.sh courierledger_app
m1/scripts/verify_restore.sh courierledger_app m1/backups/<dump-file>
```

## 9. Doc corrections made in Phase 2

- `m1/README.md` §4: the "RLS ENABLE + FORCE on 26 tables" line
  was imprecise (FORCE is on 24). Corrected with a dated annotation
  preserving the original 2026-10-31 record; the "49 GRANTs" figure
  is annotated as the count as of 2026-10-31 (current: 51 GRANT
  statements in `roles.sql`, 74 runtime privilege rows live).
- `roles.sql` header: corrected the migrator-ownership sentence to
  the live reality and added the fresh-cluster provisioning order.
- `m1/backend/.env.example` created (the README referenced it; it
  did not exist). Root `README.md` created (the repo had none).
- No ADR 1.0 exists (re-confirmed in Phase 2); stack authority
  remains the kickoff brief.
