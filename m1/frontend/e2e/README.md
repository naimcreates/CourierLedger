# CourierLedger M1 — Playwright end-to-end tests

**Status: RUN — all green.** Playwright 1.64.1 +
Chromium headless shell installed 2026-11-01 (see below). The happy-path
suite runs against the real backend + real PostgreSQL — no API mocks,
synthetic fixture data only. **3 passed, 0 failed** (coordinator
re-run, 2026-11-01, 26.2s). M9-BUG-0–3 below are **fixed** (integration
pass, 2026-11-01); the run-detail test is restored from `test.fixme`
to a normal test.

## What is covered

`happy-path.spec.ts` (3 tests, 1 browser — Chromium; all 3 run):

1. **Full happy path** — register a fresh user → log in (register does not
   set a session, so login is a separate step) → create a workspace →
   create order ORD-1001 + shipment TRK-1001 (delivered, COD-bearing,
   ৳1,000.00 to collect) → create a Steadfast courier account → upload
   `backend/tests/data/generic_statement_fixture.csv` (asserts 12 rows /
   10 parsed / 6 error rows) → create a Fixture Alpha run from the import
   (asserts run `completed`, 1 result for TRK-1001, `needs_review`) →
   create a settlement batch linked to the run with expected payout
   ৳1,000.00 → add a ৳600.00 bank receipt → mark it complete → allocate
   it in full (asserts `fully_allocated` + `fully_reconciled`) → calculate
   the bank variance (asserts ৳-400.00 — variance is allocated minus
   expected: 60000 − 100000 = −40000 poisha) → reverse the allocation
   (asserts the batch returns to `unallocated` /
   `receipt_complete_unallocated` and the current variance becomes NULL,
   shown as the backend's `not_fully_allocated` reason instead of the
   stale −৳400.00; the variance history still shows calculation #1).

   **Which steps are UI and which are same-session API:**
   everything up to run creation is pure UI, and since Milestone 10
   step 1 every settle mutation is UI too — the batch-detail forms for
   receipt, complete, allocate, calculate-variance and reverse. Run
   completion is asserted on the import detail's "Reconciliation runs"
   list (UI — the run shows with status `completed`) plus a
   same-session `page.request` READ of the run detail + results (real
   backend, no mocks) to cross-check the exact stored poisha values.
   No step in this suite uses `page.request` for a mutation.
2. **Run detail page shows the completed run** — restored from
   `test.fixme` to a normal test once M9-BUG-1 was fixed (integration
   pass, 2026-11-01). It runs the full flow and asserts `completed` /
   TRK-1001 / `needs_review` on `/runs/:runId`.
3. **Unauthenticated visitor is sent to login** — `/` redirects to
   `/login` without a session.

## App bugs found by this suite — all fixed (integration pass, 2026-11-01)

All four were frontend/backend contract drift — the M6/M7 frontend was
built against a contract the backend does not implement, and was only
ever verified with Vitest mocks (which used the assumed shape) plus
API-level checks, never a live browser render. Each is fixed in the
frontend to match the real backend shape (verified against the backend
endpoint code and proven by the now-green suite); no backend shape was
changed to fit the frontend.

- **M9-BUG-0 (minor, FIXED):** backend `RegisterRequest.password` requires
  min 12 characters, but the frontend register form only asked for 8 —
  an 8–11 character password submitted and failed with a bare
  "Request failed (422)". Frontend now requires 12 and surfaces the
  backend's 422 detail message.
- **M9-BUG-1 (run detail crashes, FIXED):** `GET /runs/{id}` returns the flat
  run dict (`status` + `result_counts` at top level, `results`
  embedded), but the frontend `RunDetailResponse` expected
  `{ run, results, total }` and `Runs.tsx` read `run.data.run.*` —
  the page threw `TypeError: Cannot read properties of undefined
  (reading 'run_id')` at `RunDetailPage` (proven by the Playwright
  trace `pageError`) and rendered nothing. Frontend now reads the flat
  shape; tracking ids are resolved per result via `GET /shipments/{id}`.
- **M9-BUG-2 (settlement detail renders no forms, FIXED):**
  `GET /settlement-batches/{id}` returns the flat batch dict (no
  `batch` wrapper; `net_allocated_total_poisha`, not
  `net_allocated_poisha`), but the frontend `SettlementBatchDetail`
  expected `{ batch, receipts, ... }` — the page rendered only its
  header, so the receipt / allocate / calculate-variance forms never
  appeared. Frontend now reads the flat shape. The settle mutations
  were converted from same-session API calls to the batch-detail UI
  forms in Milestone 10 step 1 (2026-11-01); the reversal step was
  added then too. No app bug was found by the conversion: the two
  failures during it were test bugs — a strict-mode violation (the
  receipt label also appears in the allocation select's option text)
  and a wrong expected null-variance reason (after a full reversal
  the backend's documented reason order reports
  `not_fully_allocated`, not `input_set_changed`, because the batch
  is no longer fully allocated).
- **M9-BUG-3 (settlement list + run counts, FIXED):**
  `GET /settlement-batches` returns the paginated envelope
  `{ settlement_batches, total, ... }` but the frontend expected a bare
  array, so the list rendered nothing; and every run payload carries
  `result_counts`, not the `counts_by_status` the frontend
  `RunSummary` expected, so run counts rendered as 0 / '—' wherever
  they are shown. Frontend now reads the envelope and `result_counts`.
  The same drift class was found and fixed for `GET /bank-receipts`
  (envelope `{receipts, total, ...}`).

Each run registers a fresh user with a timestamped email
(`e2e-<millis>@example.com`), so re-runs never collide. The run creates
real rows in `courierledger_app` — that is the local dev database, by
design.

## Run it

```bash
cd ~/workspace/goals/courierledger-planning-and-market-validation/m1/frontend
npx playwright test            # starts backend + Vite itself (see below)
npx playwright test --list     # dry run: list tests without a browser
npx playwright test --headed   # watch it in a headed browser
```

## How the servers start

`playwright.config.ts` `webServer` starts both servers before the tests
and stops them afterwards:

- **Backend:** `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`
  (cwd `m1/backend`), with the environment read from `m1/backend/.env`
  (DATABASE_URL against `courierledger_app` as the runtime role,
  SECRET_KEY, SESSION_SECRET). Waits on `/api/health`.
- **Frontend:** `npm run dev -- --port 5173 --strictPort` (proxies `/api`
  to the backend). Waits on `http://localhost:5173`.

`reuseExistingServer: true` — if the servers are already running, the
suite reuses them. **Warning (known trap, M7):** a stale uvicorn started
before a router was added serves 404s for the new routes while a fresh
server fails to bind. If settlement/run steps fail with 404s, kill any
old server on port 8000 first (PID-based kill, not `pkill -f uvicorn`,
which once matched the caller's own shell) and re-run.

Manual start (if you prefer to run the servers yourself first):

```bash
# Terminal 1 — backend (from m1/backend; .env is sourced by the shell):
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# Terminal 2 — frontend (from m1/frontend):
npm run dev -- --port 5173 --strictPort
```

PostgreSQL 16 must be online on port 5432 with the `courierledger_app`
database provisioned (see `m1/README.md` §3). If the container was
replaced and PostgreSQL is gone, re-provision per that section first.

## Installation (2026-11-01, exact output)

```bash
cd ~/workspace/goals/courierledger-planning-and-market-validation/m1/frontend
npm install -D @playwright/test   # added 3 packages in 4s
npx playwright --version           # Version 1.64.1
npx playwright install chromium   # Chromium headless shell 156.0.8078.4
                                  # + FFmpeg, downloaded to ~/.cache/ms-playwright/
```

No `--with-deps` was needed: the headless shell runs without extra
system packages in this VM. If a future run fails with missing shared
libraries, run `npx playwright install --with-deps chromium` (may need
sudo) or `npx playwright install-deps chromium`.

## Results

- 2026-11-01, run 1: **2 passed, 1 skipped (fixme), 0 failed** (48.4s) —
  full happy path (27.2s) + unauthenticated redirect (3.3s), Chromium,
  real backend + real PostgreSQL. DB cross-check after the run: latest
  run `completed`, 1 `needs_review` result; settlement batch
  `fully_allocated` / `fully_reconciled`, expected 100000, allocated
  60000; variance calculation #1 = −40000 poisha.
- 2026-11-01, run 2 (repeatability): **2 passed, 1 skipped, 0 failed**
  (23.8s) — same result with a fresh timestamped user.
- 2026-11-01, coordinator re-run after the M9-BUG-0–3 fixes:
  **3 passed, 0 failed** (26.2s) — full happy path (12.3s), restored
  run-detail test (7.0s), unauthenticated redirect (1.2s).
- 2026-11-01, M10 step 1 (settle mutations converted to UI forms +
  reversal step added): **3 passed, 0 failed** (31.1s), repeated
  **3 passed, 0 failed** (42.3s) — full happy path (13.0s / 17.7s),
  run-detail test (9.9s / 15.2s), unauthenticated redirect (1.8s /
  2.9s). Baseline before the change (same day): **3 passed**
  (39.7s). No step uses `page.request` for a mutation any more.

Playwright writes run artifacts to `frontend/test-results/` (traces on
failure); that directory is now in `frontend/.gitignore`.
