# CourierLedger M1 — Core Database (Milestone 2)

PostgreSQL target. SQLAlchemy 2.x models + one Alembic initial migration +
RLS / trigger / role SQL, in this directory and `backend/alembic/`.

> **Gating reminder:** this is database code for a gated milestone. It
> authorises no production deployment, no production data, and no M1
> production scaffolding beyond this milestone's scope. Tests and fixtures
> use synthetic data only.

## Files

| File | What it is |
|---|---|
| `app/db/models.py` | All 32 tables as SQLAlchemy 2.x typed models (`Base.metadata` is the single Alembic target). Identity tables are imported from `auth_models.py`, not duplicated (see below). |
| `app/db/auth_models.py` | Auth-owned identity tables (`users`, `merchants`, `merchant_memberships`, `user_sessions`, `password_reset_tokens`) + shared `Base`. The DB-models agent made three minimal additive changes here (see "Reuse of auth_models"). |
| `app/db/session.py` | Auth-owned engine/session helpers incl. `set_tenant_context()` (CL-06/CL-11). Read and reused; **not** overwritten by this milestone. |
| `app/db/rls.sql` | `app_current_merchant_id()` + one RLS policy per tenant table. Applied by the migrator as a separate reviewed step. |
| `app/db/triggers.sql` | Append-only guards + CL-01/CL-03/CL-10/CL-13/CL-16 deferred constraint triggers + CL-13 current-decision derivation. Applied by the migrator as a separate reviewed step. |
| `app/db/roles.sql` | `courierledger_migrator` / `courierledger_runtime` / `courierledger_maintenance` roles and grants; CL-19/CL-24 idempotency predicate table; CL-20/CL-25 lock protocol statement. Applied by a superuser at provisioning. |
| `alembic.ini`, `alembic/env.py` | Alembic config; env-based `DATABASE_URL`, offline + online modes. |
| `alembic/versions/0001_initial_m1.py` | Single initial migration: all 32 tables, indexes, constraints, RLS ENABLE + FORCE. Generated from `models.py` metadata (`/tmp/gen_migration.py`, since removed from the workspace) and hand-reviewed. |
| `tests/test_models.py` | Model registry / money / tenant-key / amendment-traceability tests. |
| `tests/test_migration_sql.py` | Offline Alembic SQL-generation tests + SQL-file content tests. |

## The 32 tables

`users`, `merchants`, `merchant_memberships`, `user_sessions`,
`password_reset_tokens`, `couriers`, `merchant_courier_accounts`,
`courier_statement_templates`, `column_mappings`, `courier_rule_versions`,
`courier_rule_evidence`, `orders`, `shipments`, `file_objects`,
`import_batches`, `import_rows`, `import_errors`, `statement_lines`,
`shipment_match_candidates`, `shipment_match_decisions`,
`reconciliation_runs`, `reconciliation_results`,
`reconciliation_fee_lines`, `reconciliation_findings`,
`settlement_batches`, `bank_receipts`, `settlement_allocations`,
`settlement_variance_calculations`,
`settlement_variance_calculation_inputs`, `workspace_settings`,
`audit_logs`, `idempotency_keys`.

### Count discrepancy (genuine, not hidden)

The brief says "31 total (29 + 2 variance tables)" but its own table list
names **32** tables: it includes `password_reset_tokens`, which is not in
v0.1 section C (v0.1 keeps only `users.password_hash`). This build
implements all 32 named tables and asserts the count (32) in
`test_table_count_is_32_and_matches_registry`. The spec's own count is
31 (28 in v0.1 + 1 `shipment_match_decisions` in v0.2 + 2 variance
tables in v0.3). Dropping `password_reset_tokens` to hit 31 would have
deleted a table the brief explicitly listed, so the discrepancy is
recorded here for the coordinator instead.

## Design decisions (all in `models.py`, traceable to the spec)

- **Money:** every amount column is `BIGINT` named `*_poisha` (39 such
  columns). No FLOAT/REAL/NUMERIC money column exists. No money column
  carries a DEFAULT, with **one documented exception**:
  `workspace_settings.tolerance_poisha` has DEFAULT 0 because v0.1 C.7
  mandates it (a typed configuration control, not a calculated amount;
  the approved default tolerance is 0 poisha, set in settings).
  `reconciliation_runs.tolerance_poisha` has **no** default (v0.1 C.5).
  The test `test_no_money_column_has_a_default_except_documented_tolerance`
  asserts exactly this one exception.
- **NULL vs zero:** NULL means unknown / not yet calculable, never zero
  (CL-02 state-to-value CHECKs on every amount/state pair; fee lines
  carry three per-basis states, CL-08).
- **Enumerations:** TEXT + CHECK constraints, used consistently for every
  status/code column. The spec states every enumeration as a CHECK list;
  no PostgreSQL ENUM type is introduced, so a closed-list change is a
  reviewed migration and the CHECK text stays visible in review.
- **Tenancy:** `merchant_id` on every tenant-owned table; children
  reference parents by composite FKs `(merchant_id, parent_id)` against
  `UNIQUE (merchant_id, id)` on every tenant-owned parent (v0.1 A.7/R2).
  All FKs are `ON DELETE RESTRICT` (CL-12/CL-15).
- **PKs:** application-generated UUIDs (`uuid4`). **Timestamps:**
  TIMESTAMPTZ, `server_default now()` on M1 core tables.
- **Tenant-context setting name:** `app.current_merchant_id`
  everywhere (models docs, `rls.sql`, `session.py`). NOTE: v0.1 E.2
  names the setting `app.merchant_id`; this build uses
  `app.current_merchant_id` because `app/db/session.py:set_tenant_context()`
  (auth agent, pre-existing) already sets that name. Do not mix the two.

## Reuse of auth_models (not duplication)

`backend/app/db/auth_models.py` already existed when this milestone was
built (auth agent, Milestone 1). Per the brief, its `User`,
`Merchant`, `MerchantMembership`, `UserSession` and `PasswordResetToken`
definitions are **imported and reused** in `models.py` on the same shared
`Base` -- they are not redefined. `Base.metadata` therefore holds all
32 tables and is the single Alembic target. The DB-models agent made
only these additive changes to `auth_models.py`, each recorded here:

1. `merchants.anonymised_at` (nullable TIMESTAMPTZ) + CHECK
   `anonymised_at IS NULL OR status = 'closed'` -- CL-15 tombstone column,
   required by the spec and absent from the auth definition.
2. `UNIQUE (merchant_id, id)` on `merchant_memberships` -- tenant-parent
   pattern uniformity (v0.1 A.7/R2); harmless, `id` is already the PK.
3. `ondelete="RESTRICT"` on all six auth FKs -- CL-12/CL-15 requirement.

**Known deviations of the reused auth definitions from v0.1 section C**
(the DB layer follows the auth implementation because the auth service
and its tests depend on it; resolving them is a coordinator decision
with the auth agent, not a silent edit by this milestone):

| Deviation | Spec (v0.1 C.1) | auth_models as built |
|---|---|---|
| Session token column | `user_sessions.refresh_token_hash` | `token_hash` (SHA-256 of a server-managed opaque session token; the M1 session model is opaque tokens, not refresh tokens) |
| Membership default status | DEFAULT `'invited'` | DEFAULT `'active'` (workspace-create flow creates active owner memberships) |
| Timestamp defaults | `DEFAULT now()` (server) | Application-side `_utcnow` Python defaults |
| `merchants` CHECK | name length 1-200, status list | Same, plus the CL-15 anonymised CHECK above |
| Email | stored lower-cased, UK | `String(320)` unique; lower-casing is an application rule in the auth service |

Also: `column_mappings.canonical_field` CHECK lists exactly the ten
fields v0.1 C.2 names. The spec says "and the remaining fields enumerated
in the implementation ticket"; no such ticket was supplied with this
build, so the list is the ten named fields and extending it is a
reviewed migration.

## CL-01..CL-26 traceability

| ID | Subject | Implemented in |
|---|---|---|
| CL-01 | Scenario-only V_total -> `open`, never `mismatch`; five-value overall status | `models.py` (`ReconciliationResult` CHECK + identity CHECKs); `triggers.sql` `fn_result_status_check` (deferred) |
| CL-02 | Negative-payout state model; `present_valid` = NOT NULL, zero only via `explicit_zero` | `models.py` (`StatementLine` payout CHECK + `negative_payout_permitted` snapshot; `CourierRuleVersion.negative_net_payout_permitted`) |
| CL-03 | Candidates strictly insert-only; decisions in new append-only table | `models.py` (`ShipmentMatchCandidate` has no decision columns; `ShipmentMatchDecision`); `rls.sql` (no UPDATE/DELETE policies); `triggers.sql` append-only guard + `fn_confirmed_candidate_line_check` |
| CL-04 | `idempotency_keys` sole owner; typed result refs; `import_create`/`run_create` only | `models.py` (`IdempotencyKey`; `idempotency_key` columns removed from `import_batches`/`reconciliation_runs`); `roles.sql` |
| CL-05 | Deduplication on `UNIQUE (merchant_id, run_hash)` is the single behaviour | `models.py` (`ReconciliationRun`) |
| CL-06 | Malformed tenant UUID returns NULL, never raises | `rls.sql` `app_current_merchant_id()`; `session.py:set_tenant_context()` (pre-existing) |
| CL-07 | Immutable decision history, audit mirror | `models.py` (`ShipmentMatchDecision`); `triggers.sql` |
| CL-08 | Three per-basis fee-line DQ states | `models.py` (`ReconciliationFeeLine`: `expected_basis_state` / `reported_basis_state` / `reported_actual_state` + per-basis CHECKs) |
| CL-09 | Multiple components per fee category | `models.py` (`component_seq`, `source_statement_field`, key `(result_id, fee_code, component_seq)`) |
| CL-10 | Allocations: `is_active` deleted; `kind` allocation/reversal; single reversal; over-allocation backstop | `models.py` (`SettlementAllocation`); `triggers.sql` `fn_allocation_overage_check` (deferred, mandatory) |
| CL-11 | Trust boundary of application-set tenant context | `rls.sql` header; `README` (context set only from authenticated membership resolution; no default tenant) |
| CL-12 | Two-stage closure-then-anonymisation; `ON DELETE RESTRICT` | `models.py` (all FKs RESTRICT; `merchants.closed_at`); `roles.sql` (maintenance-only Stage 2) |
| CL-13 | Linear match-decision chain; max one successor; deterministic derivation incl. malformed states | `models.py` (composite supersedes FK, partial unique successor index, `prior_candidate_id`); `triggers.sql` `fn_current_match_decision` + `fn_match_decision_chain_check` |
| CL-14 | Idempotency lease/fencing: `lease_expires_at`, `claim_token`, `attempt_count`, `claimed_by_instance`; two clocks | `models.py` (`IdempotencyKey` + state-to-column CHECK); `roles.sql` predicate table; **implemented + verified live 2026-10-31 in `app/services/idempotency_service.py` (B-3)** |
| CL-15 | Tombstone merchant; shared-user rule | `models.py` (`merchants.anonymised_at`); `roles.sql` (Stage 2 is maintenance-only) |
| CL-16 | Variance as append-only calculation records; stored `bank_variance_poisha` removed | `models.py` (`SettlementVarianceCalculation`, `SettlementVarianceCalculationInput`; no stored variance on `settlement_batches`); `triggers.sql` `fn_variance_calculation_check` |
| CL-17 | Cross-table consistency is a deferred constraint trigger, not a CHECK | `triggers.sql` (all cross-table invariants are `DEFERRABLE INITIALLY DEFERRED` constraint triggers) |
| CL-18 | CL-13 trigger scoped to the delta; repair paths not blocked | `triggers.sql` `fn_match_decision_chain_check` (delta via `xmin`) |
| CL-19 | Purge eligibility by state; renewal only while lease live; exact conditional-update predicates | `roles.sql` predicate table; `models.py` state CHECK. No schema change (CL-19 states none). **Predicates implemented + verified live 2026-10-31 in `app/services/idempotency_service.py` (B-3).** |
| CL-20 | Uniform per-user advisory lock for membership/session/Stage 2 | Application-protocol duty, stated in `roles.sql` + this README (not a DB object). Lock ordering: ascending lock-key order (CL-20). |
| CL-21 | Batch-scoped variance input FKs; current variance = input-set equality | `models.py` (`settlement_batch_id` on inputs + batch-scoped FKs + supporting uniques on receipts/allocations/calculations); `triggers.sql` `fn_variance_calculation_check` |
| CL-22 | Six-rule validation-trigger context pattern | `triggers.sql` header + every validation function (SECURITY DEFINER, migrator-owned, merchant from firing rows, explicit tenant predicates, fail-closed anchor, no BYPASSRLS) |
| CL-23 | Replica-mode fixture route removed; load-before-trigger is sole route | `triggers.sql` header (no `session_replication_role` mechanism anywhere in this milestone) |
| CL-24 | Uniform ownership rule: renewal/completion/failure all require in_progress + token + live lease | `roles.sql` predicate table (revised completion/failure rows); **implemented + verified live 2026-10-31 in `app/services/idempotency_service.py` (B-3)** |
| CL-25 | Session renewal/refresh/extension/reactivation take the per-user lock; pure revocation/deletion do not | Application-protocol duty, stated in `roles.sql` + `models.py` (`UserSession` docstring) |
| CL-26 | Rule 7: function-level `search_path`, schema qualification, no dynamic SQL, explicit EXECUTE revocation | `triggers.sql` (every function: `SET search_path TO public, pg_catalog`, `public.`-qualified references, no dynamic SQL). Note: `roles.sql` grants EXECUTE on `app_current_merchant_id()` to runtime/maintenance only; no EXECUTE grant on validation functions is given to the runtime role. |

## Lease-clock rule (read this before touching idempotency)

PostgreSQL `now()` is **transaction-start time**. Every lease
comparison in this design (CL-14/CL-19/CL-24 predicates in `roles.sql`)
MUST use **`clock_timestamp()`** (statement-execution wall clock).
The spec's ADAPT notation writes `now()`; this build deliberately uses
`clock_timestamp()` instead, because a long work transaction would
otherwise see a stale clock and a worker could complete on an expired
lease. A worker that lets its lease lapse mid-operation loses the
operation; completion is the last write of the work transaction and a
zero-row completion rolls the whole work transaction back (CL-24).
**Implemented 2026-10-31 (B-3):** `app/services/idempotency_service.py`
is the only module that updates `idempotency_keys`, and its predicates
use `clock_timestamp()`; verified live, including the decisive
`pg_sleep(3)` test (see `m1/IMPLEMENTATION_CHECKLIST.md`).

## Verification status

### VERIFIED (commands run 2026-10-31, this session)

| Check | Command | Output |
|---|---|---|
| Model import + registry | `.venv/bin/python -c "from app.db.models import Base; ..."` | 32 tables, `ALL_TABLES` matches, 39 `*_poisha` columns all BIGINT, no FLOAT/NUMERIC |
| New tests | `.venv/bin/python -m pytest tests/test_models.py tests/test_migration_sql.py -q` | **29 passed** |
| Full backend suite (no regressions to other agents' tests) | `.venv/bin/python -m pytest -q` | **146 passed** (runtime role, live modules included; was 90 before B-1/B-4/B-3/M4) |
| Offline migration SQL | `DATABASE_URL='postgresql+psycopg://courierledger:change-me@localhost:5432/courierledger' .venv/bin/alembic upgrade head --sql` | 33 `CREATE TABLE` (32 M1 + `alembic_version`), 26 `ENABLE ROW LEVEL SECURITY` + 26 `FORCE ROW LEVEL SECURITY` (merchants + 25 tenant tables), partial unique indexes and composite FKs present, `email LIKE '%@%'` correct (see note below) |
| PostgreSQL syntax parse of all SQL (pglast, in a scratch venv -- not the project venv) | `pglast.parse_sql()` on `rls.sql`, `triggers.sql`, `roles.sql`, and the offline migration SQL | All parse OK: rls.sql 92 statements, triggers.sql 51, roles.sql 54, migration SQL 136. This validates SQL syntax only -- it is NOT execution and does not verify trigger logic, RLS behaviour, or grants. |
| SQLite DDL-shape smoke | `test_sqlite_create_all_smoke_only` (in the pytest run above) | all 32 tables create + drop on SQLite (shape only) |

Note on `email LIKE '%@%'`: SQLAlchemy's PostgreSQL compiler doubles
`%` in literal CHECK text (`%%@%%`) when rendering DDL. The migration
generator un-escapes it back to a single `%`, and the offline SQL above
confirms the emitted DDL is `email LIKE '%@%'`. The ORM definition in
`auth_models.py` is unchanged (single `%` is correct there).

### UNVERIFIED (no PostgreSQL server was available in this session)

- `psql --version`: **command not found**; no server binaries under
  `/usr/lib/postgresql`; port 5432 closed. (An `apt install` was said to
  be running in the coordinator session; it had not produced a server
  in this VM at build time.)
- `alembic upgrade head` (live) -- **not run**. Run it only when a real
  PostgreSQL is reachable, as the migrator role:
  `DATABASE_URL='postgresql+psycopg://<migrator>:<password>@<host>:5432/courierledger' .venv/bin/alembic upgrade head`
  then `alembic current` must show `0001`.
- `rls.sql` / `triggers.sql` / `roles.sql` -- **written, never executed**.
  They are real, complete SQL, but no statement in them has run against
  a server. In particular the PL/pgSQL in `triggers.sql` (CL-01
  re-derivation, CL-13 delta-scoped chain check, CL-16 variance
  backstop) and the `xmin::text::bigint = txid_current()` delta
  identification (CL-18) must be proven in the B-3 verification
  environment before being treated as verified.
- Live smoke test (create merchant + user, verify RLS blocks a
  cross-tenant read under a different tenant context, verify the
  append-only trigger rejects an UPDATE on `reconciliation_runs`) --
  **not run, no server**. Do not claim it passed.
- SQLite is **not** a substitute for any of the above: the SQLite
  `create_all` smoke test exercises DDL shape only, and the
  application-level SQLite fallback in `app/config.py` /
  `app/db/session.py` (another agent's test-only convenience) is not
  the M1 data model.

### How to verify when PostgreSQL exists (for the coordinator)

1. Provision PostgreSQL, create the three roles from `roles.sql`
   (passwords from the Secure Vault, never committed).
2. As migrator: `alembic upgrade head`, then apply `rls.sql`,
   `triggers.sql`, `roles.sql` in that order.
3. As runtime, with context set to merchant A: SELECTs return only A's
   rows; with context unset/malformed: zero rows, no exception (T-22).
4. As runtime: UPDATE/DELETE on an append-only table affects zero rows
   or is denied (T-11/T-21); UPDATE on `reconciliation_runs` changing
   anything but `status`/`completed_at` is rejected by the trigger.
5. Run the spec test scenarios T-10/T-12/T-15b/T-15c/T-16/T-17/T-21/
   T-26/T-27/T-28/T-30/T-31 against the live database (they are
   application/transaction-level scenarios; this milestone provides the
   schema, triggers and predicates they depend on, not the test code).

## Open decisions carried forward (v0.1 section J + amendments)

- **OD-11** -- "at least one active owner" per workspace is an
  application rule, not a database constraint.
- **OD-12** -- whether any real courier statement reports a negative
  net payout. Open until redacted merchant statements confirm or refute
  it. The schema can represent it only under an evidence-backed,
  merchant-confirmed permission (`negative_net_payout_permitted`,
  default False); until then negative payouts are invalid (CL-02).
- **OD-16** -- workspace grace window, Bangladesh financial retention
  period, and the erasure-vs-retention conflict rule (CL-12/CL-15).
  Blocks production launch, not design review.
- **OD-17** -- third-party personal data inside append-only raw records
  (`import_rows.raw_values`, statement lines) after anonymisation
  (CL-15). Interim position: retained raw records of a tombstoned
  workspace are sealed -- unreachable through the runtime role,
  excluded from anonymised exports, accessed only under the break-glass
  procedure (itself unauthorised until production-readiness approval).
- Also still open from v0.1 J (not blocking this milestone):
  OD-03 (identity provider choice), OD-05 (file-storage provider),
  OD-07 (merchant template overrides), OD-08 (merchant order-ref
  uniqueness -- refs are indexed, NOT unique, in this build),
  OD-09 (bank receipt label digits -- none stored), OD-10 (single
  active rule version per account -- partial unique index is an
  application/activation-transaction concern), OD-13 (idempotency
  expiry window -- 24h design default, CL-04/CL-14).

## Genuine spec contradictions found (isolated, not silently resolved)

1. **Count: brief says 31, brief's list names 32.** See "Count
   discrepancy" above. No table was dropped to force the numbers to agree.
2. **Tenant-context setting name: v0.1 E.2 says `app.merchant_id`;
   the brief and the pre-existing `session.py` say
   `app.current_merchant_id`.** This build uses `app.current_merchant_id`
   everywhere. If the spec name is preferred, the app and `rls.sql`
   must change together.
3. **Idempotency operations: v0.1 C.7 lists four operations
   (`import.create`, `run.create`, `receipt.create`, `allocation.create`)
   with dotted names; v0.2 CL-04 supersedes this with two operations
   (`import_create`, `run_create`) and typed result references.** This
   build follows CL-04 (later amendment wins). Receipt/allocation
   idempotency would need a schema change (new typed column + CHECK
   extension), not a new string.
4. **`workspace_settings.tolerance_poisha` DEFAULT 0 vs the "no money
   column with a DEFAULT" rule.** v0.1 C.7 mandates DEFAULT 0; the
   brief's model rule forbids money defaults. Kept the spec's DEFAULT 0
   for this one configuration control and asserted it as the single
   exception in tests (see "Design decisions").
5. **Auth-owned identity tables deviate from v0.1 C.1** (token column
   name, membership default, timestamp defaults). See the table in
   "Reuse of auth_models". These pre-date this milestone; the DB layer
   follows the running auth implementation rather than re-litigating it
   here.
6. **`now()` vs `clock_timestamp()` for leases.** The spec's ADAPT
   notation writes `now()`; `now()` is transaction-start time and is
   wrong for lease comparisons. This build uses `clock_timestamp()`
   (see "Lease-clock rule"). If the spec notation is treated as
   normative, that is a spec correction to record, not a code change to
   make silently.
7. **CL-01 enforcement wording (v0.2) vs CL-17 (v0.3).** v0.2 CL-01 item 4
   says "a consistency CHECK (or deferred constraint trigger)"; v0.3
   CL-17 corrects this: a CHECK cannot do it, it must be a deferred
   constraint trigger. This build implements the trigger (CL-17 wins).
8. **No `%%` issue is a spec contradiction, but a rendering trap is
   recorded:** see the `email LIKE` note under VERIFIED.

## What this milestone deliberately does NOT do

- No changes to `frontend/`, `prototype-p0/` (read-only), or
  `backend/app/main.py`, `config.py`, `security.py`, `api/` (another
  agent owns those). `backend/app/db/session.py` was read and reused,
  not overwritten.
- No production deployment, no credentials, no real courier rates or
  statement formats (every rule must come from merchant-confirmed
  evidence in `courier_rule_evidence`).
- No `password_reset_tokens` duplication: the table is defined once,
  in `auth_models.py`, and reused.

## Addendum — M6 live deferred-trigger verification, 2026-10-31

**Status change:** the deferred triggers for CL-01, CL-13 and CL-16 are
now verified live against PostgreSQL 16.15
(`backend/tests/test_triggers_live_pg.py`, **19 tests, all passing**;
full backend suite in the same run: owner role 183 passed / 3 failed /
5 skipped, runtime role 188 passed / 3 failed — the 3 failures are all
in another worker's `test_runs_live_pg.py`, a seed bug there
(`INSERT INTO orders` without an `id`, `NotNullViolation` on
`orders.id`), unrelated to triggers.sql; see
`m1/IMPLEMENTATION_CHECKLIST.md`). Live tests use the shared harness
(`tests/live_pg.py`, scratch DB `courierledger_live_test`) and skip
cleanly without `CL_LIVE_PG_PASSWORD`.

### CL-01 correction found by live probe (documented, not silent)

The probe confirmed the suspected contradiction in
`fn_result_status_check` as written: the "must be mismatch" branches
fired whenever V_R, S or V_cond was outside tolerance, with **no
exemption** for results that are `needs_review` because a data-quality
finding exists or a required value is NULL. That contradicted v0.2
CL-01 precedence — needs_review FIRST ("If any required value is NULL
or a data-quality finding blocks exact calculation, overall status is
needs_review. Else, if V_R, S or V_cond...") — and its test T-16(d).
Live probe (scratch DB, pre-fix): a DQ finding + S = −1000 outside
tolerance + overall `needs_review` was **rejected** ("must be
mismatch"), and the same values stored as `mismatch` were rejected by
the needs_review-first branch — the legitimate T-16(d) result was
unrepresentable either way. **Fix** (`triggers.sql` §4 only, engine
untouched): both mismatch branches are now gated on
`NOT (v_has_null OR v_has_dq)`, with an in-file comment citing v0.2
CL-01 precedence + T-16(d). Post-fix probe: the T-16(d) row commits;
all other probe verdicts unchanged (V_R-out-as-`open` still raises,
matched-over-live-finding still raises, required-NULL-as-`matched`
still raises). The corrected `triggers.sql` was re-applied to
`courierledger_app` + `courierledger_dev` (and is applied by the live
harness to the scratch DB).

### What the 19 live tests prove

- **CL-01 (6 tests):** valid `matched` / `mismatch` / `open` /
  `needs_review` / `matched_with_tolerance` results commit (each with
  consistent variances + findings; `matched_with_tolerance` uses a
  500-poisha run tolerance, scenario-only V_total = 400 — T-16(e));
  V_R-outside-tolerance stored as `open` raises at commit; T-16(d)
  (DQ finding + S outside tolerance + `needs_review`) commits;
  `matched` with a non-zero finding outside tolerance raises;
  `x_upper_r` NULL with overall `matched` raises. All inserted rows
  satisfy the row-local identities V_cond = S − V_R and
  V_total = C − Δ − V_R + S; `x_upper_r` is X_R (deductions
  recalculated on the reported basis, not the reported total).
- **CL-13 (6 tests) + CL-03 (1 test):** root `confirmed` + successor
  `rejected` in one transaction commits and
  `fn_current_match_decision` returns `rejected` (the head); a second
  successor to the same decision fails at **statement time** via the
  partial unique index `uq_match_decisions_one_successor`; a second
  root fails at **commit** via the chain trigger; a successor naming a
  non-head predecessor fails via the unique index (in a well-formed
  chain every non-head already has a successor, so the index is the
  layer that catches it; the trigger's non-head check is the backstop
  for histories the index cannot see); two currently-`confirmed`
  candidates for one statement line raise at commit (CL-03).
  **Structural limit, recorded not hidden:** a forked history (two
  heads) cannot be created through the guarded insert path — the
  unique index rejects a second successor and the chain trigger
  rejects a second root — so the multi-head unanimous-rejection and
  conflicted branches of `fn_current_match_decision` only fire for
  pre-existing malformed histories loaded before the triggers (CL-23
  load-before-trigger fixtures), which the harness does not do. The
  tests prove the fork attempts fail and that a well-formed rejected
  head derives `rejected`; `fn` returns NULL for undecided (no rows).
- **CL-16 (5 tests):** a calculation whose snapshots agree with the
  batch's current receipt/allocation sets commits (expected 100000,
  receipts 60000 + 40000 complete, allocations 60000 + 40000 with a
  40000 reversal, net 60000, variance −40000); a snapshot disagreeing
  with the listed allocations raises at commit; after adding a new
  receipt to the batch the CL-16/CL-21 current-variance derivation
  (implemented as a SQL query in the test: highest-seq calculation
  whose listed input set exactly equals the batch's current input set
  and whose preconditions still hold) returns NULL while the old
  calculation row stays unchanged (append-only); a batch that is not
  `fully_allocated` raises; an incomplete receipt raises.
- **Append-only (1 test):** UPDATE and DELETE on a
  `reconciliation_results` row raise `append-only table
  reconciliation_results: ... is not permitted` (run as the postgres
  superuser via `su postgres`, so the trigger itself fires — as the
  owner/runtime role the missing UPDATE/DELETE policies would
  silently affect zero rows instead); the row stays intact.

### Environment note (same day)

The container was replaced mid-task (PostgreSQL install vanished: no
`postgres` user, no cluster). PostgreSQL 16.15 was reinstalled in the
new container from the cached apt archives (`dpkg -i` of the cached
`.deb`s, because the system os-intent replay's `apt-get update` was
stalled; the replay's ledger already records postgresql as intended,
so a later replay pass will see it installed and skip it), the cluster
was started, roles/databases were re-provisioned per `m1/README.md`
§3 (alembic head, `rls.sql`, corrected `triggers.sql`, `roles.sql`,
seed; runtime-role password set to the password in gitignored `backend/.env` (a fresh random value since the Phase 1 rotation, 2026-11-01)),
and **all 19 trigger tests + the full suite above were run in the new
container** — no live result in this addendum is carried over from the
replaced container (the pre-replacement probe/fix observations are
recorded in `m1/IMPLEMENTATION_CHECKLIST.md` as the evidence that
motivated the fix; the fix itself was re-verified by the passing
T-16(d) test in the new container).

---

## Addendum — coordinator live-PostgreSQL verification, 2026-10-31 (updated: B-1, B-4, B-3, M4 done)

The "UNVERIFIED — no PostgreSQL server" status above is now partly superseded. PostgreSQL 16.15 was installed via apt (cluster 16 main, port 5432, online) and verified live in this session: `alembic upgrade head` → 33 tables (32 + `alembic_version`); `rls.sql` 45+ policies, `triggers.sql` 21 triggers, `roles.sql` 49 grants, all exit 0; RLS cross-tenant isolation verified as the runtime role (tenant A sees only ORD-A, tenant B only ORD-B, no tenant sees 0 rows, cross-tenant INSERT rejected, malformed UUID fail-closed with 0 rows); append-only trigger verified (superuser UPDATE/DELETE on `audit_logs` both raise `append-only table audit_logs: ... is not permitted`, row intact). **Resolved since the first addendum:** B-1 (membership-based `merchants` SELECT policy + SECURITY DEFINER `public.create_workspace()`, `NO FORCE` on `merchants` + `merchant_memberships` only, `app.user_id` now set by the app — 6 live workspace tests pass under both roles); B-4 (the app now connects as `courierledger_runtime` by default; 5 live runtime-role tests pass, including the privilege-denial and context-isolation checks); B-3 (lease comparisons implemented in `app/services/idempotency_service.py` using `clock_timestamp()`, never `now()` — 6 live lease tests pass, including the decisive `pg_sleep(3)` clock test). M4 (CSV import) is built and verified live (24 SQLite unit tests + 5 live tests; `seed_couriers.sql` seeds the 5 courier codes only). **Now VERIFIED (M6, 2026-10-31):** deferred-trigger behaviour for CL-01/CL-13/CL-16 — see the "M6 live deferred-trigger verification" addendum above (19 live tests pass; one documented CL-01 correction to `triggers.sql`). **Still open:** B-2 Docker — Dockerfiles written and hadolint-clean, `docker compose config` exits 0, but image builds and `docker compose up` cannot run in this VM (containers cannot start: `runc ... setns: operation not permitted`). See `m1/IMPLEMENTATION_CHECKLIST.md` for the full record.
