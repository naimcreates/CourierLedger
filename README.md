# CourierLedger

Bangladesh-first COD courier reconciliation for online sellers:
reconcile what a courier was supposed to collect, what it reported
collecting, what it charged, what it paid out, and what actually
arrived in the bank — with every unexplained difference shown as
needing review, never silently labelled a loss.

**Status: local development build. NOT production-ready.**
Production deployment and real merchant data are gated (see
`m1/README.md` §7). All data in this repository is synthetic unless
a merchant has explicitly provided it.

## Where things live

- `GOAL.md` — the goal record: decisions, milestones, blockers.
- `m1/` — the M1 application: React + TypeScript + Vite frontend,
  FastAPI backend, PostgreSQL 16, Alembic migrations, RLS
  multi-tenancy. Start with `m1/README.md` (setup, verification,
  architecture) and `m1/LOCAL_VERIFICATION.md` (the reproducible
  local test procedure, including PostgreSQL recovery after a
  container replacement).
- `prototype-p0/` — the disposable P0 prototype (read-only): the
  pure-Python V2.2.1 reconciliation engine with its own test suite.
- `files/` — planning and validation documents: the specification
  (V2–V2.2.1), the M1 database design (v0.1 + amendments v0.2–v0.5),
  the market-validation playbook and evidence tracker, and the
  Day 7 validation pack.
- `SECURITY_AUDIT.md`, `SECURITY_REMEDIATION.md`,
  `SECURITY_DECISIONS.md`, `SECURITY_TEST_REPORT.md`,
  `PRODUCTION_SECRETS_GUIDE.md` — Phase 1 / 1.1 security work.
- `DATABASE_OPERATIONS.md`, `DATABASE_BACKUP_RECOVERY.md`,
  `INFRASTRUCTURE_DESIGN.md`, `MIGRATION_VERIFICATION.md`,
  `PRODUCTION_CONFIGURATION_GUIDE.md`,
  `INFRASTRUCTURE_TEST_REPORT.md` — Phase 2 database and
  infrastructure hardening.

## Quick start (local development only)

See `m1/README.md` §3 for setup (PostgreSQL 16 via apt, UTF8
cluster, `provision_db.sh`, Alembic, frontend `npm run dev`) and
`m1/LOCAL_VERIFICATION.md` for the full test procedure. Docker is
documented but **cannot run containers in this VM** (see
`m1/docker-compose.yml` header and `INFRASTRUCTURE_DESIGN.md`).

## Rules that do not change

- Money is integer poisha; each fee is counted exactly once;
  missing or unverified information is "cannot calculate exactly" /
  "needs review", never an invented amount or an implicit zero.
- Courier rates come only from merchant-confirmed evidence, never
  from public pricing pages.
- No match is ever made on amount alone; source rows are preserved,
  never silently merged.
- Synthetic data only, unless the merchant has given permission to
  use appropriate real data.
- Never commit a real credential. `.env` files are gitignored;
  templates live in `m1/.env.example` and
  `m1/backend/.env.example`.
