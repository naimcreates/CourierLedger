# CourierLedger — Migration Verification

**Status:** Phase 2 (2026-11-01). Alembic revisions 0001–0006.
All migration tests below were **actually run** against real
PostgreSQL 16.15 — never substituted with SQLite.

Related: `DATABASE_OPERATIONS.md` (provisioning order),
`INFRASTRUCTURE_TEST_REPORT.md` (full test counts).

---

## 1. The revision chain

| Revision | Content | Downgrade |
|---|---|---|
| 0001 `initial_m1` | All 32 tables (single migration) | yes |
| 0002 `fix_result_identity_checks` | M6-A1: identity CHECKs enforce only when all terms are non-NULL | yes — **proven hazard** (see §4) |
| 0003 | CSV safety: formula-like flagging, etc. (Phase 1) | yes |
| 0004 | CSV limits (Phase 1) | yes |
| 0005 | `step_up_expires_at` on sessions (Phase 1.1) | yes |
| **0006 `phase2_missing_fk_indexes`** | **New in Phase 2:** indexes `ix_user_sessions_user_id`, `ix_password_reset_tokens_user_id`, `ix_import_batches_template_id` | yes (drops the three indexes) |

Rules honoured: historical migrations (0001–0005) were **never
modified**; the Phase 2 change is a new corrective migration.
Indexes are plain `CREATE INDEX` (not CONCURRENTLY) because Alembic
runs inside a transaction — CONCURRENTLY would fail there. The
columns were verified to exist before the migration was written.

## 2. Test results (Phase 2, real PostgreSQL)

### A — Empty database → complete migration: **PASS**

Fresh database → `alembic upgrade head` → head **0006** in **1.17 s**
(re-run by Ren in Phase 2; Stream B measured 2.25 s at 0005).
Alembic-only database: 33 tables, **0 policies, 0 functions,
0 triggers**, 131 indexes, 259 constraints.

**One gap, stated plainly:** Alembic alone does **not** produce a
working database. A database migrated by Alembic only has 33 tables
but **0 policies, 0 functions, 0 triggers, 0 seed rows**; the
runtime role gets permission denied and `create_workspace()` does
not exist. A working database needs Alembic **plus** `rls.sql` +
`triggers.sql` + `roles.sql` + seed, in the order in
`DATABASE_OPERATIONS.md` §6. This is now encoded in
`m1/scripts/provision_db.sh` and is the reason a "migrated" database
is not a "provisioned" database.

### B — Existing database at 0005 → latest: **PASS**

Both working databases (`courierledger_app`, `courierledger_dev`)
were upgraded 0005 → 0006 as the owner role with the password read
from `.env` at runtime. `alembic current` shows **0006 (head)** on
both; all three new indexes present. (Note: the upgrade prints no
"Running upgrade" line on stdout — Alembic logs INFO to stderr; the
head change was verified via `alembic current` and the index count,
not the stdout filter.)

### C — Repeated migration command → safe no-op: **PASS**

`alembic upgrade head` re-run on a database already at head is a
clean no-op (exit 0, no changes). Verified on the Phase 2 scratch
database and on both working databases.

### D — Fresh cluster provisioning in the correct dependency order: **PASS**

`m1/scripts/provision_db.sh` was run end-to-end on a scratch
database `courierledger_phase2_test`: NOLOGIN roles → Alembic →
`rls.sql`/`triggers.sql` → `roles.sql` → runtime password → seed →
verification. Result: head **0006**, 33 tables, 46 policies, 15
functions, 22 triggers, 5 couriers, all three new indexes present.
A second run was a **clean no-op** with identical counts. The
scratch database was dropped afterwards.

**The trap, proven live:** the two wrong orders both fail.
`roles.sql` first → `function public.app_current_merchant_id()
does not exist` (exit 3). `rls.sql` first → `role
"courierledger_maintenance" does not exist`. Both failure modes were
reproduced in Phase 2 (the second by simulation + static dependency
analysis + the prior Phase 1.1 record, because dropping cluster roles
live was not safe). **Both CI workflows would hit this trap on a
fresh environment** — they pre-created only `courierledger` and
`courierledger_runtime` and ran `rls.sql` before `roles.sql`. Phase 2
fixed both workflows: the two NOLOGIN roles are now pre-created
before `rls.sql` runs (verified by count assertions + YAML parse;
CI itself cannot run — billing lock).

## 3. Schema drift — `alembic check` fails, and why

`alembic check` fails with **exit 255** on a correctly migrated
database: 17 ops on an Alembic-only database, 18 on a provisioned
one. Every op is **representation drift, not a missing object**:

- 6 foreign keys created unnamed at `NO ACTION` in SQLAlchemy
  (no DDL) while the models declare `ondelete="RESTRICT"` — the
  behaviour is identical, the representation differs.
- Unique indexes vs `UniqueConstraint` representation differences.
- One genuinely missing but harmless constraint:
  `uq_memberships_merchant_id` (a UniqueConstraint on
  `merchant_memberships`).
- One RLS-layer index (`ix_merchant_memberships_user_active`) that
  exists in the live database but has no model counterpart.

**No drift detection exists in CI.** Decision still open (recorded
for the founder, not silently resolved):

- **Option A — align:** apply a corrective migration (a draft
  exists in the Phase 2 working notes: adds
  `uq_memberships_merchant_id`, re-creates the 6 FKs with RESTRICT)
  so `alembic check` can go green.
- **Option B — allow-list:** teach the drift check to ignore the
  RLS-layer objects and the representation differences.
- **Option C — tolerate:** leave `alembic check` red and rely on the
  migration tests (A–D) and the object-count verification instead.

**Not applied in Phase 2:** the draft 0006-style alignment migration
was deliberately *not* applied — the applied 0006 is the index
migration only. Applying the draft without a decision would have
changed FK definitions on a live schema under the guise of drift
cleanup.

## 4. Downgrade hazard — 0002 (proven at CHECK level)

The 0002 downgrade (re-adding 0001's identity CHECKs) fails on a
row shape where S is non-NULL, V_R is NULL, and V_cond is non-NULL:
the row inserts fine under 0002 but re-adding the old CHECK fails.
**No such rows exist in `courierledger_app` today** (0 of 4 result
rows). The hazard is a data-shape hazard, not a present failure.
Production stance: **no downgrades in production** — restore from
backup instead of downgrading. All five revisions still carry
downgrades for local development use.

## 5. Verification hooks added in Phase 2

- New live test `test_infrastructure_phase2_live_pg.py` (4 tests):
  alembic head is 0006; the three 0006 indexes exist; runtime-role
  timeout settings in `pg_roles.rolconfig`; `GET /api/ready` returns
  200 with `database_ok: true` and an `X-Request-ID` header. All 4
  pass (see `INFRASTRUCTURE_TEST_REPORT.md`).
- `provision_db.sh` runs a verification block after provisioning
  (head, tables, policies, functions, triggers, couriers) and fails
  loudly if any count differs.
- `verify_restore.sh` re-verifies the alembic version and object
  counts on the restored database.

## 6. What migration verification still lacks

- No CI gate runs `alembic check` (and it would currently fail —
  §3).
- No automated test asserts the provisioning order (the script
  encodes it; the test is the manual scratch-DB run in §2 D).
- No test proves a downgrade on a populated database — by design
  (§4: downgrades are not a production recovery path).
