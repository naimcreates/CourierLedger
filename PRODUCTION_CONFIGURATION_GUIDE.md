# CourierLedger — Production Configuration Guide

**Status:** Phase 2 (2026-11-01). This is a **checklist for a
production host that does not exist yet**, not a record of a
deployed system. Every item is labelled **APPLIED** (done here,
verified live) or **RECOMMENDATION** (cannot be verified on this
VM — apply and re-verify when a production host exists). Do not
read a recommendation as a fact about the current system.

---

## 1. PostgreSQL server (16.x)

| Setting | Value | Status |
|---|---|---|
| Version | PostgreSQL 16.x (16.15 locally) | APPLIED (local) |
| Encoding | UTF8, locale C.UTF-8 | **APPLIED — mandatory.** A SQL_ASCII cluster breaks psycopg/Alembic (proven in M10). Verify with `pg_encoding_to_char(encoding)` on every database after any rebuild |
| Timezone | Server Etc/UTC; app stores TIMESTAMPTZ | APPLIED |
| `max_connections` | 100 (local default) | Local default; size for the host — see §5 |
| `shared_buffers` | **2 GB** | RECOMMENDATION (local: 128 MB — do not copy the local value to production; do not apply the 2 GB value to this VM either) |
| `effective_cache_size` | **6 GB** | RECOMMENDATION |
| `work_mem` | **16 MB** | RECOMMENDATION (local: 4 MB) |
| `maintenance_work_mem` | **512 MB** | RECOMMENDATION |
| `statement_timeout` (server) | 0 (unlimited) locally | **Per-role timeouts are applied instead** — see §2 |
| `log_min_duration_statement` | **1000 ms** | RECOMMENDATION (slow-query visibility) |
| `pg_stat_statements` | Install + `CREATE EXTENSION` | RECOMMENDATION — needs a restart and the extension; not installed here |
| `wal_level` | replica | APPLIED (local) |
| `archive_mode` | **on** (with an archive command) | RECOMMENDATION — currently **off**; required for PITR (see `DATABASE_BACKUP_RECOVERY.md`) |
| SSL | on, snakeoil cert locally | RECOMMENDATION — a real certificate on a real host |
| `listen_addresses` | localhost locally | RECOMMENDATION — loopback or private network only, never public |

## 2. Per-role timeouts (APPLIED)

`ALTER ROLE ... SET` — applied live on both working databases and
recorded in `m1/backend/app/db/roles.sql` so fresh provisioning
gets them. No restart needed. Verified via `pg_roles.rolconfig`
and by the new live test.

| Role | statement_timeout | idle_in_transaction_session_timeout | lock_timeout |
|---|---|---|---|
| `courierledger_runtime` | 30s | 60s | 5s |
| `courierledger_maintenance` | 10min | 10min | 30s |

If a legitimate operation ever exceeds 30s, raise the timeout for
that path only (`SET LOCAL` in that transaction), never globally.

## 3. Connection pooling (APPLIED)

No external pooler. SQLAlchemy explicit settings in
`app/db/session.py`: `pool_size=5`, `max_overflow=10`,
`pool_timeout=30`, `pool_recycle=1800`, `pool_pre_ping=True` —
at most 15 connections per backend process. If a pooler is ever
added it **must be PgBouncer in transaction mode** (v0.1 §E.5);
session-mode pooling is forbidden. Tenant/user context is
transaction-local only; a pooled connection cannot carry a previous
tenant's context (proven live: `test_pooled_connection_context_does_not_leak`).

## 4. Roles and privileges (APPLIED — verified live)

See `DATABASE_OPERATIONS.md` §3 for the full table and negative
probes. Essentials:

- Runtime role `courierledger_runtime`: no table ownership,
  `rolbypassrls = false`, 0 DELETE grants, column UPDATE only on
  `reconciliation_runs(status, completed_at)`.
- Migration privileges are **not** separated live: the migrator role
  exists with 0 grants; migrations run as `courierledger`. **Open
  decision:** put the migrator into service (REASSIGN OWNED) or
  document `courierledger` as the live migrator.
- Maintenance role: SELECT all tables, DELETE only on
  `idempotency_keys`, narrow UPDATEs, no financial-history mutation.
- Default privileges: `pg_default_acl` is empty (fail-closed) — every
  new table needs an explicit grant in `roles.sql` in the same change.
- No application superuser credentials. No credentials in git (the
  only real secret found in the repo was the gitignored
  `m1/backend/.env`; the historical local-dev password in git
  history is invalid for both roles — see
  `PRODUCTION_SECRETS_GUIDE.md`).

## 5. Sizing guidance (RECOMMENDATION)

The local cluster (128 MB shared_buffers, 4 MB work_mem, 100
connections) is a development default, not a production target.
Size from the host's memory: `shared_buffers` ≈ 25% of RAM,
`effective_cache_size` ≈ 75% of RAM, `work_mem` from the expected
concurrent-sort count, `max_connections` from (backend processes ×
15 pool) + maintenance headroom. Re-verify with the actual workload
after the first production-like load test — no load test has been run.

## 6. Environment variables (APPLIED — templates)

Templates: `m1/.env.example` (compose) and
`m1/backend/.env.example` (backend, created in Phase 2 — the README
referenced it and it did not exist). Rules:

- The app connects as the **runtime** role (`DATABASE_URL`); Alembic
  and admin SQL connect as the **owner** role
  (`MIGRATION_DATABASE_URL`). Never the reverse.
- `SECRET_KEY` / `SESSION_SECRET`: long random values
  (`secrets.token_urlsafe(48)`); never committed; never the local-dev
  values in production.
- `SESSION_COOKIE_SECURE=true` behind HTTPS; `ENVIRONMENT=production`
  disables API docs and enforces the secure-cookie guard.
- `CREATE_TABLES_ON_STARTUP=false` in production — schema is owned
  by Alembic.
- `FRONTEND_URL` is the single CORS origin.
- **Never** put a database password in an image layer, a compose
  file, or a workflow file. The compose files are fail-closed on an
  empty password. Passwords are injected at runtime from the secret
  store (local dev: the gitignored `.env`).

## 7. Backups (RECOMMENDATION — procedure proven, no schedule)

See `DATABASE_BACKUP_RECOVERY.md`. The procedure (`backup_db.sh` /
`verify_restore.sh`) was proven once on this VM. What does not exist
yet: a schedule, an off-host copy, WAL archiving, a globals backup,
a recurring drill, encryption, failure alerts. **Proposed targets
(not verified):** RPO ≤ 1 h, RTO ≤ 2 h, 30 daily + 12 monthly dumps.
Do not load real merchant data until a backup schedule and an
off-host copy exist.

## 8. Observability (APPLIED where noted)

- Request IDs + structured JSON logs with `request_id` on every line
  — **APPLIED** (Phase 2).
- `/api/health` (liveness) vs `/api/ready` (readiness, 200/503) —
  **APPLIED** (Phase 2). Use `/ready` for load-balancer checks.
- `pg_stat_statements` + `log_min_duration_statement = 1000` —
  RECOMMENDATION (needs restart + extension).
- Hosted error reporting — **not built** (founder decision).
- Metrics endpoint / distributed tracing — **not built**.
- Backup failure alerts — RECOMMENDATION (the backup job must alert
  on failure).

## 9. Pre-launch checklist (all must be true before production)

- [ ] Production host provisioned; Docker verified on a
  container-capable machine (B-2 closed) or native PostgreSQL
  documented as the production path
- [ ] PostgreSQL 16, UTF8/C.UTF-8, `postgresql.conf` per §1,
  `pg_stat_statements` installed
- [ ] Migrator role decision made and applied (§4)
- [ ] NOLOGIN-role passwords documented or nulled
- [ ] Backup schedule + off-host copy + WAL archiving running;
  restore drill passed and recorded
- [ ] Object-storage provider chosen (or local storage explicitly
  accepted as a temporary measure with its limits recorded)
- [ ] Email provider chosen; password reset no longer log-only
- [ ] OD-12 / OD-16 / OD-17 closed (legal/policy review)
- [ ] ADR 1.0 written
- [ ] GitHub billing lock resolved; CI green on a real run
- [ ] Market-validation gates passed (Day 7 + Day 14)
- [ ] Merchant-confirmed courier evidence (rate cards, redacted
  statement headers) for at least the first courier
- [ ] `alembic check` drift decision made (see
  `MIGRATION_VERIFICATION.md` §3) and applied

**None of these is true today. The project is NOT production-ready.**
