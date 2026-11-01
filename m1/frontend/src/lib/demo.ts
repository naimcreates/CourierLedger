/**
 * Synthetic demo data ONLY. These rows are invented for UI development and
 * are always labelled "SYNTHETIC DEMO" in the UI. They are not real
 * merchants, couriers, rates, or shipments.
 */

export type ReconciliationStatus =
  | 'matched'
  | 'mismatch'
  | 'needs_review'
  | 'open'
  | 'matched_with_tolerance'

/** Short Bangla explanation for each financial status. */
export const STATUS_BANGLA: Record<ReconciliationStatus, string> = {
  matched: 'মিলেছে',
  mismatch: 'অমিল',
  needs_review: 'যাচাই প্রয়োজন',
  open: 'খোলা',
  matched_with_tolerance: 'সহনশীলতার মধ্যে মিলেছে',
}

/** One-line plain-English explanation for each status, shown in the UI. */
export const STATUS_EXPLANATION: Record<ReconciliationStatus, string> = {
  matched: 'Expected and reported figures agree exactly.',
  mismatch:
    'A fee, statement-arithmetic or contractual-payout finding is non-zero. Review before acting.',
  needs_review:
    'Some required information is missing or invalid, so the figures cannot be calculated exactly.',
  open: 'Only the collection differs from what was expected. The contractual payout still matches — this is not a confirmed loss.',
  matched_with_tolerance:
    'Figures differ, but every difference is within the configured tolerance.',
}

export interface DemoShipment {
  id: string
  trackingId: string
  courier: string
  orderId: string
  expectedCodPoisha: number
  reportedCollectedPoisha: number | null
  status: ReconciliationStatus
}

export const DEMO_SHIPMENTS: DemoShipment[] = [
  {
    id: 'demo-1',
    trackingId: 'SF-DEMO-1001',
    courier: 'Steadfast',
    orderId: 'ORD-1001',
    expectedCodPoisha: 125000,
    reportedCollectedPoisha: 125000,
    status: 'matched',
  },
  {
    id: 'demo-2',
    trackingId: 'SF-DEMO-1002',
    courier: 'Steadfast',
    orderId: 'ORD-1002',
    expectedCodPoisha: 89000,
    reportedCollectedPoisha: 85000,
    status: 'open',
  },
  {
    id: 'demo-3',
    trackingId: 'PT-DEMO-2001',
    courier: 'Pathao',
    orderId: 'ORD-2001',
    expectedCodPoisha: 210000,
    reportedCollectedPoisha: null,
    status: 'needs_review',
  },
  {
    id: 'demo-4',
    trackingId: 'PT-DEMO-2002',
    courier: 'Pathao',
    orderId: 'ORD-2002',
    expectedCodPoisha: 64000,
    reportedCollectedPoisha: 64000,
    status: 'matched_with_tolerance',
  },
  {
    id: 'demo-5',
    trackingId: 'SF-DEMO-1003',
    courier: 'Steadfast',
    orderId: 'ORD-1003',
    expectedCodPoisha: 150000,
    reportedCollectedPoisha: 140000,
    status: 'mismatch',
  },
]

export interface DemoWorkspace {
  id: string
  name: string
  couriers: string
}

export const DEMO_WORKSPACES: DemoWorkspace[] = [
  { id: 'demo-ws-1', name: 'Demo Shop — Dhaka', couriers: 'Steadfast, Pathao' },
  { id: 'demo-ws-2', name: 'Demo Shop — Chattogram', couriers: 'Steadfast' },
]

/** Format integer poisha as ৳ with two decimals, e.g. 125000 -> ৳1,250.00 */
export function formatTaka(poisha: number | null): string {
  if (poisha === null) return '—'
  return `৳${(poisha / 100).toLocaleString('en-BD', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

/**
 * Synthetic reconcile-demo input. Five shipments, one for each V2.2.1
 * overall status. The input values are invented for UI development and
 * use the engine's SYNTHETIC FIXTURE rules (Fixture Alpha, 1% COD fee
 * on the full COD) — they are not real courier terms. Verified against
 * the live engine 2026-10-31: the statuses below are exactly what
 * POST /api/reconcile returns for these inputs.
 *
 * - SF-DEMO-A: matched (everything agrees exactly)
 * - SF-DEMO-B: open (collection-only shortfall of ৳50.00; contractual payout still matched)
 * - SF-DEMO-C: needs_review (outcome unknown, no amount to collect)
 * - SF-DEMO-D: mismatch (reported payout ৳200.00 short of the contractual payout)
 * - SF-DEMO-E: matched_with_tolerance (payout ৳1.00 short, tolerance ৳5.00)
 *
 * Demo E needs a tolerance of 500 poisha; the other four run at 0.
 */
export interface DemoReconcileShipment {
  merchant_order_id: string
  tracking_id: string
  courier_fixture: string
  outcome: string
  amount_to_collect_poisha: number | null
  reported_collected_poisha: number | null
  expected_delivery_fee_poisha: number
  reported_delivery_fee_poisha: number
  cod_fee_base: string
  cod_fee_rate_bp: number
  other_fees_poisha: number
  excluded_tax_poisha: number
  reported_deductions_total_poisha: number
  reported_payout_poisha: number
  /** Tolerance (poisha) to send with this shipment. 0 unless stated. */
  tolerance_poisha?: number
}

function demoReconcileBase(over: Partial<DemoReconcileShipment>): DemoReconcileShipment {
  return {
    merchant_order_id: 'ORD-DEMO',
    tracking_id: 'SF-DEMO-A',
    courier_fixture: 'Fixture Alpha',
    outcome: 'delivered',
    amount_to_collect_poisha: 100000,
    reported_collected_poisha: 100000,
    expected_delivery_fee_poisha: 7000,
    reported_delivery_fee_poisha: 7000,
    cod_fee_base: 'full_cod',
    cod_fee_rate_bp: 100,
    other_fees_poisha: 0,
    excluded_tax_poisha: 0,
    reported_deductions_total_poisha: 8000,
    reported_payout_poisha: 92000,
    ...over,
  }
}

export const DEMO_RECONCILE_SHIPMENTS: DemoReconcileShipment[] = [
  demoReconcileBase({ merchant_order_id: 'ORD-DEMO-A', tracking_id: 'SF-DEMO-A' }),
  demoReconcileBase({
    merchant_order_id: 'ORD-DEMO-B',
    tracking_id: 'SF-DEMO-B',
    reported_collected_poisha: 95000,
    reported_deductions_total_poisha: 7950,
    reported_payout_poisha: 87050,
  }),
  demoReconcileBase({
    merchant_order_id: 'ORD-DEMO-C',
    tracking_id: 'SF-DEMO-C',
    outcome: 'unknown',
    amount_to_collect_poisha: null,
  }),
  demoReconcileBase({
    merchant_order_id: 'ORD-DEMO-D',
    tracking_id: 'SF-DEMO-D',
    reported_payout_poisha: 90000,
  }),
  demoReconcileBase({
    merchant_order_id: 'ORD-DEMO-E',
    tracking_id: 'SF-DEMO-E',
    reported_payout_poisha: 91900,
    tolerance_poisha: 500,
  }),
]

/** Default column mapping for the generic synthetic statement fixture
 * (backend/tests/data/generic_statement_fixture.csv). Canonical field ->
 * source header. The backend validates the mapping and rejects unknown
 * canonical fields (422). */
export const DEFAULT_STATEMENT_MAPPING: Record<string, string> = {
  tracking_id: 'Tracking ID',
  merchant_order_ref: 'Order Ref',
  reported_collected: 'Collected (poisha)',
  reported_payout: 'Payout (poisha)',
  delivery_fee: 'Delivery Fee (poisha)',
  cod_fee: 'COD Fee (poisha)',
  return_fee: 'Return Fee (poisha)',
  other_fee: 'Other Fee (poisha)',
  shipment_status: 'Status',
  statement_date: 'Statement Date',
}

/** The ten canonical fields the backend import accepts (v0.1 C.2). */
export const CANONICAL_FIELDS: string[] = [
  'tracking_id',
  'merchant_order_ref',
  'reported_collected',
  'reported_payout',
  'delivery_fee',
  'cod_fee',
  'return_fee',
  'other_fee',
  'shipment_status',
  'statement_date',
]
