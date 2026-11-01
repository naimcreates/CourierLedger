// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { CourierStatementsPage } from './pages/CourierStatements'
import { ReportsPage } from './pages/Reports'
import { ShipmentsPage } from './pages/Shipments'

const WS_ID = '11111111-1111-1111-1111-111111111111'
const ORDER1_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const ORDER2_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
const BATCH_ID = '22222222-2222-2222-2222-222222222222'
const ACCOUNT_ID = '44444444-4444-4444-4444-444444444444'

const MOCK_WORKSPACES = [
  {
    id: WS_ID,
    name: 'Test Shop — Dhaka',
    trading_name: null,
    status: 'active',
    role: 'owner',
    created_at: '2026-10-31T00:00:00Z',
  },
]

const MOCK_ACCOUNTS = [
  {
    id: ACCOUNT_ID,
    courier_code: 'steadfast',
    merchant_account_ref: 'SF-TEST',
    display_label: 'Steadfast — Test',
    status: 'active',
  },
]

// Real backend shapes (backend/app/api/shipments.py + shipment_service.py):
// orders/shipments paginate with limit/offset; order COD may be NULL;
// a shipment has no status/amount_to_collect fields — it carries
// needs_review + needs_review_reason computed at read time.
const MOCK_ORDERS = {
  orders: [
    {
      order_id: ORDER1_ID,
      merchant_id: WS_ID,
      merchant_order_ref: 'ORD-1001',
      order_cod_poisha: 125000,
      currency: 'BDT',
      ordered_at: null,
      source: 'manual',
      created_at: '2026-11-01T00:00:00Z',
      updated_at: '2026-11-01T00:00:00Z',
    },
    {
      order_id: ORDER2_ID,
      merchant_id: WS_ID,
      merchant_order_ref: 'ORD-1002',
      order_cod_poisha: 89000,
      currency: 'BDT',
      ordered_at: null,
      source: 'manual',
      created_at: '2026-11-01T00:30:00Z',
      updated_at: '2026-11-01T00:30:00Z',
    },
  ],
  total: 2,
  limit: 20,
  offset: 0,
}

const MOCK_SHIPMENTS = [
  {
    shipment_id: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
    merchant_id: WS_ID,
    order_id: ORDER1_ID,
    merchant_courier_account_id: ACCOUNT_ID,
    tracking_id: 'TRK-1001',
    tracking_id_normalised: 'TRK-1001',
    outcome: 'delivered',
    outcome_source: 'merchant_confirmed',
    expected_collectible_cod_poisha: 125000,
    expected_collectible_derived_poisha: 125000,
    partial_amount_evidence_ref: null,
    is_cod_bearing: true,
    is_superseded: false,
    superseded_by_shipment_id: null,
    superseded_reason: null,
    shipped_on: '2026-10-28',
    needs_review: false,
    needs_review_reason: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  },
  {
    shipment_id: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
    merchant_id: WS_ID,
    order_id: ORDER2_ID,
    merchant_courier_account_id: null,
    tracking_id: 'TRK-1002',
    tracking_id_normalised: 'TRK-1002',
    outcome: 'unknown',
    outcome_source: 'merchant_confirmed',
    // Unknown outcome: expected collectible is NULL — never 0.
    expected_collectible_cod_poisha: null,
    expected_collectible_derived_poisha: null,
    partial_amount_evidence_ref: null,
    is_cod_bearing: false,
    is_superseded: false,
    superseded_by_shipment_id: null,
    superseded_reason: null,
    shipped_on: null,
    needs_review: true,
    needs_review_reason: 'outcome_not_confirmed',
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  },
  {
    shipment_id: 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
    merchant_id: WS_ID,
    order_id: ORDER1_ID,
    merchant_courier_account_id: ACCOUNT_ID,
    tracking_id: 'TRK-1003',
    tracking_id_normalised: 'TRK-1003',
    outcome: 'returned',
    outcome_source: 'statement',
    // Confirmed return: verified 0, labelled as such.
    expected_collectible_cod_poisha: 0,
    expected_collectible_derived_poisha: 0,
    partial_amount_evidence_ref: null,
    is_cod_bearing: false,
    is_superseded: false,
    superseded_by_shipment_id: null,
    superseded_reason: null,
    shipped_on: '2026-10-29',
    needs_review: false,
    needs_review_reason: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  },
]

const MOCK_IMPORTS = [
  {
    batch_id: BATCH_ID,
    status: 'parsed',
    row_count: 12,
    parsed_row_count: 10,
    error_row_count: 2,
    file_object_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    mapping_snapshot: null,
    created_at: '2026-10-31T10:00:00Z',
  },
]

// Real backend shape (backend/app/api/statements.py): nested shipment /
// courier_account / import_batch objects; parse_status may be null.
const MOCK_STATEMENTS = [
  {
    statement_line_id: '11111111-2222-3333-4444-555555555555',
    import_batch_id: BATCH_ID,
    import_row_id: '12121212-1212-1212-1212-121212121212',
    row_number: 1,
    parse_status: 'parsed',
    raw_values: { 'Tracking ID': 'TRK-1001', 'Collected (poisha)': '125000' },
    match_status: 'strong_matched',
    tracking_id_raw: 'TRK-1001',
    tracking_id_normalised: 'TRK-1001',
    merchant_order_ref_raw: 'ORD-1001',
    reported_status_raw: 'Delivered',
    statement_date: '2026-10-30',
    reported_collected_poisha: 125000,
    reported_collected_state: 'present_valid',
    reported_payout_poisha: 117000,
    reported_payout_state: 'present_valid',
    reported_delivery_fee_poisha: 7000,
    reported_delivery_fee_state: 'present_valid',
    reported_cod_fee_poisha: 1000,
    reported_cod_fee_state: 'present_valid',
    reported_return_fee_poisha: 0,
    reported_return_fee_state: 'explicit_zero',
    reported_other_fee_poisha: null,
    reported_other_fee_state: 'blank',
    shipment_id: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
    shipment: {
      shipment_id: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
      tracking_id_raw: 'TRK-1001',
      tracking_id_normalised: 'TRK-1001',
      outcome: 'delivered',
      expected_collectible_cod_poisha: 125000,
      is_cod_bearing: true,
    },
    courier_account: {
      merchant_courier_account_id: ACCOUNT_ID,
      courier_code: 'steadfast',
      display_label: 'Steadfast — Test',
      merchant_account_ref: 'SF-TEST',
      status: 'active',
    },
    import_batch: {
      batch_id: BATCH_ID,
      status: 'parsed',
      statement_period_start: null,
      statement_period_end: null,
      row_count: 12,
      parsed_row_count: 10,
      error_row_count: 2,
      created_at: '2026-10-31T10:00:00Z',
    },
    created_at: '2026-10-31T10:30:00Z',
  },
  {
    statement_line_id: '66666666-7777-8888-9999-000000000000',
    import_batch_id: BATCH_ID,
    import_row_id: '34343434-3434-3434-3434-343434343434',
    row_number: 2,
    parse_status: 'parsed',
    raw_values: { 'Tracking ID': 'TRK-9999', 'Collected (poisha)': '50000' },
    match_status: 'unmatched',
    tracking_id_raw: 'TRK-9999',
    tracking_id_normalised: 'TRK-9999',
    merchant_order_ref_raw: 'ORD-9999',
    reported_status_raw: 'Delivered',
    statement_date: '2026-10-30',
    reported_collected_poisha: 50000,
    reported_collected_state: 'present_valid',
    reported_payout_poisha: null,
    reported_payout_state: 'blank',
    reported_delivery_fee_poisha: null,
    reported_delivery_fee_state: 'blank',
    reported_cod_fee_poisha: null,
    reported_cod_fee_state: 'blank',
    reported_return_fee_poisha: null,
    reported_return_fee_state: 'blank',
    reported_other_fee_poisha: null,
    reported_other_fee_state: 'blank',
    shipment_id: null,
    shipment: null,
    courier_account: {
      merchant_courier_account_id: ACCOUNT_ID,
      courier_code: 'steadfast',
      display_label: 'Steadfast — Test',
      merchant_account_ref: 'SF-TEST',
      status: 'active',
    },
    import_batch: {
      batch_id: BATCH_ID,
      status: 'parsed',
      statement_period_start: null,
      statement_period_end: null,
      row_count: 12,
      parsed_row_count: 10,
      error_row_count: 2,
      created_at: '2026-10-31T10:00:00Z',
    },
    created_at: '2026-10-31T10:31:00Z',
  },
]

// Real backend shape (backend/app/api/reports.py): nested aggregates;
// each variance total is {total_poisha, non_null_count, null_count}
// and is always a number — 0 when there are no results.
const MOCK_SUMMARY = {
  workspace_id: WS_ID,
  reconciliation_runs: {
    total: 2,
    by_status: { running: 0, completed: 2, failed: 0 },
  },
  reconciliation_results: {
    total: 7,
    by_overall_status: {
      matched: 3,
      open: 1,
      mismatch: 1,
      needs_review: 2,
      matched_with_tolerance: 0,
    },
    variances_poisha: {
      collection_c: { total_poisha: -5000, non_null_count: 7, null_count: 0 },
      fee_v_r: { total_poisha: -700, non_null_count: 6, null_count: 1 },
      statement_s: { total_poisha: 0, non_null_count: 7, null_count: 0 },
      conditional_payout_v_cond: { total_poisha: 2000, non_null_count: 5, null_count: 2 },
      scenario_v_total: { total_poisha: -4950, non_null_count: 7, null_count: 0 },
    },
  },
  settlement_batches: {
    total: 2,
    by_allocation_status: {
      unallocated: 1,
      partially_allocated: 1,
      fully_allocated: 0,
    },
    by_bank_status: {
      no_receipt: 1,
      partial_receipt: 1,
      receipt_complete_unallocated: 0,
      fully_reconciled: 0,
    },
  },
  bank_receipts: { total: 2, complete: 1, incomplete: 1 },
  import_batches: {
    total: 1,
    by_status: {
      uploaded: 0,
      mapped: 0,
      parsed: 1,
      parse_failed: 0,
      ready: 0,
      superseded: 0,
    },
  },
}

const EMPTY_SUMMARY = {
  workspace_id: WS_ID,
  reconciliation_runs: {
    total: 0,
    by_status: { running: 0, completed: 0, failed: 0 },
  },
  reconciliation_results: {
    total: 0,
    by_overall_status: {
      matched: 0,
      open: 0,
      mismatch: 0,
      needs_review: 0,
      matched_with_tolerance: 0,
    },
    variances_poisha: {
      collection_c: { total_poisha: 0, non_null_count: 0, null_count: 0 },
      fee_v_r: { total_poisha: 0, non_null_count: 0, null_count: 0 },
      statement_s: { total_poisha: 0, non_null_count: 0, null_count: 0 },
      conditional_payout_v_cond: { total_poisha: 0, non_null_count: 0, null_count: 0 },
      scenario_v_total: { total_poisha: 0, non_null_count: 0, null_count: 0 },
    },
  },
  settlement_batches: {
    total: 0,
    by_allocation_status: {
      unallocated: 0,
      partially_allocated: 0,
      fully_allocated: 0,
    },
    by_bank_status: {
      no_receipt: 0,
      partial_receipt: 0,
      receipt_complete_unallocated: 0,
      fully_reconciled: 0,
    },
  },
  bank_receipts: { total: 0, complete: 0, incomplete: 0 },
  import_batches: {
    total: 0,
    by_status: {
      uploaded: 0,
      mapped: 0,
      parsed: 0,
      parse_failed: 0,
      ready: 0,
      superseded: 0,
    },
  },
}

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderWithClient(ui: React.ReactElement) {
  return render(<QueryClientProvider client={makeClient()}>{ui}</QueryClientProvider>)
}

function installFetch(
  handler: (url: string, init?: RequestInit) => Promise<unknown>,
  posted: Array<{ url: string; body: Record<string, unknown> }> = [],
) {
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST' && init.body) {
      posted.push({ url, body: JSON.parse(init.body as string) })
    }
    const payload = await handler(url, init)
    return { ok: true, status: 200, json: async () => payload }
  }) as unknown as typeof fetch
}

function baseHandler(url: string, init?: RequestInit): Promise<unknown> {
  const u = new URL(url, 'http://test')
  const path = u.pathname
  if (path === '/api/workspaces') return Promise.resolve(MOCK_WORKSPACES)
  if (path === '/api/auth/csrf') return Promise.resolve({ csrf_token: 'test-csrf' })
  if (path === `/api/workspaces/${WS_ID}/courier-accounts`) {
    return Promise.resolve(MOCK_ACCOUNTS)
  }
  if (path === `/api/workspaces/${WS_ID}/orders`) {
    if (init?.method === 'POST') {
      const body = JSON.parse(init?.body as string) as Record<string, unknown>
      return Promise.resolve({
        order_id: '99999999-9999-9999-9999-999999999999',
        merchant_id: WS_ID,
        merchant_order_ref: body.merchant_order_ref,
        order_cod_poisha: body.order_cod_poisha ?? null,
        currency: 'BDT',
        ordered_at: null,
        source: 'manual',
        created_at: '2026-11-01T01:00:00Z',
        updated_at: '2026-11-01T01:00:00Z',
      })
    }
    return Promise.resolve(MOCK_ORDERS)
  }
  if (path === `/api/workspaces/${WS_ID}/orders/${ORDER1_ID}/shipments`) {
    const body = JSON.parse(init?.body as string) as Record<string, unknown>
    return Promise.resolve({
      ...MOCK_SHIPMENTS[0],
      shipment_id: '12121212-1212-1212-1212-121212121212',
      tracking_id: body.tracking_id,
      tracking_id_normalised: String(body.tracking_id).toUpperCase(),
      outcome: body.outcome,
      outcome_source: body.outcome_source,
      is_cod_bearing: body.is_cod_bearing,
      partial_amount_evidence_ref: body.partial_amount_evidence_ref ?? null,
      expected_collectible_cod_poisha: body.amount_to_collect_poisha ?? null,
      expected_collectible_derived_poisha: body.amount_to_collect_poisha ?? null,
      needs_review: false,
      needs_review_reason: null,
    })
  }
  if (path === `/api/workspaces/${WS_ID}/shipments`) {
    // The real backend matches tracking_id on the normalised value.
    const tracking = u.searchParams.get('tracking_id')
    const rows = tracking
      ? MOCK_SHIPMENTS.filter(
          (s) => s.tracking_id_normalised === tracking.trim().toUpperCase(),
        )
      : MOCK_SHIPMENTS
    return Promise.resolve({
      shipments: rows,
      total: rows.length,
      limit: 20,
      offset: 0,
    })
  }
  if (path === `/api/workspaces/${WS_ID}/imports`) {
    return Promise.resolve(MOCK_IMPORTS)
  }
  if (path === `/api/workspaces/${WS_ID}/statements`) {
    const match = u.searchParams.get('match_status')
    const batch = u.searchParams.get('import_batch_id')
    let rows = MOCK_STATEMENTS
    if (match) rows = rows.filter((s) => s.match_status === match)
    if (batch) rows = rows.filter((s) => s.import_batch_id === batch)
    return Promise.resolve({
      statements: rows,
      total: rows.length,
      page: 1,
      page_size: 20,
    })
  }
  if (path.startsWith(`/api/workspaces/${WS_ID}/statements/`)) {
    const lineId = path.split('/').pop()
    const line = MOCK_STATEMENTS.find((s) => s.statement_line_id === lineId)
    if (!line) throw new Error(`unknown statement line in test: ${lineId}`)
    return Promise.resolve(line)
  }
  if (path === `/api/workspaces/${WS_ID}/reports/summary`) {
    return Promise.resolve(MOCK_SUMMARY)
  }
  throw new Error(`unexpected fetch URL in test: ${url} ${init?.method ?? 'GET'}`)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ShipmentsPage — real list (M8)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('renders real shipments with NULL expected collectible shown explicitly, never as ৳0.00', async () => {
    installFetch((url, init) => baseHandler(url, init))
    renderWithClient(
      <MemoryRouter>
        <ShipmentsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Shipments (3)')).toBeInTheDocument()
    })
    expect(screen.getByText('TRK-1001')).toBeInTheDocument()
    expect(screen.getByText('TRK-1002')).toBeInTheDocument()
    expect(screen.getByText('TRK-1003')).toBeInTheDocument()
    // Order refs come from the orders list, not the shipment payload.
    expect(screen.getAllByText('ORD-1001').length).toBeGreaterThanOrEqual(1)
    // Delivered: expected collectible is the full amount.
    expect(screen.getAllByText('৳1,250.00').length).toBeGreaterThanOrEqual(1)
    // Unknown outcome: NULL is shown explicitly, never as ৳0.00, and
    // the backend's needs-review reason is explained.
    expect(screen.getByText('NULL — unknown, not ৳0.00')).toBeInTheDocument()
    expect(screen.getByText('needs review · যাচাই প্রয়োজন')).toBeInTheDocument()
    expect(
      screen.getByText(/The outcome is not confirmed yet/),
    ).toBeInTheDocument()
    // Confirmed return: verified zero, labelled as such — distinct from NULL.
    expect(screen.getByText('(verified zero)')).toBeInTheDocument()
    // The V2.1 outcome rule is explained on the page.
    expect(screen.getByText(/Expected collectible COD — the outcome rule/)).toBeInTheDocument()
    expect(screen.getByText(/never silently assumed to be collectible/)).toBeInTheDocument()
  })

  it('filters by tracking id', async () => {
    const fetched: string[] = []
    installFetch((url, init) => {
      fetched.push(url)
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter>
        <ShipmentsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Shipments (3)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Find by tracking id'), {
      target: { value: 'TRK-1002' },
    })
    fireEvent.click(screen.getByText('Filter'))
    await waitFor(() => {
      expect(screen.getByText('Shipments (1)')).toBeInTheDocument()
    })
    expect(screen.getByText('TRK-1002')).toBeInTheDocument()
    expect(screen.queryByText('TRK-1001')).not.toBeInTheDocument()
    expect(fetched.some((u) => u.includes('tracking_id=TRK-1002'))).toBe(true)
    // The real backend paginates with limit/offset, not page/page_size.
    expect(fetched.some((u) => u.includes('limit=20&offset=0'))).toBe(true)
  })

  it('creates an order, converting taka to poisha', async () => {
    const posted: Array<{ url: string; body: Record<string, unknown> }> = []
    installFetch((url, init) => baseHandler(url, init), posted)
    renderWithClient(
      <MemoryRouter>
        <ShipmentsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Shipments (3)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Merchant order ref'), {
      target: { value: 'ORD-2001' },
    })
    fireEvent.change(screen.getByLabelText('Order COD ৳ (optional)'), {
      target: { value: '1250.50' },
    })
    fireEvent.click(screen.getByText('Create order'))
    await waitFor(() => {
      expect(screen.getByText('Order ORD-2001 created.')).toBeInTheDocument()
    })
    const orderPost = posted.find((p) => p.url.endsWith('/orders'))
    expect(orderPost).toBeDefined()
    // ৳1,250.50 -> 125050 poisha.
    expect(orderPost?.body).toEqual({
      merchant_order_ref: 'ORD-2001',
      order_cod_poisha: 125050,
    })
  })

  it('creates an order with an unknown COD — blank stays NULL, never 0', async () => {
    const posted: Array<{ url: string; body: Record<string, unknown> }> = []
    installFetch((url, init) => baseHandler(url, init), posted)
    renderWithClient(
      <MemoryRouter>
        <ShipmentsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Shipments (3)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Merchant order ref'), {
      target: { value: 'ORD-2002' },
    })
    fireEvent.click(screen.getByText('Create order'))
    await waitFor(() => {
      expect(screen.getByText('Order ORD-2002 created.')).toBeInTheDocument()
    })
    const orderPost = posted.find((p) => p.url.endsWith('/orders'))
    expect(orderPost?.body).toEqual({
      merchant_order_ref: 'ORD-2002',
      order_cod_poisha: null,
    })
  })

  it('adds a partial-delivery shipment with amount + evidence ref, never a partial_amount_poisha field', async () => {
    const posted: Array<{ url: string; body: Record<string, unknown> }> = []
    installFetch((url, init) => baseHandler(url, init), posted)
    renderWithClient(
      <MemoryRouter>
        <ShipmentsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Shipments (3)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Order'), {
      target: { value: ORDER1_ID },
    })
    fireEvent.change(screen.getByLabelText('Tracking id'), {
      target: { value: 'TRK-2001' },
    })
    fireEvent.change(screen.getByLabelText('Outcome'), {
      target: { value: 'partially_delivered' },
    })
    fireEvent.change(screen.getByLabelText('Amount to collect ৳ (optional)'), {
      target: { value: '500.00' },
    })
    fireEvent.change(screen.getByLabelText('Partial amount evidence ref (optional)'), {
      target: { value: 'courier message 12' },
    })
    fireEvent.click(screen.getByText('Add shipment'))
    await waitFor(() => {
      expect(screen.getByText('Shipment TRK-2001 created.')).toBeInTheDocument()
    })
    const shipmentPost = posted.find((p) => p.url.includes('/shipments'))
    expect(shipmentPost).toBeDefined()
    expect(shipmentPost?.url).toBe(`/api/workspaces/${WS_ID}/orders/${ORDER1_ID}/shipments`)
    expect(shipmentPost?.body.tracking_id).toBe('TRK-2001')
    expect(shipmentPost?.body.outcome).toBe('partially_delivered')
    expect(shipmentPost?.body.outcome_source).toBe('merchant_confirmed')
    expect(shipmentPost?.body.is_cod_bearing).toBe(true)
    expect(shipmentPost?.body.amount_to_collect_poisha).toBe(50000)
    expect(shipmentPost?.body.partial_amount_evidence_ref).toBe('courier message 12')
    // The real backend rejects partial_amount_poisha (extra="forbid").
    expect(shipmentPost?.body).not.toHaveProperty('partial_amount_poisha')
    expect(shipmentPost?.body.shipped_on).toBeNull()
  })

  it('sends no amount for a returned shipment — the backend would reject it', async () => {
    const posted: Array<{ url: string; body: Record<string, unknown> }> = []
    installFetch((url, init) => baseHandler(url, init), posted)
    renderWithClient(
      <MemoryRouter>
        <ShipmentsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Shipments (3)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Order'), {
      target: { value: ORDER1_ID },
    })
    fireEvent.change(screen.getByLabelText('Tracking id'), {
      target: { value: 'TRK-2002' },
    })
    fireEvent.change(screen.getByLabelText('Outcome'), {
      target: { value: 'returned' },
    })
    // The amount field is disabled for amountless outcomes.
    expect(screen.getByLabelText('Amount to collect ৳ (optional)')).toBeDisabled()
    fireEvent.click(screen.getByText('Add shipment'))
    await waitFor(() => {
      expect(screen.getByText('Shipment TRK-2002 created.')).toBeInTheDocument()
    })
    const shipmentPost = posted.find((p) => p.url.includes('/shipments'))
    expect(shipmentPost?.body.amount_to_collect_poisha).toBeNull()
  })
})

describe('CourierStatementsPage — real list and detail (M8)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('renders statement lines with match badges, Bangla labels and DQ states', async () => {
    installFetch((url, init) => baseHandler(url, init))
    renderWithClient(
      <MemoryRouter>
        <CourierStatementsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Statement lines (2)')).toBeInTheDocument()
    })
    expect(screen.getByText('TRK-1001')).toBeInTheDocument()
    expect(screen.getByText('TRK-9999')).toBeInTheDocument()
    // The same text also appears in the filter <select> options, so
    // assert on all matches, not a single element.
    expect(screen.getAllByText('strong_matched · দৃঢ়ভাবে মিলেছে').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('unmatched · মেলেনি').length).toBeGreaterThanOrEqual(1)
    // Blank payout is unknown, never a silent zero.
    expect(screen.getByText('blank — unknown, not zero')).toBeInTheDocument()
    expect(screen.getAllByText('present').length).toBeGreaterThanOrEqual(1)
    // Courier comes from the nested courier account object.
    expect(screen.getAllByText('steadfast').length).toBeGreaterThanOrEqual(1)
  })

  it('filters by match status and shows the detail view with verbatim raw values', async () => {
    const fetched: string[] = []
    installFetch((url, init) => {
      fetched.push(url)
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter>
        <CourierStatementsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Statement lines (2)')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('Match status'), {
      target: { value: 'unmatched' },
    })
    await waitFor(() => {
      expect(screen.getByText('Statement lines (1)')).toBeInTheDocument()
    })
    expect(fetched.some((u) => u.includes('match_status=unmatched'))).toBe(true)
    expect(screen.queryByText('TRK-1001')).not.toBeInTheDocument()
    // Open the detail view for the unmatched line — it fetches the
    // single line from GET /statements/{lineId}.
    fireEvent.click(screen.getByText('TRK-9999'))
    await waitFor(() => {
      expect(screen.getByText('Statement line detail')).toBeInTheDocument()
    })
    expect(
      fetched.some((u) =>
        u.endsWith('/statements/66666666-7777-8888-9999-000000000000'),
      ),
    ).toBe(true)
    expect(screen.getByText('No shipment linked — needs human confirmation.')).toBeInTheDocument()
    // Raw values are shown verbatim from the file.
    expect(screen.getByText(/"Collected \(poisha\)"/)).toBeInTheDocument()
    expect(screen.getByText(/"Tracking ID"/)).toBeInTheDocument()
    // The nested import batch is shown in the detail footer.
    expect(screen.getByText(/12 rows \(10 parsed, 2 errors\)/)).toBeInTheDocument()
  })

  it('shows the linked shipment in the detail view for a matched line', async () => {
    installFetch((url, init) => baseHandler(url, init))
    renderWithClient(
      <MemoryRouter>
        <CourierStatementsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Statement lines (2)')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('TRK-1001'))
    await waitFor(() => {
      expect(screen.getByText('Statement line detail')).toBeInTheDocument()
    })
    expect(screen.getByText(/outcome: delivered/)).toBeInTheDocument()
    expect(screen.getByText(/expected collectible: ৳1,250.00/)).toBeInTheDocument()
  })
})

describe('ReportsPage — real aggregates (M8)', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('shows run/result counts and the five variance totals separately, never summed', async () => {
    installFetch((url, init) => baseHandler(url, init))
    renderWithClient(
      <MemoryRouter>
        <ReportsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Runs by status')).toBeInTheDocument()
    })
    // Each variance total lives in its own card with its own value
    // and its known/unknown counts.
    const c = screen.getByTestId('variance-total-collection_c')
    expect(c.textContent).toContain('৳-50.00')
    expect(c.textContent).toContain('7 known values')
    const vR = screen.getByTestId('variance-total-fee_v_r')
    expect(vR.textContent).toContain('৳-7.00')
    expect(vR.textContent).toContain('6 known values')
    expect(vR.textContent).toContain('1 unknown (not counted)')
    const s = screen.getByTestId('variance-total-statement_s')
    expect(s.textContent).toContain('৳0.00')
    const vCond = screen.getByTestId('variance-total-conditional_payout_v_cond')
    expect(vCond.textContent).toContain('৳20.00')
    expect(vCond.textContent).toContain('2 unknown (not counted)')
    const vTotal = screen.getByTestId('variance-total-scenario_v_total')
    expect(vTotal.textContent).toContain('৳-49.50')
    // The page states plainly that the totals are never added together.
    expect(screen.getByText(/never added together/)).toBeInTheDocument()
    // Each variance card holds exactly one ৳ figure — its own total, not a sum.
    for (const key of [
      'collection_c',
      'fee_v_r',
      'statement_s',
      'conditional_payout_v_cond',
      'scenario_v_total',
    ]) {
      const card = screen.getByTestId(`variance-total-${key}`)
      expect((card.textContent?.match(/৳/g) ?? []).length).toBe(1)
    }
    // Result counts by overall status, with Bangla labels.
    expect(screen.getByText('matched · মিলেছে')).toBeInTheDocument()
    expect(screen.getByText('needs_review · যাচাই প্রয়োজন')).toBeInTheDocument()
    // Bank receipts split complete / incomplete; import batches by status.
    expect(screen.getByText('1 complete · 1 incomplete')).toBeInTheDocument()
  })

  it('shows an honest empty state: zeros and ৳0.00 over 0 results, never invented figures', async () => {
    installFetch((url, init) => {
      const u = new URL(url, 'http://test')
      if (u.pathname === `/api/workspaces/${WS_ID}/reports/summary`) {
        return Promise.resolve(EMPTY_SUMMARY)
      }
      return baseHandler(url, init)
    })
    renderWithClient(
      <MemoryRouter>
        <ReportsPage />
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText(/Nothing to report yet/)).toBeInTheDocument()
    })
    // All five variance totals are ৳0.00 over 0 results — the real
    // backend returns 0, never null, when there is nothing to total.
    for (const key of [
      'collection_c',
      'fee_v_r',
      'statement_s',
      'conditional_payout_v_cond',
      'scenario_v_total',
    ]) {
      const card = screen.getByTestId(`variance-total-${key}`)
      expect(card.textContent).toContain('৳0.00')
      expect(card.textContent).toContain('no results yet')
    }
    // Counts are 0 — the real backend returns every known status key
    // with 0, so the lists render zero rows rather than an empty label.
    expect(screen.getByText('unallocated · বরাদ্দ হয়নি')).toBeInTheDocument()
    expect(screen.getByText('no_receipt · রসিদ নেই')).toBeInTheDocument()
    expect(screen.getByText('0 complete · 0 incomplete')).toBeInTheDocument()
  })
})

describe('auth gate — M8 pages (M8)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it.each(['/shipments', '/courier-statements', '/reports'])(
    'redirects an unauthenticated user from %s to the login page',
    async (path) => {
      globalThis.fetch = vi.fn(async (url: string) => {
        if (url === '/api/auth/me') {
          return {
            ok: false,
            status: 401,
            json: async () => ({ detail: 'Not authenticated.' }),
          }
        }
        throw new Error(`unexpected fetch URL in test: ${url}`)
      }) as unknown as typeof fetch
      window.history.pushState({}, '', path)
      render(<App />)
      await waitFor(() => {
        expect(screen.getByText('Sign in to CourierLedger')).toBeInTheDocument()
      })
    },
  )
})
