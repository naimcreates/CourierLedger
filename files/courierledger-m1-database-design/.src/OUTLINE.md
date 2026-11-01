# Outline - CourierLedger M1 Database Design Specification

Section: Cover + status and gating
Claim: This is a design blueprint only; M1 implementation stays gated behind market validation and production-readiness approval.
Source: verbatim request sections 7-8; MEMORY V2.2.1 / P0 gates.

Section: How to read this document + conventions
Claim: Confirmed decisions, recommendations and open assumptions are labelled separately so nothing is silently promoted.
Source: request section 6A, 6J.

Section: A. Architecture Review
Claim: The draft module list is broadly sound but misses statement lines, fee lines, match candidates and file/idempotency records, and understates RLS failure modes.
Source: request sections 3-5.

Section: B. ERD
Claim: Four Mermaid sub-diagrams cover all entities with PK/FK, cardinality and optionality.
Source: entity list + additions in A.

Section: C. Table-by-Table Specification
Claim: Every table has typed columns, nullability, keys, checks, indexes, tenant class, RLS and immutability.
Source: sections 4-5 invariants.

Section: D. Financial Data Integrity
Claim: BIGINT poisha, NULL vs zero, snapshots, D_c confirmation, five-status model and identities V_total = C - delta - V_R + S and V_cond = S - V_R are preserved exactly.
Source: V2.2.1 + P0 patch in MEMORY.

Section: E. RLS Design
Claim: Tenant isolation needs FORCE RLS, a non-owner runtime role, transaction-local context, composite FKs and pool safety together; merchant_id alone is insufficient.
Source: request section 5.

Section: F. Indexing and Performance
Claim: Index for tenant-first query patterns only; avoid premature optimisation.
Source: request 6F.

Section: G. Lifecycle and Retention
Claim: No legal retention period is invented; all periods are policy/legal decisions.
Source: request 6G.

Section: H. SQLAlchemy and Alembic
Claim: Constraints in the database complement app validation; repositories are tenant-aware by construction.
Source: request 6H.

Section: I. Testing Strategy
Claim: Each required failure mode has a named test scenario with expected result.
Source: request 6I.

Section: J. Open Decisions
Claim: Every unresolved decision lists why it matters, options, a recommended default and whether it blocks implementation.
Source: request 6J.

Section: Appendices - invariants checklist, glossary
Claim: A reviewer can trace every financial invariant to a table, constraint or test.
Source: request section 4.
