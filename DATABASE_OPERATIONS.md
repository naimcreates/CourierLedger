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
| Alembic head | **0006** (Phase 2: 0006 adds 3 FK indexes; was 0005 after Phase 1.1) |
| Tables | 33 per database (32 + `alembic_version`) |
| RLS policies | 46 per database |
| Functions | 15 (4 context helpers + `create_workspace` + 10 `fn_*` trigger functions) |
| Triggers | 22 (17 `CREATE TRIGGER` + 5 deferred `CONSTRAINT TRIGGER`: CL-01/CL-13/CL-16/CL-10) |
| Indexes | 132 per database (129 before migration 0006) |
| Constraints | 264 per database (public-schema `pg_constraint`: 94 CHECK, 85 FK, 33 PK, 5 trigger, 47 UNIQUE) |
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
| `courierledger_migrator` | **no** | no | no | nothing | **0 grants** — exists but unused (see §3.1) |
| `courierledger_maintenance` | **no** | no | no | nothing | SELECT on all 33 tables; DELETE only on `idempotency_keys`; narrow UPDATEs; no financial-history mutation |

### 3.1 The migrator question (open decision, not silently resolved)

The design says `courierledger_migrator` owns the schema and runs
Alembic. Live reality: it owns 0 objects and holds 0 grants; live
migrations run as `courierledger` (the LOGIN owner). Two options,
neither applied yet:

- **Option 1 (preferred for production):** put the migrator into
  service — `REASSIGN OWNED BY courierledger TO
  courierledger_migrator;` per database, then use it (LOGIN, vault
  password) as `MIGRATION_DATABASE_URL`.
- **Option 2:** document the status quo in `roles.sql` (already
  partially done in Phase 2: the header now states the live reality)
  and keep `courierledger` as the live migrator.

Until one is chosen, `roles.sql` is a specification of the intended
posture plus the live grants, and the migrator role is reserved.

### 3.2 NOLOGIN-role passwords (observation, not an active hole)

`courierledger_migrator` and `courierledger_maintenance` both carry
password verifiers despite NOLOGIN. NOLOGIN blocks login today, so
this is not an active hole — but flipping LOGIN would make a stored
password live immediately. Either null them (`ALTER ROLE ...
PASSWORD NULL`) if break-glass goes through the superuser, or record
them in `PRODUCTION_SECRETS_GUIDE.md` as the break-glass credential
with rotation discipline. Do not flip LOGIN on either role without a
reviewed change. Recorded in `roles.sql` (Phase 2 note 4).

### 3.3 Default privileges

`pg_default_acl` is **empty** — fail-closed. A new table gets no
grants by default; every new table needs an explicit grant added to
`roles.sql` in the same change. Add this to the migration review
checklist for every future migration.

### 3.4 Negative probes (live, as `courierledger_runtime`)

All behaved as designed (Phase 2, rolled-back transaction, row
counts re-verified unchanged afterwards):

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
- **No scheduler** for `purge_expired_auth_rows` — and the name is
  misleading: it is an **app-side Python function**
  (`auth_service.py`), not a database procedure, and nothing invokes
  it on a schedule. (Phase 1.1 docs called it a procedure; corrected
  here.)
- **No drift gate:** `alembic check` fails (exit 255, 17–18 ops) on a
  correctly migrated database — all representation drift, no missing
  tables/columns. See `MIGRATION_VERIFICATION.md`.
- **Downgrades:** all five revisions have downgrades, but only the
  0002 downgrade has ever been proven, and it can fail on data
  containing a specific row shape (S non-NULL, V_R NULL, V_cond
  non-NULL). Production stance: no downgrades in production —
  restore from backup instead.

## 8. Routine checks (run these, in this order)

```bash
# 1. Cluster up, UTF8, version
pg_lsclusters; psql -d courierledger_app -tAc "SELECT version();"
# 2. Migration head + object counts (expect 0006 / 33 / 46 / 15 / 22 / 5 couriers)
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
