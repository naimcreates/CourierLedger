import { expect, test, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'

/**
 * CourierLedger M1 — register → workspace → import → run → settle happy path.
 *
 * Against the REAL backend + REAL PostgreSQL (started by the webServer
 * entries in playwright.config.ts). No API mocks. Synthetic fixture data
 * only: backend/tests/data/generic_statement_fixture.csv, order ORD-1001 /
 * shipment TRK-1001, and a ৳600.00 bank receipt against a ৳1,000.00
 * expected payout.
 *
 * Each run registers a fresh user with a timestamped email, so re-runs
 * never collide. The run creates real rows in courierledger_app; that is
 * by design — the app database is the local dev database, not a test
 * database.
 *
 * Known app behaviour the assertions rely on (verified live, M5–M8):
 * - POST /api/auth/register does NOT set a session cookie; the UI must
 *   log in afterwards (RegisterPage navigates to /workspaces, which
 *   redirects to /login until a session exists).
 * - The backend requires passwords of 12+ characters; the frontend
 *   register form only asks for 8 (see M9-BUG-0 in the report) — the
 *   suite uses a 12-character password.
 * - The generic fixture uploads as 12 rows / 10 parsed / 6 error rows.
 * - A Fixture Alpha run over the fixture with the API-created TRK-1001
 *   shipment completes with exactly 1 result, needs_review (Fixture
 *   Alpha has no rule delivery fee, so the expected fee is NULL).
 * - Bank variance = bank-confirmed allocated − expected payout, so
 *   ৳600.00 allocated against ৳1,000.00 expected is ৳-400.00.
 * - Reversing the only allocation makes the current variance NULL:
 *   the batch is no longer fully allocated, so the backend's
 *   documented null-reason order (no_calculation →
 *   not_fully_allocated → receipt_incomplete → expected_changed →
 *   input_set_changed) reports not_fully_allocated and the page shows
 *   that reason instead of a stale number; the batch returns to
 *   unallocated / receipt_complete_unallocated.
 *
 * APP BUGS FOUND BY THIS SUITE (all fixed, integration pass 2026-11-01):
 * - M9-BUG-0 (register password length), M9-BUG-1 (run detail flat
 *   payload), M9-BUG-2 (settlement detail flat payload), M9-BUG-3
 *   (settlement list envelope + run result_counts) — see e2e/README.md.
 * The settle mutations (receipt → complete → allocate → calculate →
 * reverse) are driven through the batch-detail UI forms since Milestone
 * 10 step 1; nothing in this suite uses page.request for a mutation.
 * The run-completion step below still cross-checks exact stored values
 * with same-session API READS (the run detail page itself is covered by
 * the second test); reads are not mutations and need no UI workaround.
 */

const FIXTURE_CSV = fileURLToPath(
  new URL(
    '../../backend/tests/data/generic_statement_fixture.csv',
    import.meta.url,
  ),
)

interface HappyPathIds {
  email: string
  workspaceName: string
  workspaceId: string
  batchId: string
  runId: string
}

/**
 * Drives the UI from register through run creation. Returns the ids the
 * later steps need. Ends on the run detail page; callers navigate away
 * from there.
 */
async function setupToRun(page: Page): Promise<HappyPathIds> {
  const suffix = Date.now().toString()
  const email = `e2e-${suffix}@example.com`
  // Backend requires min 12 chars (the frontend form only asks for 8).
  const password = 'E2ePass1234!'
  const workspaceName = `E2E Shop ${suffix}`

  await test.step('register a fresh user', async () => {
    await page.goto('/register')
    await page.getByLabel('Full name').fill('E2E Shop Owner')
    await page.getByLabel('Email').fill(email)
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByLabel('Confirm password').fill(password)
    await page.getByRole('button', { name: 'Create account' }).click()
    // Register does not set a session: /workspaces redirects to /login.
    await expect(page).toHaveURL(/\/login/)
  })

  await test.step('log in', async () => {
    await page.getByLabel('Email').fill(email)
    await page.getByLabel('Password', { exact: true }).fill(password)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page).toHaveURL('http://localhost:5173/')
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
  })

  let workspaceId = ''
  await test.step('create a workspace', async () => {
    await page.goto('/workspaces')
    await page.getByLabel('Workspace name').fill(workspaceName)
    await page.getByRole('button', { name: 'Create workspace' }).click()
    // The name appears in the layout banner (selected workspace) and in
    // the list; assert the exact list item.
    await expect(page.getByText(workspaceName, { exact: true })).toBeVisible()
    const res = await page.request.get('/api/workspaces')
    expect(res.ok()).toBeTruthy()
    const workspaces = (await res.json()) as { id: string }[]
    expect(workspaces).toHaveLength(1)
    workspaceId = workspaces[0].id
  })

  await test.step('create order ORD-1001 and shipment TRK-1001', async () => {
    await page.goto('/shipments')
    await page.getByLabel('Merchant order ref').fill('ORD-1001')
    await page.getByLabel('Order COD ৳ (optional)').fill('1000')
    await page.getByRole('button', { name: 'Create order' }).click()
    await expect(page.getByText('Order ORD-1001 created.')).toBeVisible()

    // The shipment form defaults are delivered / merchant_confirmed /
    // COD-bearing — exactly what the happy path needs.
    await expect(page.locator('#shipment-cod-bearing')).toBeChecked()
    // One order exists in this fresh workspace: index 1 is ORD-1001
    // (index 0 is "Choose an order…").
    await page.locator('#shipment-order').selectOption({ index: 1 })
    await page.locator('#shipment-tracking').fill('TRK-1001')
    await page.getByLabel('Amount to collect ৳ (optional)').fill('1000')
    await page.getByRole('button', { name: 'Add shipment' }).click()
    await expect(page.getByText('Shipment TRK-1001 created.')).toBeVisible()
  })

  await test.step('create a Steadfast courier account', async () => {
    await page.goto('/imports')
    await expect(page.locator('#courier-code')).toHaveValue('steadfast')
    await page.getByRole('button', { name: 'Create account' }).click()
    await expect(page.getByText('Courier account created')).toBeVisible()
  })

  let batchId = ''
  await test.step('upload the generic statement fixture', async () => {
    await page.setInputFiles('#csv-file', FIXTURE_CSV)
    // One courier account exists: index 1 is the Steadfast account.
    await page.locator('#courier-account').selectOption({ index: 1 })
    // The mapping textarea already holds the default mapping for this
    // fixture (canonical field -> CSV header); leave it unchanged.
    await page.getByRole('button', { name: 'Upload statement' }).click()
    await expect(page).toHaveURL(/\/imports\/[0-9a-f-]+$/)
    await expect(page.locator('p:text-is("Rows") + p')).toHaveText('12')
    await expect(page.locator('p:text-is("Parsed rows") + p')).toHaveText('10')
    await expect(page.locator('p:text-is("Error rows") + p')).toHaveText('6')
    await expect(page.getByText('TRK-1001').first()).toBeVisible()
    batchId = page.url().split('/imports/')[1]
  })

  let runId = ''
  await test.step('create a run from the import', async () => {
    await expect(page.getByText('No run yet for this import.')).toBeVisible()
    await expect(page.locator('#run-fixture')).toHaveValue('Fixture Alpha')
    await expect(page.locator('#run-tolerance')).toHaveValue('0')
    await page.getByRole('button', { name: 'Create run' }).click()
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+$/)
    runId = page.url().split('/runs/')[1]
  })

  return { email, workspaceName, workspaceId, batchId, runId }
}

test.describe('happy path: register → workspace → import → run → settle', () => {
  test('full happy path', async ({ page }) => {
    const ids = await setupToRun(page)

    await test.step('run is completed (UI list + same-session API read)', async () => {
      // The run detail page itself is covered by the second test; here
      // the import detail's "Reconciliation runs" list shows the run as
      // completed in the UI, and a same-session API READ cross-checks
      // the exact stored values (reads only — no mutation via API).
      await page.goto(`/imports/${ids.batchId}`)
      await expect(page.locator(`a[href="/runs/${ids.runId}"]`)).toBeVisible()
      await expect(page.getByText('completed')).toBeVisible()

      const detail = await page.request.get(
        `/api/workspaces/${ids.workspaceId}/runs/${ids.runId}`,
      )
      expect(detail.ok()).toBeTruthy()
      const run = (await detail.json()) as {
        status: string
        result_counts: Record<string, number>
      }
      expect(run.status).toBe('completed')
      expect(run.result_counts.needs_review).toBe(1)

      const results = await page.request.get(
        `/api/workspaces/${ids.workspaceId}/runs/${ids.runId}/results`,
      )
      expect(results.ok()).toBeTruthy()
      const resultsJson = (await results.json()) as {
        total: number
        results: {
          overall_status: string
          expected_collectible_poisha: number
          reported_collected_poisha: number
        }[]
      }
      expect(resultsJson.total).toBe(1)
      expect(resultsJson.results[0].overall_status).toBe('needs_review')
      expect(resultsJson.results[0].expected_collectible_poisha).toBe(100000)
      expect(resultsJson.results[0].reported_collected_poisha).toBe(100000)
    })

    await test.step('create a settlement batch linked to the run', async () => {
      await page.goto('/settlement-batches')
      // One courier account and one run exist: index 1 in each select.
      await page.locator('#batch-courier-account').selectOption({ index: 1 })
      await page.locator('#batch-run').selectOption({ index: 1 })
      await page.getByLabel('Expected payout ৳ (optional)').fill('1000')
      await page.getByRole('button', { name: 'Create batch' }).click()
      await expect(page).toHaveURL(/\/settlement-batches\/[0-9a-f-]+$/)
      // exact: true — without it the name matches two headings
      // ("Settlement Batches" and "Create a settlement batch") and
      // the assertion fails with a strict-mode violation (observed
      // 2026-11-01; the batch detail page itself had loaded fine).
      await expect(
        page.getByRole('heading', { name: 'Settlement batch', exact: true }),
      ).toBeVisible()
    })

    await test.step('settle via the batch-detail UI forms', async () => {
      // Already on the batch detail page after batch creation. Every
      // mutation below goes through the page's own forms — no API calls.
      // The receipt form and the allocation form both label their
      // amount field "Amount ৳", so both are driven by id, not label.

      // 1. Record a ৳600.00 bank receipt (60000 poisha).
      await page.locator('#receipt-amount').fill('600')
      await page.getByLabel('Label (optional)').fill('E2E bank transfer')
      await page.getByRole('button', { name: 'Add receipt' }).click()
      await expect(page.getByText('Bank receipts (1)')).toBeVisible()
      // The label appears twice once the receipt exists — the table
      // cell and the allocation select's option — so assert on the cell.
      await expect(
        page.getByRole('cell', { name: 'E2E bank transfer' }),
      ).toBeVisible()
      await expect(page.getByText('incomplete · অসম্পূর্ণ')).toBeVisible()

      // 2. Mark the receipt complete.
      await page.getByRole('button', { name: 'Mark complete' }).click()
      await expect(page.getByText('complete · সম্পূর্ণ')).toBeVisible()
      await expect(
        page.getByRole('button', { name: 'Mark complete' }),
      ).toHaveCount(0)

      // 3. Allocate the receipt in full (one receipt exists: index 1).
      await page.locator('#allocation-receipt').selectOption({ index: 1 })
      await page.locator('#allocation-amount').fill('600')
      await page.getByRole('button', { name: 'Allocate' }).click()
      await expect(page.getByText('Allocations (1)')).toBeVisible()
      await expect(
        page.getByText('fully_allocated · সম্পূর্ণ বরাদ্দ'),
      ).toBeVisible()
      await expect(
        page.getByText('fully_reconciled · সম্পূর্ণ মিলেছে'),
      ).toBeVisible()

      // 4. Calculate the bank variance: allocated − expected =
      // 60000 − 100000 = −40000 poisha (৳-400.00).
      await page.getByRole('button', { name: 'Calculate variance' }).click()
      await expect(
        page.getByText(
          'Calculation #1: expected ৳1,000.00, bank-confirmed allocated ৳600.00, variance ৳-400.00.',
        ),
      ).toBeVisible()
      await expect(page.getByTestId('current-variance')).toContainText('৳-400.00')
      await expect(
        page.getByText('Variance calculation history (1)'),
      ).toBeVisible()

      // 5. Reverse the allocation. The reversal is a new row — the
      // original allocation is kept — so the batch returns to
      // unallocated / receipt_complete_unallocated, and the current
      // variance becomes NULL: the batch is no longer fully allocated,
      // so the backend's documented null-reason order reports
      // not_fully_allocated and the page shows that reason instead of
      // the stale −৳400.00. (The history table still shows
      // calculation #1 — it is never rewritten — so the "no stale
      // number" check is scoped to the current-variance region.)
      await page.getByRole('button', { name: 'Reverse' }).click()
      await expect(page.getByText('Allocations (2)')).toBeVisible()
      // Exact match: the card description also contains the word
      // "reversal" ("A reversal undoes an allocation…").
      await expect(page.getByText('reversal', { exact: true })).toBeVisible()
      await expect(page.getByText('unallocated · বরাদ্দ হয়নি')).toBeVisible()
      await expect(
        page.getByText('receipt_complete_unallocated · রসিদ সম্পূর্ণ, বরাদ্দ বাকি'),
      ).toBeVisible()
      await expect(page.getByTestId('current-variance')).toContainText(
        'Cannot calculate yet — the batch is not fully allocated.',
      )
      await expect(page.getByTestId('current-variance')).not.toContainText(
        '৳-400.00',
      )
    })
  })

  test(
    'run detail page shows the completed run',
    async ({ page }) => {
      // Restored after the M9-BUG-1 fix: RunDetailPage now reads the
      // flat backend run payload (status + result_counts at top level,
      // results embedded) instead of expecting { run, results, total }.
      const ids = await setupToRun(page)
      await expect(page).toHaveURL(new RegExp(`/runs/${ids.runId}$`))
      await expect(page.getByText('completed').first()).toBeVisible()
      await expect(page.getByText('TRK-1001').first()).toBeVisible()
      await expect(page.getByText('needs_review').first()).toBeVisible()
    },
  )

  test('unauthenticated visitor is sent to login', async ({ page }) => {
    await page.goto('/')
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByText('Sign in to CourierLedger')).toBeVisible()
  })
})
