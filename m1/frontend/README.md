# CourierLedger — Frontend (M1)

React + TypeScript + Vite + Tailwind CSS v3 + React Router + TanStack Query +
React Hook Form + Zod. Indigo primary `#4F46E5`, light theme, desktop-first.
English UI with short Bangla explanations for financial statuses
(matched = মিলেছে, mismatch = অমিল, needs_review = যাচাই প্রয়োজন, open = খোলা,
matched_with_tolerance = সহনশীলতার মধ্যে মিলেছে).

**Synthetic/demo data only, clearly labelled.** No real merchant data and no
invented courier rates anywhere in this frontend. Real data comes only from
the backend endpoints listed below; sections without an endpoint say so
plainly instead of showing invented figures.

**Milestone 5 (dashboard) built and verified live 2026-10-31** — see "What is
working vs placeholder" below. Backend pytest still 146 passed; frontend
Vitest 6 passed.

**Milestone 6 (frontend, 2026-10-31)** — persisted reconciliation runs are
surfaced in the dashboard (see "Milestone 6 — frontend" below). Frontend
Vitest 12 passed. **Not verified live:** the backend run endpoints do not
exist yet (no `runs.py` in `backend/app/api/` at the time of writing), so no
live run data was fetched — Vitest + build are the verification.

## Commands run (2026-10-31)

```bash
cd ~/workspace/goals/courierledger-planning-and-market-validation/m1/frontend
npm create vite@latest . -- --template react-ts   # create-vite v9.3.3, Node v24.22.1, npm 10.9.4
npm install                                       # added 28 packages
npm install react-router-dom @tanstack/react-query react-hook-form zod @hookform/resolvers
                                                  # added 13 packages
npm install -D tailwindcss@3 postcss autoprefixer  # added 76 packages (Tailwind v3.4.19)
npx tailwindcss init -p                           # created tailwind.config.js + postcss.config.js
npm install -D vitest jsdom @testing-library/react @testing-library/jest-dom
                                                  # added 75 packages
```

## Build — PASSES

```bash
npm run build   # tsc -b && vite build
```

Real output (2026-10-31, Milestone 6):

```
vite v8.3.5 building client environment for production...
✓ 196 modules transformed.
dist/index.html                   0.49 kB │ gzip:   0.32 kB
dist/assets/index-DIn6afEq.css   12.71 kB │ gzip:   3.32 kB
dist/assets/index-DnV6cvFR.js   504.38 kB │ gzip: 145.86 kB
✓ built in 1.24s
```

(The 500 kB chunk-size warning is pre-existing — the bundle was already
481 kB at M5; no code-splitting was added for M6.)

## Tests — 12 passed

```bash
npm test   # vitest run
# Test Files  4 passed (4)
#      Tests  12 passed (12)
```

`src/smoke.test.tsx`: poisha→taka formatting, Bangla labels for all five
statuses, and the Shipments page rendering its synthetic demo table (mocked
`POST /api/reconcile`). `src/imports.test.tsx`: Imports list rendering from
mocked API data in the real M4 shape (2 batches + 1 courier account), and the
no-courier-account message. `src/auth.test.tsx`: an unauthenticated user
hitting `/` is redirected to the login page (mocked `GET /api/auth/me` → 401).
`src/runs.test.tsx` (M6, 6 tests): Reconciliation runs list from the mocked
list payload (2 runs, counts by status, Bangla labels) + the "No
reconciliation runs yet" empty state; run detail rendering the five variances
separately, findings with English + Bangla explanations, fee lines, and the
shipment-id fallback when the backend provides no tracking_id; ImportDetail
showing the run link when a run exists, and "No run yet" + the create-run
form (Fixture Alpha/Beta/Gamma + tolerance 0) when none — including the
create POST body and the backend's own error message on failure.
Playwright is **not** installed and **not** claimed — browser end-to-end
testing is pending.

## Dev server — verified

```bash
npm run dev   # Vite ready in ~300 ms
```

`curl http://localhost:5173/` returns the app shell with
`<title>CourierLedger — COD Courier Reconciliation</title>`.
`GET /api/health` via the dev proxy returns the live PostgreSQL health JSON
(`{"status":"ok","service":"courierledger-m1","database":"postgresql","database_ok":true}`).
Note: Vite binds `localhost` (here `::1`); use `http://localhost:5173`, not
`127.0.0.1`. Servers stopped afterwards.

## Milestone 8 — frontend (2026-11-01)

Built against the M8 brief's contract, then **adapted to the real
backend** (`backend/app/api/shipments.py`, `statements.py`,
`reports.py` + `app/services/shipment_service.py`), which the backend
workers landed during this task. The brief's contract differed from the
real backend in five places, and this section follows the **real
backend**, not the brief (see "Contract deviations found live" below).

- `src/lib/api.ts`: `listOrders`, `createOrder`,
  `createShipmentForOrder`, `listShipments`, `listStatements`,
  `getStatement`, `getReportSummary` — same `apiFetch` conventions,
  CSRF on the POSTs. Orders/shipments paginate with `limit`/`offset`
  (`{orders|shipments, total, limit, offset}`); statements paginate
  with `page`/`page_size`; the report summary is nested with named
  variance totals that are always numbers (0 when empty).
- `src/lib/shipments.ts` (new): Bangla outcome/source labels, the V2.1
  expected-collectible rule in plain English + Bangla, and the
  backend's `needs_review_reason` explanations (EN+BN).
- `src/pages/Shipments.tsx`: real shipments list (paginated, filter by
  tracking id — exact normalised match on the backend) with expected
  collectible shown as **NULL — unknown, not ৳0.00** where NULL and
  **৳0.00 (verified zero)** for confirmed returns, plus the backend's
  needs-review badge + reason, COD-bearing badge, and outcome source;
  create-order form (COD optional — blank stays NULL) and
  add-a-shipment form (outcome, outcome source, COD-bearing checkbox,
  amount to collect only for delivered/partially-delivered, partial
  amount evidence *reference* — never a `partial_amount_poisha` field,
  which the backend rejects with 422 — optional courier account,
  shipped-on date). The labelled synthetic demo reconcile run stays on
  the Reconciliation page; nothing demo is mixed into this list.
- `src/pages/CourierStatements.tsx`: real statement-line list
  (paginated, filter by import batch + match status incl.
  `human_confirmed`) with match badges + Bangla labels and DQ states,
  and a detail view fetched from `GET /statements/{lineId}` showing
  raw values verbatim, parse/match status, the linked shipment
  (tracking, outcome, expected collectible), the courier account and
  the import batch. Reported money uses the shared `MoneyCell` from
  `ImportDetail.tsx` (exported for reuse, not redefined).
- `src/pages/Reports.tsx`: real aggregates from
  `GET /reports/summary` — runs by status (running/completed/failed),
  results by the five overall statuses with Bangla explanations,
  import batches by status, settlement batches by allocation + bank
  status, bank receipts complete/incomplete, and the five variance
  totals (C, V_R, S, V_cond, V_total) each in its own card with its
  known/unknown counts, **never summed** — there is no combined
  "total difference" figure. Empty workspace: counts 0, variance
  totals ৳0.00 over 0 results, with a "Nothing to report yet" note.
- Tests: `src/milestone8.test.tsx` — 13 tests (shipments list incl.
  NULL vs verified-zero, tracking filter with `limit`/`offset`,
  create-order taka→poisha + blank-COD NULL, partial-delivery POST
  body with evidence ref and no `partial_amount_poisha`, no amount
  sent for a returned shipment, statements list + filter + detail via
  the detail endpoint, reports aggregates incl. separate variance
  totals with known/unknown counts and the empty state, auth-gate
  redirect for all three new pages). `src/smoke.test.tsx` drops the
  old Shipments demo-table test (the demo table no longer lives on
  that page); the demo-helper tests stay.

**Contract deviations found live (brief vs real backend):** (1)
orders/shipments paginate with `limit`/`offset`, not `page`/`page_size`.
(2) Order COD may be NULL. (3) A shipment payload has no `status`,
`merchant_order_ref`, `amount_to_collect_poisha` or
`partial_amount_poisha` fields — it has `tracking_id` +
`tracking_id_normalised`, `outcome_source`, `is_cod_bearing`,
`is_superseded`, `expected_collectible_cod_poisha` (stored) +
`expected_collectible_derived_poisha`, and `needs_review` +
`needs_review_reason` computed at read time. (4) The create-shipment
body takes `partial_amount_evidence_ref` (a text reference), not
`partial_amount_poisha` (`extra="forbid"` → 422); it also takes
`outcome_source`, `is_cod_bearing`, `merchant_courier_account_id`,
and rejects `amount_to_collect_poisha` (422) for returned / cancelled
/ in-transit / unknown outcomes. (5) Statement lines nest `shipment`,
`courier_account` and `import_batch` objects (no top-level
`courier_code` / `shipment_tracking_id`); match statuses include
`human_confirmed`; `parse_status` may be null. (6) The report summary
is nested (`reconciliation_runs` / `reconciliation_results` /
`settlement_batches` / `bank_receipts` / `import_batches`) with
variance keys `collection_c`, `fee_v_r`, `statement_s`,
`conditional_payout_v_cond`, `scenario_v_total`, each
`{total_poisha, non_null_count, null_count}` — always a number, 0
when empty, never null.

**Verified live (2026-11-01, after a container replacement — see
below):** backend uvicorn 8000 (runtime role, PostgreSQL 16.15
reinstalled from cached debs and re-provisioned: alembic head 0002,
`rls.sql`, `triggers.sql`, `roles.sql`, seed couriers; 33 tables, 46
policies, 21 triggers) + Vite dev 5173. Fresh synthetic user
`m8smoke@example.com`: register 201, login 200, workspace 201,
Steadfast account 201. Orders: COD 125000 → 201; COD null → 201
(`order_cod_poisha: null`). Shipments: delivered + 125000 →
expected **125000**, `needs_review: false`; unknown → expected
**NULL**, `needs_review: true`, reason `outcome_not_confirmed`;
partially_delivered + 50000 + evidence ref → expected **50000**;
returned + amount → **422**; second COD-bearing shipment for the same
order → **422**; returned, no amount → expected **0**. List envelope
`{total: 4, limit: 20, offset: 0}`; tracking filter returns exactly
the one row. CSV fixture upload 201 — batch `parsed`, 12 rows / 10
parsed / 6 error rows; import detail shows 10 statement lines (2
duplicate, 8 unmatched). Unauthenticated shipments list → 401. Vite
serves `/shipments`, `/courier-statements`, `/reports` (200 each);
`/api/health` through the proxy returns live PostgreSQL health JSON.
Servers stopped afterwards; PostgreSQL cluster left running.

**NOT verified live (stated plainly, not claimed):** the statements
and reports routers exist as modules (`backend/app/api/statements.py`,
`reports.py` — route paths confirmed by import) but are **not mounted
in `backend/app/main.py`** — only `shipments.router` is wired, with a
comment saying the wiring is temporary and the coordinator owns the
permanent wiring. `GET /statements` and `GET /reports/summary`
currently return 404, so the Courier Statements and Reports pages show
their honest "endpoint may not be deployed yet" error until the
coordinator mounts those routers. The statements list/detail shapes
were verified by code-read against `statements.py` plus the import
detail cross-check above (same rows, same match statuses); the reports
shape was verified by code-read against `reports.py` plus a
tenant-scoped DB count (0 runs, 0 results, 1 import, 0 batches, 0
receipts, 10 statement lines) confirming what the summary will return
once mounted. The frontend must not be changed for this — the API
functions already match the real modules.

**Environment note (2026-11-01):** the container was replaced
mid-task — PostgreSQL vanished entirely (no binaries, no data
directory, no `postgres` user; the first uvicorn died with it). It
was reinstalled from the cached apt archives (`dpkg -i` of
postgresql-16 16.15 + deps: libcommon-sense-perl,
libtypes-serialiser-perl, libjson-xs-perl, libllvm17t64 — the
`libjson-xs-perl`/`postgresql-16` configure failures were missing
cached deps, fixed by installing them in dependency order) and
re-provisioned per `m1/README.md` §3, including the known
`rls.sql`-before-`roles.sql` ordering quirk on `courierledger_app`
(re-ran `rls.sql` after roles existed). Both DB roles and
`backend/.env` were synced to the password in gitignored `backend/.env` (a fresh random value since the Phase 1 rotation, 2026-11-01)
(the `.env` password had drifted from the fresh roles, so the app
reported `database_ok: false` until synced — the `.env` is gitignored,
mode 600, local dev only). The Vite dev server survived the
replacement; the first uvicorn did not and was restarted by PID.

## Milestone 7 — frontend (2026-11-01)

Built against the exact M7 settlement-batch / bank-receipt contract from
the brief (`src/lib/api.ts`, "Settlement batches & bank receipts (M7)"
section). The endpoints landed in parallel and were verified live by the
coordinator on 2026-11-01 (see the M7 verification log in
`m1/IMPLEMENTATION_CHECKLIST.md`): batch → receipt 60000 → allocate →
fully_allocated → variance **−40000** → reversal → variance NULL.

- `src/lib/api.ts`: `listSettlementBatches`, `getSettlementBatch`,
  `createSettlementBatch`, `addBankReceipt`, `completeBankReceipt`,
  `listBankReceipts`, `createAllocation`, `reverseAllocation`,
  `calculateVariance`, `getCurrentVariance` — same `apiFetch` conventions,
  CSRF on the POSTs.
- `src/lib/settlement.ts` (new): Bangla status labels, plain-language
  variance-status reasons (English + Bangla), and taka↔poisha parsing
  with validation (optional expected payout must be non-negative;
  receipt/allocation amounts must be more than ৳0.00).
- `src/pages/SettlementBatches.tsx`: batch list + create-batch form, and
  a detail page (`/settlement-batches/:batchId`, new route) with receipts,
  allocations + net total, variance calculation history, current variance
  with its status reason, and forms/buttons for add receipt, mark receipt
  complete, allocate, reverse an allocation, calculate variance (a 422
  reason is shown verbatim). The current variance is shown ONLY when the
  backend returns a non-null value; when null, the variance_status reason
  is shown and no number appears. Money uses the existing `formatTaka()`
  helper (৳, two decimals).
- `src/pages/BankReceipts.tsx`: workspace receipt list (amount, batch
  link, date, label, complete status with Bangla) plus a standalone
  add-receipt form that requires choosing a batch. No bank account number
  is ever asked for. Receipts stay separate from courier-reported payouts;
  a difference is shown for review, never automatically labelled a loss.
- Tests: `src/settlement.test.tsx` — 8 tests (batch list rendering with
  Bangla labels, create-batch validation incl. negative payout rejected
  with no POST, variance-null reason with no ৳ shown, bank receipts list).

**Not verified live (stated plainly, not claimed):** at the time of
writing there is no settlement route in `backend/app/main.py` and a live
probe of the running backend (health ok) returns 404 for
`/api/workspaces/:id/settlement-batches` and `/api/workspaces/:id/bank-receipts`
(the existing runs endpoint returns 401, i.e. the route exists), so no
live settlement data has been fetched. The pages show an honest "endpoint
may not be deployed yet" error until the backend worker lands them. Once
live, the check to run is: register/login, create a workspace + courier
account, create a batch, add a receipt, mark it complete, allocate it,
calculate the variance, and open the batch at
`/settlement-batches/:batchId` — then update this section.

## Milestone 6 — frontend (2026-10-31)

Built against the exact M6 run-endpoint contract from the brief
(`src/lib/api.ts`, "Reconciliation runs (M6)" section). The frontend is
ready; the backend worker is landing the endpoints in parallel.

- `src/lib/api.ts`: `createRunForImport`, `listRuns`, `getRun`,
  `listRunResults`, `listRunFindings`, `listBatchRuns` — same `apiFetch`
  conventions, CSRF on the POST.
- `src/pages/Reconciliation.tsx`: persisted runs list FIRST (paginated,
  newest first: status, created time, import batch link, result count,
  counts by status with Bangla labels), then rule fixtures, then the
  labelled SYNTHETIC DEMO reconcile below. Empty state: "No reconciliation
  runs yet — create one from an import."
- `src/pages/Runs.tsx` (new, route `/runs/:runId` under the existing auth
  gate): run header (status, created/completed, tolerance, import link,
  engine version, rule snapshot), five-status counts grid, paginated
  results table (C, V_R, S, V_cond, V_total shown separately, never
  summed), expandable per-result detail (fee lines + findings), paginated
  findings list with English + Bangla explanations. Tracking is rendered
  only when the payload provides it; otherwise the shipment id (short
  form) is shown.
- `src/pages/ImportDetail.tsx`: the pending import→run note is replaced
  by a real "Reconciliation runs" section — runs listed with links to
  `/runs/:runId`, or "No run yet for this import" plus a "Create run" form
  (fixture selector: Fixture Alpha/Beta/Gamma, tolerance input default 0,
  per-form idempotency key) wired to the real POST endpoint; a failed
  create shows the backend's own error message, and a successful create
  navigates to the new run.
- Shared: `src/components/RunCounts.tsx` (counts grid + one-line summary,
  reuses `STATUS_BANGLA`), `src/components/RunResultDetail.tsx`
  (per-result breakdown).

**Not verified live (stated plainly, not claimed):** at the time of
writing there is no `runs.py` in `backend/app/api/`, the backend is not
running on port 8000 and PostgreSQL is not running in this container, so
no live run endpoint could be hit. The Vite dev server was started and
served the app shell (200, including the `/runs/:runId` route shell);
`/api/health` through the proxy returned 502 because no backend is
running. The dev server was stopped afterwards. Once the backend worker
lands the endpoints, the live check to run is: register/login, create a
workspace + courier account, upload the generic statement fixture, create
a run from the import, and open it at `/runs/:runId` — then update this
section. `src/pages/Imports.tsx` still carries its M5 "run not yet
available" note (and `imports.test.tsx` asserts it); it should be updated
once the run endpoints are live and verified.

## Milestone 5 — live smoke (2026-10-31)

Backend (uvicorn 127.0.0.1:8000, runtime role, `courierledger_app`) + Vite dev
(5173). Fresh synthetic user `m5smoke@example.com` via the real auth flow:
register 201, login 200, `/me` 200; unauthenticated `/me` → 401. Workspace
"M5 Smoke Shop" created (201), Steadfast courier account created (201),
`generic_statement_fixture.csv` uploaded through the Vite proxy (201): batch
`659f7bb7…`, status `parsed`, 12 rows / 10 parsed / 6 error rows, mapping
snapshot stored. List shows the batch; detail shows 12 rows, `truncated:
false`, 9 errors, including row 8 (blank collected, invalid payout `"1,5x0"`).
Demo reconcile through the proxy returns all five statuses exactly as the
UI expects: SF-DEMO-A matched, SF-DEMO-B open (C −5000), SF-DEMO-C
needs_review, SF-DEMO-D mismatch (V_cond −2000), SF-DEMO-E
matched_with_tolerance (tolerance 500). `GET /api/rules` returns the three
synthetic fixtures. The Vite dev server serves the new page modules
(`Imports.tsx`, `ImportDetail.tsx` transform cleanly); the Vitest suite
renders the Imports list and the auth-gate redirect against the same API
shapes. Servers stopped afterwards; PostgreSQL cluster left running.

## Run it

```bash
npm install
npm run dev      # http://localhost:5173 (or next free port)
npm run build    # production build into dist/
npm test         # Vitest smoke tests
```

Copy `.env.example` to `.env` if needed. Leave `VITE_API_URL` empty to use the
Vite dev proxy (`/api` → `http://127.0.0.1:8000`); set it only to point at a
backend on another origin.

## What is working vs placeholder

| Area | State |
| --- | --- |
| Dashboard | **Working** — live `GET /api/health` check (refetches every 30 s); real counts from `GET /api/workspaces` and `GET /api/workspaces/:id/imports` for the selected workspace; status counts and recent-shipments table from the labelled synthetic demo reconcile run |
| Login / Register | **Working forms** — React Hook Form + Zod validation; submit calls `POST /api/auth/login` / `/api/auth/register` and shows the backend error (no fake success). All routes except login/register are guarded: `GET /api/auth/me` → 401 sends the user to `/login` |
| Workspaces | **Working** — lists via `GET /api/workspaces` (real names, roles, status); create form posts to `POST /api/workspaces`. On backend error the page says so — it no longer falls back to invented demo workspaces |
| Imports | **Working (M5 centrepiece)** — batch list from `GET /api/workspaces/:id/imports` with a workspace picker; detail page (`/imports/:batchId`) from the detail endpoint: rows table with parse/match status, raw values verbatim, reported money with per-field DQ state, errors list, mapping snapshot, truncated flag. CSV upload form (multipart + mapping JSON + courier account) posts to `POST /api/workspaces/:id/imports` with the CSRF flow; courier accounts are listed/created via `GET|POST /api/workspaces/:id/courier-accounts` (5 seeded codes only, no rates). **M6:** the detail page now shows the real "Reconciliation runs" section (list + create-run form wired to `POST /api/workspaces/:id/imports/:batchId/runs`); the batch *list* page still carries the M5 "run not yet available" note until the endpoints are verified live |
| Shipments | **Working (M8, verified live 2026-11-01)** — real shipments from `GET /api/workspaces/:id/shipments` (`limit`/`offset`, tracking filter) with expected collectible shown as NULL (unknown, never ৳0.00) or verified ৳0.00, needs-review badge + reason, COD-bearing badge; create-order form (COD optional, blank stays NULL) and add-a-shipment form (outcome, outcome source, COD-bearing, amount only where it applies, partial-amount evidence *reference* — never `partial_amount_poisha`, which the backend rejects with 422). Live smoke: delivered → expected 125000, unknown → NULL + `outcome_not_confirmed`, partial + evidence → 50000, returned + amount → 422, second COD-bearing → 422, returned → 0. The labelled synthetic demo reconcile run stays on the Reconciliation page — nothing demo is mixed into this list. |
| Reconciliation | **Working (M6, verified live 2026-10-31)** — persisted runs list FIRST from `GET /api/workspaces/:id/runs` (paginated, newest first, counts by status with Bangla labels, links to `/runs/:runId`); run detail page shows the run header, counts, paginated results with the five variances separate, fee lines and findings. Rule fixtures from `GET /api/rules` and the five synthetic demo shipments reconciled live by `POST /api/reconcile` remain below, labelled SYNTHETIC DEMO. |
| Settlement Batches | **Working (M7, verified live 2026-11-01)** — batch list from `GET /api/workspaces/:id/settlement-batches` (period, courier account, expected payout, allocated total, allocation + bank status with Bangla labels, current variance only when non-null); create-batch form (courier account, optional run, period dates, optional expected payout in taka → poisha, non-negative validation); detail page (`/settlement-batches/:batchId`) with receipts, allocations + net total, variance calculation history, current variance with its status reason, and forms/buttons for add receipt, mark receipt complete, allocate, reverse an allocation, calculate variance (422 reason shown verbatim). Live smoke: batch → receipt 60000 → allocate → fully_allocated → variance **−40000** → reversal → variance NULL. |
| Bank Receipts | **Working (M7, verified live 2026-11-01)** — receipt list from `GET /api/workspaces/:id/bank-receipts` (amount, batch link, date, label, complete status with Bangla), plus a standalone add-receipt form that requires choosing a batch. Receipts are kept separate from courier-reported payouts and a difference is never automatically labelled a loss. No bank account number is ever asked for. |
| Courier Statements | **Working (M8, code-verified; live pending router wiring)** — real statement lines from `GET /api/workspaces/:id/statements` (paginated, filter by import batch + match status incl. `human_confirmed`) with match badges + Bangla labels and DQ states; detail view from `GET /statements/:lineId` with raw values verbatim, parse/match status, linked shipment, courier account and import batch. The router module exists but is **not mounted in `backend/app/main.py`** yet, so live calls currently 404 and the page shows its honest "endpoint may not be deployed yet" error. Statement rows were verified live through the import detail endpoint (10 lines: 2 duplicate, 8 unmatched). |
| Reports | **Working (M8, code-verified; live pending router wiring)** — real aggregates from `GET /api/workspaces/:id/reports/summary`: runs by status, results by the five overall statuses, import batches by status, settlement batches by allocation + bank status, bank receipts complete/incomplete, and the five variance totals each separate with known/unknown counts, never summed. The router module exists but is **not mounted in `backend/app/main.py`** yet, so live calls currently 404 and the page shows its honest "endpoint may not be deployed yet" error. Expected values were verified by code-read plus a tenant-scoped DB count (0 runs, 0 results, 1 import, 0 batches, 0 receipts). |
| Team and Workspace | **Working (partial)** — real workspaces list with the caller's role per workspace; member list/invites state plainly that no backend endpoint exists yet |
| Settings | **Working (display only)** — tolerance shows the documented default ৳0.00 (0 poisha), labelled as the documented default, not a fetched value; no settings endpoint exists yet, so nothing is saved |
| Layout | **Working** — sidebar nav (all 10 sections), header shows the real selected workspace name (never a fake merchant name), signed-in email + working sign-out, SYNTHETIC DEMO badge kept |
| UI components | **Working** — hand-written shadcn-style `components/ui/`: Button, Card, Input, Label, Badge (+ `StatusBadge` with Bangla) |

## Backend integration points

- `src/lib/api.ts` — fetch wrapper: `credentials: 'include'` (HttpOnly-cookie
  session), `X-CSRF-Token` header from `GET /api/auth/csrf`
  (`{"csrf_token": "..."}` + `cl_csrf` cookie), plus a multipart variant for
  the CSV upload. Verified against `backend/app/api/` 2026-10-31 (M5):
  - `GET /api/health` → `{"status":"ok","service":...,"database":"postgresql","database_ok":true}`
  - `GET /api/auth/me`; `POST /api/auth/login|register|logout` (CSRF)
  - `GET /api/workspaces` → `WorkspaceOut[] {id,name,trading_name,status,role,created_at}`; `POST /api/workspaces` accepts `{name}`
  - `GET|POST /api/workspaces/:id/courier-accounts`
  - `GET /api/workspaces/:id/imports` (list) and `GET .../imports/:batchId`
    (detail: batch + rows + truncated + errors)
  - `POST /api/workspaces/:id/imports` (multipart: file, mapping JSON,
    merchant_courier_account_id)
  - `POST /api/reconcile {shipments, tolerance_poisha}` → `{results, count}`
    (computes only, persists nothing); `GET /api/rules` → synthetic fixtures
  - `POST /api/workspaces/:id/imports/:batchId/runs` → run created (201;
    200 on idempotency replay); `GET /api/workspaces/:id/runs?limit&offset`
    → `{runs, total, limit, offset}`; `GET .../runs/:runId` → `{run, results,
    total}`; `GET .../runs/:runId/results|findings?limit&offset` →
    paginated `{results|findings, total, limit, offset}`;
    `GET /api/workspaces/:id/imports/:batchId/runs` → `{runs, total}` —
    **M6 contract only: these endpoints do not exist in the backend yet
    (no `runs.py` in `backend/app/api/` as of 2026-10-31), so they are
    unverified live**
  - `GET /api/workspaces/:id/settlement-batches` → `SettlementBatch[]`;
    `GET .../settlement-batches/:batchId` → `{batch, receipts, allocations,
    net_allocated_poisha, variance_calculations, current_bank_variance_poisha,
    variance_status}`; `POST .../settlement-batches` → 201 batch;
    `POST .../settlement-batches/:batchId/receipts` → 201 receipt;
    `POST .../receipts/:receiptId/complete` → 200 receipt;
    `GET /api/workspaces/:id/bank-receipts` → `BankReceipt[]`;
    `POST .../settlement-batches/:batchId/allocations` → 201;
    `POST .../allocations/:allocationId/reverse` → 201 reversal;
    `POST .../settlement-batches/:batchId/calculate-variance` →
    `{calculation_seq, expected_batch_payout_poisha,
    allocated_bank_confirmed_total_poisha, bank_variance_poisha}` (422 with a
    reason shown verbatim);
    `GET .../settlement-batches/:batchId/current-variance` →
    `{bank_variance_poisha, calculation_seq, variance_status}` —
    **verified live 2026-11-01** (coordinator smoke: variance −40000,
    then NULL after reversal; full backend suite 228 passed).
  - `GET /api/workspaces/:id/orders?limit&offset` → `{orders, total, limit,
    offset}`; `POST .../orders` → 201 order (`{merchant_order_ref,
    order_cod_poisha}` — COD may be null); `POST
    .../orders/:orderId/shipments` → 201 shipment (`{tracking_id,
    outcome, outcome_source, is_cod_bearing, amount_to_collect_poisha,
    partial_amount_evidence_ref, merchant_courier_account_id,
    shipped_on}` — no `partial_amount_poisha`: `extra="forbid"` → 422;
    amount rejected for returned/cancelled/in_transit/unknown);
    `GET /api/workspaces/:id/shipments?limit&offset&order_id&tracking_id`
    → `{shipments, total, limit, offset}` — **verified live 2026-11-01**
    (see the M8 section above: delivered → expected 125000, unknown →
    NULL + `outcome_not_confirmed`, partial + evidence → 50000,
    returned + amount → 422, second COD-bearing → 422, returned → 0).
  - `GET /api/workspaces/:id/statements?page&page_size&import_batch_id&match_status`
    → `{statements, total, page, page_size}` (nested `shipment` /
    `courier_account` / `import_batch` objects);
    `GET .../statements/:lineId` → one line in full — **code-verified
    2026-11-01 against `backend/app/api/statements.py`; NOT live** (the
    router module exists but is not mounted in `backend/app/main.py`,
    so live calls currently 404).
  - `GET /api/workspaces/:id/reports/summary` → nested
    `{reconciliation_runs: {total, by_status}, reconciliation_results:
    {total, by_overall_status, variances_poisha: {collection_c, fee_v_r,
    statement_s, conditional_payout_v_cond, scenario_v_total} each
    {total_poisha, non_null_count, null_count}}, settlement_batches:
    {total, by_allocation_status, by_bank_status}, bank_receipts:
    {total, complete, incomplete}, import_batches: {total, by_status}}`
    — **code-verified 2026-11-01 against `backend/app/api/reports.py`;
    NOT live** (router not mounted in `backend/app/main.py` yet).
- **No backend endpoints exist yet** for team members — the frontend
  does not invent them (see the pending pages).
- All money is integer poisha in the API layer; `formatTaka()` in
  `src/lib/demo.ts` only formats for display.

## File map

```
frontend/
├── .env.example            # VITE_API_URL placeholder (empty = dev proxy)
├── index.html              # title: CourierLedger — COD Courier Reconciliation
├── tailwind.config.js      # content globs, primary indigo #4F46E5
├── postcss.config.js
├── vite.config.ts          # /api proxy -> http://127.0.0.1:8000
├── package.json            # scripts: dev / build / lint / preview / test
└── src/
    ├── main.tsx
    ├── App.tsx             # QueryClientProvider + Router + all routes
    ├── index.css           # Tailwind directives
    ├── lib/
    │   ├── api.ts          # fetch wrapper, CSRF, multipart upload, verified API contracts
    │   ├── demo.ts         # SYNTHETIC demo data, Bangla status labels, formatTaka, default mapping
    │   ├── settlement.ts   # M7: settlement/bank Bangla labels, variance-status reasons, taka↔poisha helpers
    │   ├── shipments.ts    # M8: outcome/source Bangla labels, V2.1 collectible rule, needs-review reasons
    │   ├── reconcileDemo.ts # useDemoReconcile(): live engine run over the 5 demo shipments
    │   └── workspace.ts    # useSelectedWorkspace(): localStorage-backed workspace picker
    ├── components/
    │   ├── Layout.tsx      # sidebar + header (real workspace name, sign-out)
    │   ├── PlaceholderPage.tsx
    │   ├── ReconcileDetail.tsx  # full per-shipment breakdown + findings (demo)
    │   ├── RunCounts.tsx   # M6: five-status counts grid + summary (Bangla)
    │   ├── RunResultDetail.tsx # M6: persisted-run per-result breakdown, fee lines, findings
    │   ├── WorkspacePicker.tsx
    │   └── ui/             # Button, Card, Input, Label, Badge
    ├── pages/
    │   ├── Dashboard.tsx   # live health + real workspace/import counts + demo reconcile
    │   ├── Login.tsx
    │   ├── Register.tsx
    │   ├── Workspaces.tsx  # list/create (real data, honest error state)
    │   ├── Shipments.tsx   # M8: real shipments list + create order/shipment forms (limit/offset, NULL collectible explicit)
    │   ├── Imports.tsx     # batch list + CSV upload + courier accounts
    │   ├── ImportDetail.tsx # rows, DQ states, raw values, errors, mapping snapshot + M6 runs section + create-run form
    │   ├── Reconciliation.tsx # M6: persisted runs list first + rules + demo run + shipment detail
    │   ├── Runs.tsx        # M6: run detail page (/runs/:runId)
    │   ├── CourierStatements.tsx # M8: real statement lines + detail (nested shipment/account/batch); router not yet mounted live
    │   ├── Reports.tsx     # M8: real aggregates, five variance totals separate, never summed; router not yet mounted live
    │   │               # (both show an honest "endpoint may not be deployed yet" error until wired)
    │   ├── SettlementBatches.tsx # M7: batch list + create form + detail (/settlement-batches/:batchId) with receipts, allocations, variance history, current variance
    │   ├── BankReceipts.tsx # M7: workspace receipt list + standalone add-receipt form (batch required)
    │   ├── Team.tsx        # real workspaces + roles; members pending
    │   └── Settings.tsx    # documented default tolerance ৳0.00, not saved
    ├── smoke.test.tsx      # 2 Vitest tests (demo helpers; the old Shipments demo-table test was removed in M8 — the demo table no longer lives on that page)
    ├── imports.test.tsx    # 2 Vitest tests (Imports list, mocked API shape)
    ├── runs.test.tsx       # M6: 6 Vitest tests (runs list, run detail, import→run)
    ├── settlement.test.tsx # M7: 8 Vitest tests (batch list + Bangla labels, create-batch validation, variance-null reason, bank receipts list)
    ├── milestone8.test.tsx # M8: 13 Vitest tests (shipments list + NULL collectible, filters, create order/shipment POST bodies, statements list + filter + detail, reports aggregates + empty state, auth gate for the three pages)
    └── auth.test.tsx       # 1 Vitest test (unauthenticated redirect to login)
```
