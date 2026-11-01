# CourierLedger — Migration Verification

**Status:** Phase 3 (2026-11-02). Alembic revisions 0001–0007.
All migration tests below were **actually run** against real
PostgreSQL 16.15 — never substituted with SQLite.

Related: `DATABASE_OPERATIONS.md` (provisioning order, role model),
`INFRASTRUCTURE_TEST_REPORT.md` (full test counts — **note:** that
report still records head 0006 from Phase 2; it was not updated in
Phase 3 because it belongs to another stream).

**Environment note (2026-11-02):** midway through Phase 3 the VM
container was replaced: PostgreSQL was wiped entirely (no
`postgres` user, no cluster, `/tmp` empty). It was rebuilt from
the cached `.deb`s exactly as in the Phase 1.1 recovery
(`dpkg -i` from `/var/cache/apt/archives/`, cluster `16 main`
already UTF8/C.UTF-8, PostgreSQL 16.15, online on 5432), both
working databases were re-provisioned from scratch with the
Phase 3 code, and every live proof below was re-run on the
rebuilt cluster. The old cluster's synthetic rows (244 in
`courierledger_app`) are gone with it; the rebuilt databases hold
only the 5 seed courier codes plus whatever tests create. The
local-dev DB password was rotated during that rebuild (a helper
echoed a prefix of the old value into a tool log; the old value
is now invalid — see `DATABASE_OPERATIONS.md` §3.5). Live-test
runners must read the current password from gitignored
`m1/backend/.env`; the `CI_POSTGRES_PASSWORD` repo secret still
holds the old value and needs updating if CI is ever unblocked.

---

## 1. The revision chain

| Revision | Content | Downgrade |
|---|---|---|
| 0001 `initial_m1` | All 32 tables (single migration) | yes |
| 0002 `fix_result_identity_checks` | M6-A1: identity CHECKs enforce only when all terms are non-NULL | yes — **proven hazard** (see §4) |
| 0003 | CSV safety: formula-like flagging, etc. (Phase 1) | yes |
| 0004 | CSV limits (Phase 1) | yes |
| 0005 | `step_up_expires_at` on sessions (Phase 1.1) | yes |
| 0006 `phase2_missing_fk_indexes` | Indexes `ix_user_sessions_user_id`, `ix_password_reset_tokens_user_id`, `ix_import_batches_template_id` | yes (drops the three indexes) |
| **0007 `phase3_missing_membership_unique`** | **New in Phase 3:** unique constraint `uq_memberships_merchant_id` on `merchant_memberships (merchant_id, id)` — the one genuine drift mismatch (see §3) | yes (drops the constraint) |

Rules honoured: historical migrations (0001–0006) were **never
modified**; the Phase 3 change is a new corrective migration.
The constraint is additive only and can never fail on existing
rows (`id` is already the primary key, so `(merchant_id, id)`
uniqueness is already implied by the data).

## 2. Test results (Phase 3, real PostgreSQL, rebuilt cluster)

### A — Empty database → complete migration: **PASS**

Fresh database → `alembic upgrade head` → head **0007**.
Run as the live migrator (`courierledger`) via
`m1/scripts/provision_db.sh courierledger_phase3_prov` (which
starts from an empty database): head **0007**, 33 tables, 46
policies, 15 functions, 22 triggers, 5 couriers. The same run
also proves D (below) — A and D share the empty-DB path.

**Unchanged gap, stated plainly:** Alembic alone does **not**
produce a working database. A database migrated by Alembic only
has 33 tables but **0 policies, 0 functions, 0 triggers, 0 seed
rows**; the runtime role gets permission denied and
`create_workspace()` does not exist. A working database needs
Alembic **plus** `rls.sql` + `triggers.sql` + `roles.sql` + seed,
in the order in `DATABASE_OPERATIONS.md` §6. This is encoded in
`m1/scripts/provision_db.sh` and is the reason a "migrated"
database is not a "provisioned" database.

### B — Existing database at 0006 → latest: **PASS**

Both working databases (`courierledger_app`, `courierledger_dev`)
were upgraded 0006 → 0007 as the live migrator
(`MIGRATION_DATABASE_URL` naming `courierledger`, password read
from `.env` at runtime). `alembic current` shows **0007 (head)**
on both; `uq_memberships_merchant_id` present on both
(`UNIQUE (merchant_id, id)`). The same upgrade was first proven
on the pre-rebuild cluster and then re-run on the rebuilt one
(the rebuild re-provisioned both DBs from scratch at 0007, so
the rebuilt-cluster "upgrade" is the provisioning path, not a
second 0006 → 0007 step — see the environment note above).

### C — Repeated migration command → safe no-op: **PASS**

`alembic upgrade head` re-run on a database already at head is a
clean no-op (exit 0, no changes). Verified on `courierledger_app`
and `courierledger_dev` after the 0007 upgrade, and on the
Phase 3 scratch database. `m1/scripts/provision_db.sh` re-run on
an already-provisioned database is likewise a clean no-op with
identical counts (head 0007, 33 / 46 / 15 / 22 / 5).

### D — Fresh cluster provisioning in the correct dependency order: **PASS**

`m1/scripts/provision_db.sh` was run end-to-end on a scratch
database `courierledger_phase3_prov` on the rebuilt cluster:
roles → Alembic (0007, as the live migrator) → `rls.sql` /
`triggers.sql` → `roles.sql` → runtime password → seed →
verification. Result: head **0007**, 33 tables, 46 policies, 15
functions, 22 triggers, 5 couriers, all three 0006 indexes
present, `uq_memberships_merchant_id` present. A second run was
a **clean no-op** with identical counts. The scratch database
was dropped afterwards.

**New trap found and fixed in Phase 3:** on a truly fresh
cluster (no roles at all), provisioning failed at `rls.sql:186`
with `role "courierledger_runtime" does not exist` — `rls.sql`
grants EXECUTE on the context helpers to the runtime role, but
`provision_db.sh` step 1 only pre-created the two NOLOGIN roles;
the runtime role was only created later by `roles.sql`. Earlier
fresh-cluster runs never hit this because the runtime role
already existed cluster-wide from a previous provisioning.
Fix (in `provision_db.sh` only): step 1 now also creates
`courierledger_runtime` (LOGIN NOBYPASSRLS, **no password** — it
cannot authenticate until step 5 sets one from `.env`); `roles.sql`
re-asserts the role idempotently in step 4. Verified live: the
rebuilt-cluster provisioning above ran with the fix.

The two wrong orders from Phase 2 still fail as documented
(`roles.sql` first → missing function; `rls.sql` first →
missing maintenance role); the fix does not change the order,
it completes the role set the order assumes.

### (5) — RLS, triggers, indexes, constraints correct after the correction: **PASS**

On the freshly provisioned scratch database (head 0007):

| Check | Result |
|---|---|
| Tables / policies / functions / triggers | 33 / 46 / 15 / 22 |
| Indexes | 133 (was 132 at 0006 — +1 is the backing index of the new unique constraint) |
| Constraints (`pg_constraint`, public) | 265 (was 264 at 0006 — +1 is `uq_memberships_merchant_id`) |
| `uq_memberships_merchant_id` | present, `UNIQUE (merchant_id, id)` |
| 0006 indexes (`ix_user_sessions_user_id`, `ix_password_reset_tokens_user_id`, `ix_import_batches_template_id`) | all 3 present |
| RLS-layer index `ix_merchant_memberships_user_active` | present |
| FORCE RLS tables / RLS-enabled tables | 24 / 26 (unchanged) |
| Auth-table FKs with `ON DELETE NO ACTION` (`confdeltype='a'`) | 6 (unchanged — see §3) |
| Named FKs with `ON DELETE RESTRICT` (`confdeltype='r'`) | 79 (unchanged) |
| Seed couriers | 5 |

### (6) — No schema changes caused by ordinary application startup: **PASS**

The app was started as the runtime role against the provisioned
scratch database (`CREATE_TABLES_ON_STARTUP=false`), served
`GET /api/health` (200) and `GET /api/ready` (200,
`database_ok: true`), then performed real DML — register (201),
login (200), `POST /api/workspaces` (201), list workspaces (200,
1 row) — and was stopped. `alembic check` before startup and
after shutdown reported the **identical** residual signature
(the 13-operation allow-list in §3; the list is printed twice
by Alembic — once on stdout, once on stderr — so a naive
stdout+stderr count reads 26; the per-stream count is 13).
Object counts after startup were unchanged (33 tables, 265
constraints, 133 indexes; 1 user, 1 merchant created by the
smoke flow). The same check runs inside the new live test
`test_drift_is_exactly_the_documented_allow_list`, which does
its own register/login through the fixture server before
running `alembic check`.

## 3. Schema drift — `alembic check`, fully inventoried (Phase 3)

### 3.1 Baseline (before Phase 3, at head 0006)

`alembic check` failed with **exit 255** on a correctly migrated
database: **21 operations on a provisioned database, 20 on an
Alembic-only database** (the difference is exactly the one
RLS-layer index, op D-4 below). Every operation was re-derived
in Phase 3 by running `alembic check` and comparing each item
against the live catalog (`pg_constraint`, `pg_indexes`,
`pg_get_constraintdef`) and the model sources — not by trusting
the Phase 2 summary. One row per operation:

| # | Operation | Object | Classification | Evidence / disposition |
|---|---|---|---|---|
| 1 | add_constraint | `uq_templates_id` on `courier_statement_templates (id)` | **Representation — redundant UNIQUE on a PK column** | The pre-rebuild live databases carried a primary key literally named `uq_templates_id` while the model also declared `UNIQUE (id)`; autogenerate therefore reported the unique as "added" (the live `uq_templates_id` is a PK, not a unique constraint). Provenance of that PK name is **unknown**: no committed code produces it — a fresh 0001 creates `courier_statement_templates_pkey` and no `(id)` unique (proven live, Phase 3, on a scratch database upgraded to 0001 only), and the old model renders a separate `UNIQUE (id)` — so it predates the squashed git history and is not something to preserve. Uniqueness was already enforced by the PK either way. **Fixed in Phase 3 (metadata-only):** the redundant `UniqueConstraint("id", name="uq_templates_id")` was removed from `CourierStatementTemplate` in `models.py`. No DDL; the rebuilt cluster confirms the model now matches a fresh 0001 exactly on this table. |
| 2 | remove_index | `ix_import_batches_template_id` | **Migration/metadata gap** | Migration 0006 created it; `ImportBatch.template_id` had no `index=True`. **Fixed (metadata-only):** `index=True` added to the column. |
| 3 | remove_index | `ix_password_reset_tokens_user_id` | **Migration/metadata gap** | Same as #2, for `PasswordResetToken.user_id`. **Fixed (metadata-only):** `index=True` added. |
| 4 | remove_index | `ix_user_sessions_user_id` | **Migration/metadata gap** | Same as #2, for `UserSession.user_id`. **Fixed (metadata-only):** `index=True` added. |
| 5 | remove_index | `ix_merchant_memberships_user_active` | **Manually managed object (RLS layer) — ALLOW-LISTED** | Created by `rls.sql:284` to back the membership-based policy subquery; it has no model counterpart by design and must never be added to the models. This is the one residual `remove_index`. |
| 6 | add_constraint | `uq_memberships_merchant_id` on `merchant_memberships (merchant_id, id)` | **GENUINE schema mismatch — fixed by migration 0007** | Declared in `auth_models.py` (`MerchantMembership`, the A.7/R2 tenant-parent pattern every other tenant table follows) but migration 0001 never created it; the live database had only `uq_membership_merchant_user`. Harmless in practice (`id` is the PK) but a real model/DB mismatch. **Fixed by 0007** (additive, always succeeds — see §1). |
| 7–9 | remove_fk ×3 | `merchant_memberships (user_id, merchant_id, invited_by_user_id)` | **Representation — NO ACTION vs RESTRICT — ALLOW-LISTED** | Live `pg_constraint.confdeltype = 'a'` (NO ACTION, the PostgreSQL default — migration 0001 created these six FKs unnamed with no `ON DELETE` clause); models declare `ondelete="RESTRICT"` via column-level `ForeignKey`. Non-deferrable NO ACTION is behaviourally identical to RESTRICT (both reject the delete; the only difference arises with `DEFERRABLE … INITIALLY DEFERRED`, which these FKs are not). Recreating them would be DDL churn with exclusive locks for zero behavioural change — deliberately **not** migrated. |
| 10–12 | add_fk ×3 | same three FKs, `ondelete='RESTRICT'` | **Representation — paired with #7–9 — ALLOW-LISTED** | The `add_fk` half of the same comparison. |
| 13 | remove_fk | `merchants.created_by_user_id` | **Representation — ALLOW-LISTED** | Same as #7–9 (`confdeltype='a'`). |
| 14 | add_fk | `merchants.created_by_user_id`, `ondelete='RESTRICT'` | **Representation — paired with #13 — ALLOW-LISTED** | |
| 15 | remove_fk | `password_reset_tokens.user_id` | **Representation — ALLOW-LISTED** | Same as #7–9. |
| 16 | add_fk | `password_reset_tokens.user_id`, `ondelete='RESTRICT'` | **Representation — paired with #15 — ALLOW-LISTED** | |
| 17 | remove_fk | `user_sessions.user_id` | **Representation — ALLOW-LISTED** | Same as #7–9. |
| 18 | add_fk | `user_sessions.user_id`, `ondelete='RESTRICT'` | **Representation — paired with #17 — ALLOW-LISTED** | |
| 19 | remove_constraint | `users.email` | **Representation — unnamed unique vs named live constraint — FIXED (metadata-only)** | Live: `users_email_key` (PostgreSQL auto-name from migration 0001's `unique=True`). In SQLAlchemy metadata, `unique=True` + `index=True` on one column collapses to a unique INDEX only — no `UniqueConstraint` — so autogenerate could not pair the live constraint. **Fixed:** explicit `UniqueConstraint("email", name="users_email_key")` in `User.__table_args__`. The DB already had both the constraint and the unique index; the model now says so. |
| 20 | remove_constraint | `user_sessions.token_hash` | **Representation — FIXED (metadata-only)** | Same as #19. Live: `user_sessions_token_hash_key`. Fixed with an explicit `UniqueConstraint` of that name. |
| 21 | remove_constraint | `password_reset_tokens.token_hash` | **Representation — FIXED (metadata-only)** | Same as #19. Live: `password_reset_tokens_token_hash_key`. Fixed with an explicit `UniqueConstraint` of that name. |

### 3.2 How the representation fixes work (metadata-only, no DDL)

Three coordinated model changes, all in `app/db/auth_models.py`
unless noted; none changes any live object:

1. **Naming convention on the shared `Base.metadata`**
   (`NAMING_CONVENTION` in `auth_models.py`): `ix →
   ix_<table>_<column>`, `uq → <table>_<column>_key`, `fk →
   <table>_<column>_fkey`, `pk → <table>_pkey`. These reproduce
   exactly PostgreSQL's auto-names for the constraints migration
   0001 created unnamed, so autogenerate can pair metadata
   constraints with live ones. Every produced name was verified
   live against `pg_constraint` / `pg_indexes` before adoption
   (all 7 unnamed column-level uniques, all 6 unnamed auth FKs,
   the 3 `index=True` unique indexes). Explicitly named
   constraints are untouched by a convention. No `ck` entry on
   purpose — every CHECK is explicitly named.
2. **`index=True` on the three 0006 columns** (see #2–4 above).
3. **Explicit `UniqueConstraint`s for the three column uniques**
   (see #19–21 above) and **removal of the redundant
   `UniqueConstraint("id")`** on `courier_statement_templates`
   (see #1 above).

Verification that the convention introduced no new drift: after
all model changes, `alembic check` on the Alembic-only scratch
database at head 0007 reported **exactly 12 operations** (the 6
FK pairs — no new operation of any kind appeared); on the
provisioned database, **exactly 13** (the 12 FK operations + the
RLS index).

### 3.3 The residual allow-list (the documented tolerance)

After Phase 3, `alembic check` still exits 255 — by design.
The residual is a **named, closed allow-list**, not a suppressed
check:

| Operation | Count | Why it stays |
|---|---|---|
| `remove_fk` + `add_fk` on the six auth-table FKs | 6 + 6 | NO ACTION (live) vs RESTRICT (declared). Behaviourally identical for non-deferrable FKs; recreating them is churn. The declared RESTRICT is kept because v0.1/CL-12/CL-15 require it — the model must not be weakened to match the live default. |
| `remove_index ix_merchant_memberships_user_active` | 1 | RLS-layer index owned by `rls.sql`; no model counterpart by design. |

**Total: 13 operations on a provisioned database, 12 on an
Alembic-only database.** The allow-list is pinned by the live
test `test_drift_is_exactly_the_documented_allow_list`
(`tests/test_migration_phase3_live_pg.py`), which fails on any
other operation kind, any other count, or a clean pass (0 ops).
If the signature ever changes, re-derive this inventory — do not
widen the test. `alembic check` is still **not** a CI gate (CI
itself is billing-locked); the gate is the test above, run
locally with a live database.

**Not done, and why:** no corrective migration for the FK
representation (churn, exclusive locks, zero behavioural
change); no removal of the RLS index from the allow-list (it is
a real, needed index); no suppression of `alembic check`
output. The Phase 2 options are resolved as: **Option A
(align) applied only to the one genuine mismatch (0007) and to
metadata-only alignment; Option B (allow-list) applied to the
two representation-only residuals above; Option C (tolerate)
rejected** — the drift is now inventoried and pinned, not
merely tolerated.

## 4. Downgrade hazard — 0002 (proven at CHECK level)

The 0002 downgrade (re-adding 0001's identity CHECKs) fails on a
row shape where S is non-NULL, V_R is NULL, and V_cond is non-NULL:
the row inserts fine under 0002 but re-adding the old CHECK fails.
**No such rows exist in `courierledger_app` today** (0 of 4 result
rows — pre-rebuild count; the rebuilt database holds no result
rows). The hazard is a data-shape hazard, not a present failure.
Production stance: **no downgrades in production** — restore from
backup instead of downgrading. All revisions still carry
downgrades for local development use. The new 0007 downgrade
(drops `uq_memberships_merchant_id`) has no data hazard: dropping
a unique constraint cannot fail on data.

## 5. Migrator-role model (Workstream 2 — DECIDED Phase 3)

**Decision: Option 2 — `courierledger` is the live migrator;
`courierledger_migrator` stays reserved.** (Phase 2 left this
open; Phase 3 resolves it. Full evaluation in
`DATABASE_OPERATIONS.md` §3.1.)

- Live migrator: `courierledger` — LOGIN, not superuser, no
  BYPASSRLS, owns both databases and all 33 tables in each.
  `MIGRATION_DATABASE_URL` names it; `alembic/env.py` reads
  that variable and never the runtime URL.
- Reserved: `courierledger_migrator` — NOLOGIN, 0 grants,
  0 objects, **no password verifier** (`pg_authid.rolpassword
  IS NULL`, verified live; see the `pg_roles` trap below).
- Runtime: `courierledger_runtime` — LOGIN, not superuser, no
  BYPASSRLS, owns nothing, 74 privilege rows, **0 DELETE**.
  The application connects as this role only
  (`DATABASE_URL` in `.env` names it; `app/config.py`'s default
  URL names it; the app never reads `MIGRATION_DATABASE_URL`).
- Maintenance: `courierledger_maintenance` — NOLOGIN, no
  password verifier, SELECT on all tables, DELETE only on
  `idempotency_keys`.
- FORCE RLS: 24 tables; RLS enabled: 26. No application
  connection uses a superuser.

**Live proof (rebuilt cluster, `courierledger_app`):** role
state read from `pg_authid` (the correct source — see trap);
ownership 33 / 0 / 0; negative probes as the runtime role —
`CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, `DELETE FROM
idempotency_keys`, `ALTER TABLE … DISABLE ROW LEVEL SECURITY`
all fail with 42501; `SELECT` without tenant context returns 0
rows; `rolbypassrls` is false; `GRANT`/`REVOKE` by the runtime
role changes nothing (PostgreSQL answers a non-owner GRANT with
a WARNING and no change — the probe asserts the unchanged ACL:
74 privilege rows before and after); login as
`courierledger_migrator` or `courierledger_maintenance` fails
(`FATAL: password authentication failed`). All of this is
pinned by `tests/test_migration_phase3_live_pg.py` (12 tests,
all passing).

**The `pg_roles` trap (found Phase 3, corrects a Phase 2
record):** Phase 2 recorded both NOLOGIN roles as "carrying
password verifiers", citing `pg_roles`. That check cannot
establish it: the `pg_roles` view defines `rolpassword` as the
constant `'********'` for every role (verified from the view
definition), so `rolpassword IS NOT NULL` on `pg_roles` is true
for all roles, with or without a verifier. The correct source
is `pg_authid` (superuser-only). Whatever was there before,
`roles.sql` now sets `PASSWORD NULL` on both NOLOGIN roles on
every application, and the post-Phase-3 state is verified in
`pg_authid`: both NULL; the two LOGIN roles hold SCRAM
verifiers. Whether the pre-Phase-3 verifiers existed cannot be
established retroactively — the nulling already ran — and is
recorded here honestly rather than asserted either way.

## 6. Verification hooks added in Phase 3

- New live test `tests/test_migration_phase3_live_pg.py` (12
  tests): head is 0007; `uq_memberships_merchant_id` exists with
  the exact definition; live-migrator ownership and flags;
  reserved migrator has no grants/objects; NOLOGIN verifiers are
  NULL in `pg_authid`; runtime owns nothing / cannot bypass RLS /
  holds no DELETE; runtime DDL and privileged-DML negative
  probes (42501); runtime GRANT/REVOKE no-op (ACL unchanged at
  74 rows); maintenance DELETE restricted to `idempotency_keys`;
  FORCE RLS 24 / enabled 26; FK `confdeltype` pin (6 NO ACTION,
  79 RESTRICT); drift allow-list exact (`alembic check` after
  real server traffic). All 12 pass.
- New static tests in `tests/test_migration_sql.py` (2 added,
  9 total in the file, all passing): `roles.sql` nulls both
  NOLOGIN passwords and records the Option 2 decision; the
  app's default connection is the runtime role and only
  `alembic/env.py` reads `MIGRATION_DATABASE_URL`.
- Updated head assertions (expected maintenance, not weakening):
  `test_infrastructure_phase2_live_pg.py::test_migration_head_is_0007`
  and `test_runtime_role_live_pg.py::test_alembic_uses_migration_database_url`
  (0006 → 0007; both files say the assertion "must move with
  the next migration"). Results: phase2 file 4 passed;
  runtime-role file 5 passed (each run alone).
- `provision_db.sh` runs a verification block after provisioning
  (head, tables, policies, functions, triggers, couriers) and
  fails loudly if any count differs; it now also pre-creates
  the runtime role (see §2 D).
- `verify_restore.sh` re-verifies the alembic version and object
  counts on the restored database. (Not re-run in Phase 3; the
  restore drill belongs to the backup stream.)

## 7. What migration verification still lacks

- No CI gate runs `alembic check` (and it would currently fail —
  §3 — by design, with the residual allow-list pinned by a live
  test instead). CI itself is billing-locked.
- No automated test asserts the provisioning order end-to-end
  (the script encodes it; the test is the manual scratch-DB run
  in §2 D). The runtime-role pre-creation fix (§2 D) has no
  dedicated test beyond that run.
- No test proves a downgrade on a populated database — by design
  (§4: downgrades are not a production recovery path). The 0007
  downgrade was not executed live; it is a single
  `DROP CONSTRAINT` with no data hazard, stated as implemented
  but not verified.
- `INFRASTRUCTURE_TEST_REPORT.md` still records head 0006 and
  Phase 2 counts; it belongs to another stream and was not
  edited here.
