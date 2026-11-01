Section: Cover and scope guard
Claim: v0.3 supersedes only what it names in v0.1 (36 pp) as amended by v0.2; no code, migrations, tests passed, or M1 authorisation.
Source: verbatim request; v0.2 front matter pattern.

Section: Amendment at a glance
Claim: CL-13 to CL-16 resolved at design level with two new tables only where justified; CL-17 is a clarification correcting CL-01 wording; OD-16 stays open and OD-17 is newly opened.
Source: request CL-13..CL-17; v0.2 at-a-glance table.

Section: CL-13 Match decision branching and concurrency
Claim: A candidate's decisions form one linear chain - single root, at most one successor per decision (partial unique index), same-candidate composite self-FK, transaction advisory lock on the candidate as serialisation point; cycles are structurally impossible in an insert-only table whose links point only to pre-existing rows; conflicting roots/heads are preserved, derived as conflicted with no current decision, quarantined, and resolved by unanimous-rejection closure or a successor candidate; line-level double-confirmation resolves by advancing one candidate's own chain.
Source: v0.2 CL-03/CL-07 (shipment_match_decisions, supersedes_decision_id, deferred trigger for second confirmed); v0.1 candidate table (no unique on line+shipment+basis).

Section: CL-14 Idempotency recovery
Claim: Lease (minutes, fencing claim token, attempt count) is separate from key expiry (24 hours per CL-04); completion of the key and the work commit in one transaction, eliminating the crash window between them; abandoned claims are reclaimed by one atomic conditional update; a zombie worker cannot complete because its token no longer matches; completed replay, failed retry and hash conflict keep CL-04 semantics.
Source: v0.2 CL-04 lifecycle (claim txn, in-work-txn completion, failed compensation, expiry); CL-05 run_hash as a second deduplication layer.

Section: CL-15 Workspace anonymisation and tenant integrity
Claim: The anonymised retention workspace is the same merchant row tombstoned in place - merchant_id never moves, so FKs, RLS, run_hash scope and append-only history are untouched; a shared retention tenant and merchant_id rewriting are rejected with reasons; user anonymisation is global-eligibility based, so a shared user with any active membership elsewhere is never anonymised by one workspace's closure; personal data inside append-only raw rows cannot be scrubbed without breaking immutability and is escalated as OD-17, not silently resolved.
Source: v0.2 CL-12 two-stage model and OD-16; v0.1 Sections C/G (merchants, users, orders columns, import_rows.raw_values, FK RESTRICT).

Section: CL-16 Immutable bank variance history
Claim: The single mutable bank_variance_poisha column plus audit JSON cannot hold history, so one new append-only table settlement_variance_calculations plus one typed inputs link table are justified; current variance is derived from the latest calculation only while its preconditions still hold on net totals (CL-10 rule preserved); the stored column on settlement_batches is removed from the design.
Source: v0.1 D.10 and settlement_batches; v0.2 CL-10 (variance superseded by NULL, history in audit record); task condition on new tables.

Section: CL-17 Cross-table constraint clarification
Claim: A PostgreSQL CHECK is row-local and can never inspect another table; CL-01's matched-over-live-finding rule is therefore a deferred constraint trigger, and every cross-row/cross-table invariant in v0.1/v0.2 is mapped to its real mechanism; PostgreSQL has no CREATE ASSERTION, so "transaction-level assertion" in this specification means a deferred constraint trigger or a mandatory in-transaction validation the trigger backstops.
Source: v0.2 CL-01 section 4 wording ("CHECK (or deferred constraint trigger, ADAPT)"); v0.1 H.5; CL-08/CL-10 deferred triggers.

Section: Consolidated test changes
Claim: T-25 revised; T-26 to T-30 added; no test claimed passed.
Source: request requirement per finding.

Section: Appendix delta, change log, remaining blockers
Claim: Traceability rows updated; blockers B-1 to B-4 unchanged; OD-12, OD-16, OD-17 and the CL-11 deployment check remain open for evidence or legal/policy reasons.
Source: v0.2 change log and blockers.
