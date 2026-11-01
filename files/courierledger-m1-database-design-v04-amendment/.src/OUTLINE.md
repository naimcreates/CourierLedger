Section: Cover and reading rule
Claim: v0.4 supersedes only what it names in v0.1/v0.2/v0.3; no code, no migrations, no test claimed passed; M1 gated.
Source: verbatim request; v0.3 cover conventions.

Section: Amendment at a glance
Claim: Five findings CL-18 to CL-22; no new tables; design resolution is not implementation verification.
Source: verbatim request CL-18..CL-22.

Section: CL-18 Trigger enforcement vs malformed-history testing
Claim: Trigger rejects malformation a transaction creates; derivation detects malformation that already exists; T-26(e) moves to a controlled legacy fixture loaded with the trigger not yet created / disabled in a test-only database, never in production.
Source: v0.3 CL-13 sections 2(c), 4, T-26(e).

Section: CL-19 Idempotency purge and lease lifecycle
Claim: Purge only for terminal (completed/failed) keys after expires_at; in_progress never purged; renewal after lease expiry is impossible; exact conditional-update predicates for renewal, reclaim, completion, failure, cap transition and purge.
Source: v0.3 CL-14 (lease, claim_token, attempt cap 5, expires_at 24h, same-transaction completion).

Section: CL-20 Shared-user anonymisation serialisation
Claim: One transaction-scoped advisory lock keyed by user_id, taken by every membership mutation and by Stage 2, with sorted lock order and a recheck of global membership and live sessions under the lock.
Source: v0.3 CL-15 section 4 (membership-row locking, noted as insufficient in the request); OD-16/OD-17 stay open.

Section: CL-21 Same-batch variance inputs and current validity
Claim: Batch equality enforced by composite foreign keys via a settlement_batch_id column on inputs and matching unique keys on receipts/allocations; current variance requires exact input-set equality, so a changed set with an identical net invalidates current variance until a new calculation.
Source: v0.1 bank_receipts (settlement_batch_id nullable), settlement_allocations (batch NOT NULL); v0.3 CL-16 two tables.

Section: CL-22 Trigger execution context under FORCE RLS
Claim: Deferred constraint-trigger functions are SECURITY DEFINER, owned by the migrator/validation owner, derive merchant_id from the firing row, set a validation context only that owner role's policy honours, scope every query explicitly, and fail closed (raise) if the firing row is not visible - never validate an empty set.
Source: v0.3 CL-17 trigger note; PostgreSQL RLS/SECURITY DEFINER behaviour.

Section: Consolidated test changes, traceability delta, change log
Claim: T-26 revised; T-27, T-28, T-30 extended; T-31 and T-32 new; all design-stage.
Source: v0.3 consolidated table (T-17..T-30 in use).

Section: Remaining blockers and schema-freeze statement
Claim: Design is ready to freeze as a review baseline; freeze is not implementation authorisation and must be reopened if the B-3 verification environment contradicts a named mechanism.
Source: v0.3 remaining blockers B-1..B-4, OD-11/12/16/17, CL-11 check.
