import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  createOrder,
  createShipmentForOrder,
  listCourierAccounts,
  listOrders,
  listShipments,
  type OutcomeSource,
  type ShipmentOutcome,
} from '../lib/api'
import { formatTaka } from '../lib/demo'
import { parseTakaToPoisha } from '../lib/settlement'
import {
  NEEDS_REVIEW_REASON,
  OUTCOME_BANGLA,
  OUTCOME_COLLECTIBLE_RULE,
  OUTCOME_SOURCE_BANGLA,
  SHIPMENT_OUTCOMES,
} from '../lib/shipments'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { Input } from '../components/ui/Input'
import { Label } from '../components/ui/Label'
import { WorkspacePicker } from '../components/WorkspacePicker'

function shortId(id: string | null | undefined): string {
  return id ? `${id.slice(0, 8)}…` : '—'
}

const PAGE_SIZE = 20

/** Outcomes for which an amount to collect is meaningless — the
 * backend rejects it (422) instead of storing or ignoring it. */
const AMOUNTLESS_OUTCOMES: ShipmentOutcome[] = [
  'returned',
  'cancelled',
  'in_transit',
  'unknown',
]

const OUTCOME_SOURCES: OutcomeSource[] = [
  'merchant_confirmed',
  'statement',
  'system_inferred_label_only',
]

/**
 * Expected collectible COD, shown exactly as the backend returns it.
 * NULL is unknown — rendered explicitly as NULL, never as ৳0.00.
 * Verified ৳0.00 (confirmed return / cancellation / superseded) is
 * labelled as such so it cannot be mistaken for an unknown.
 */
function ExpectedCollectibleCell({
  poisha,
  derived,
}: {
  poisha: number | null
  derived: number | null
}) {
  if (poisha === null) {
    return <Badge variant="warning">NULL — unknown, not ৳0.00</Badge>
  }
  if (poisha === 0) {
    return (
      <span className="font-medium">
        ৳0.00 <span className="text-xs text-slate-500">(verified zero)</span>
      </span>
    )
  }
  return (
    <span className="font-medium">
      {formatTaka(poisha)}
      {derived !== null && derived !== poisha && (
        <span className="block text-xs font-normal text-slate-500">
          derived: {formatTaka(derived)}
        </span>
      )}
    </span>
  )
}

/**
 * Shipments: real shipments from GET /api/workspaces/:id/shipments,
 * plus forms to create an order and to add a shipment to it.
 *
 * The labelled synthetic demo reconcile run lives on the Reconciliation
 * page — nothing here is demo data, and no demo figure is mixed into
 * the real list.
 */
export function ShipmentsPage() {
  const { selectedId } = useSelectedWorkspace()
  const queryClient = useQueryClient()

  const [trackingFilter, setTrackingFilter] = useState('')
  const [appliedTracking, setAppliedTracking] = useState('')
  const [page, setPage] = useState(1)

  // Create-order form state.
  const [orderRef, setOrderRef] = useState('')
  const [orderCod, setOrderCod] = useState('')
  const [orderError, setOrderError] = useState<string | null>(null)
  const [orderSuccess, setOrderSuccess] = useState<string | null>(null)

  // Create-shipment form state.
  const [shipmentOrderId, setShipmentOrderId] = useState('')
  const [shipmentTracking, setShipmentTracking] = useState('')
  const [shipmentOutcome, setShipmentOutcome] = useState<ShipmentOutcome>('delivered')
  const [shipmentOutcomeSource, setShipmentOutcomeSource] =
    useState<OutcomeSource>('merchant_confirmed')
  const [shipmentIsCodBearing, setShipmentIsCodBearing] = useState(true)
  const [shipmentAmount, setShipmentAmount] = useState('')
  const [shipmentEvidenceRef, setShipmentEvidenceRef] = useState('')
  const [shipmentCourierAccountId, setShipmentCourierAccountId] = useState('')
  const [shipmentDate, setShipmentDate] = useState('')
  const [shipmentError, setShipmentError] = useState<string | null>(null)
  const [shipmentSuccess, setShipmentSuccess] = useState<string | null>(null)

  const shipments = useQuery({
    queryKey: ['shipments', selectedId, appliedTracking, page],
    queryFn: () =>
      listShipments(selectedId!, {
        tracking_id: appliedTracking === '' ? undefined : appliedTracking,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
    enabled: !!selectedId,
    retry: false,
  })

  const orders = useQuery({
    queryKey: ['orders', selectedId],
    queryFn: () => listOrders(selectedId!, PAGE_SIZE, 0),
    enabled: !!selectedId,
    retry: false,
  })

  const accounts = useQuery({
    queryKey: ['courier-accounts', selectedId],
    queryFn: () => listCourierAccounts(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const orderRefById = useMemo(() => {
    const map = new Map<string, string>()
    for (const o of orders.data?.orders ?? []) map.set(o.order_id, o.merchant_order_ref)
    return map
  }, [orders.data])

  const createOrderMutation = useMutation({
    mutationFn: () => {
      const ref = orderRef.trim()
      if (ref === '') throw new Error('Enter the merchant order reference.')
      return createOrder(selectedId!, {
        merchant_order_ref: ref,
        // Blank stays unknown (NULL) — the order COD is only sent
        // when the merchant confirms it.
        order_cod_poisha: parseTakaToPoisha(orderCod),
      })
    },
    onSuccess: (order) => {
      setOrderError(null)
      setOrderSuccess(`Order ${order.merchant_order_ref} created.`)
      setOrderRef('')
      setOrderCod('')
      queryClient.invalidateQueries({ queryKey: ['orders', selectedId] })
    },
    onError: (err) => {
      setOrderSuccess(null)
      setOrderError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Could not create the order.',
      )
    },
  })

  const amountApplies = !AMOUNTLESS_OUTCOMES.includes(shipmentOutcome)

  const createShipmentMutation = useMutation({
    mutationFn: () => {
      if (shipmentOrderId === '') throw new Error('Choose an order for this shipment.')
      const tracking = shipmentTracking.trim()
      if (tracking === '') throw new Error('Enter the tracking id.')
      return createShipmentForOrder(selectedId!, shipmentOrderId, {
        tracking_id: tracking,
        outcome: shipmentOutcome,
        outcome_source: shipmentOutcomeSource,
        is_cod_bearing: shipmentIsCodBearing,
        // The backend rejects an amount for returned / cancelled /
        // in-transit / unknown, so it is sent only where it applies.
        amount_to_collect_poisha: amountApplies
          ? parseTakaToPoisha(shipmentAmount)
          : null,
        partial_amount_evidence_ref:
          shipmentOutcome === 'partially_delivered' && shipmentEvidenceRef.trim() !== ''
            ? shipmentEvidenceRef.trim()
            : null,
        merchant_courier_account_id:
          shipmentCourierAccountId === '' ? null : shipmentCourierAccountId,
        shipped_on: shipmentDate === '' ? null : shipmentDate,
      })
    },
    onSuccess: (shipment) => {
      setShipmentError(null)
      setShipmentSuccess(`Shipment ${shipment.tracking_id ?? 'created'} created.`)
      setShipmentTracking('')
      setShipmentAmount('')
      setShipmentEvidenceRef('')
      setShipmentDate('')
      queryClient.invalidateQueries({ queryKey: ['shipments', selectedId] })
    },
    onError: (err) => {
      setShipmentSuccess(null)
      setShipmentError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Could not create the shipment.',
      )
    },
  })

  const totalPages =
    shipments.data && shipments.data.total > 0
      ? Math.max(1, Math.ceil(shipments.data.total / PAGE_SIZE))
      : 1

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Shipments</h1>
        <p className="mt-1 text-sm text-slate-500">
          Every COD shipment for this workspace, with the expected collectible COD
          derived server-side from the confirmed shipment outcome. An order&apos;s
          COD is never silently assumed to be collectible on every shipment.
        </p>
      </div>

      <WorkspacePicker />

      <Card>
        <CardHeader>
          <CardTitle>Expected collectible COD — the outcome rule</CardTitle>
          <CardDescription>
            The expected collectible is decided by the confirmed shipment outcome,
            not by the order total:
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-2 text-sm text-slate-700">
            {SHIPMENT_OUTCOMES.map((o) => (
              <li key={o}>
                <span className="font-medium">
                  {o} · {OUTCOME_BANGLA[o]}:
                </span>{' '}
                {OUTCOME_COLLECTIBLE_RULE[o].en} {OUTCOME_COLLECTIBLE_RULE[o].bn}
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-slate-500">
            A shipment with no confirmed outcome, or a partial delivery with no
            documented partial amount, has an unknown expected collectible — shown as
            NULL, never as ৳0.00. A merchant-stated amount that differs from the
            order total is a split: it is recorded and flagged for review, never
            silently assumed. MVP rule: one active COD-bearing shipment per order.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Shipments ({shipments.data?.total ?? '…'})</CardTitle>
          <CardDescription>
            Real shipments from the backend, newest first. Filter by tracking id to
            find one shipment.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              setAppliedTracking(trackingFilter.trim())
              setPage(1)
            }}
            className="mb-4 flex flex-wrap items-end gap-3"
          >
            <div className="space-y-2">
              <Label htmlFor="shipments-tracking-filter">Find by tracking id</Label>
              <Input
                id="shipments-tracking-filter"
                type="text"
                placeholder="e.g. TRK-1001"
                value={trackingFilter}
                onChange={(e) => setTrackingFilter(e.target.value)}
              />
            </div>
            <Button type="submit">Filter</Button>
            {appliedTracking !== '' && (
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setTrackingFilter('')
                  setAppliedTracking('')
                  setPage(1)
                }}
              >
                Clear filter
              </Button>
            )}
          </form>

          {shipments.isPending && <p className="text-sm text-slate-500">Loading…</p>}
          {shipments.isError && (
            <p className="text-sm text-red-600">
              Could not load shipments from the backend — the endpoint may not be
              deployed yet.
            </p>
          )}
          {shipments.isSuccess && shipments.data.shipments.length === 0 && (
            <p className="text-sm text-slate-500">
              {appliedTracking !== ''
                ? `No shipments match tracking id “${appliedTracking}”.`
                : 'No shipments yet — create an order and add a shipment below.'}
            </p>
          )}
          {shipments.isSuccess && shipments.data.shipments.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] text-left text-sm">
                <thead>
                  <tr className="border-b text-slate-500">
                    <th className="py-2 pr-4 font-medium">Tracking</th>
                    <th className="py-2 pr-4 font-medium">Order</th>
                    <th className="py-2 pr-4 font-medium">Outcome</th>
                    <th className="py-2 pr-4 font-medium">Expected collectible</th>
                    <th className="py-2 pr-4 font-medium">Review</th>
                    <th className="py-2 pr-4 font-medium">COD-bearing</th>
                    <th className="py-2 font-medium">Shipped on</th>
                  </tr>
                </thead>
                <tbody>
                  {shipments.data.shipments.map((s) => (
                    <tr key={s.shipment_id} className="border-b align-top last:border-0">
                      <td className="py-2 pr-4">
                        <span className="font-mono text-xs">{s.tracking_id ?? '—'}</span>
                        {s.tracking_id_normalised &&
                          s.tracking_id_normalised !== s.tracking_id && (
                            <span className="block text-xs text-slate-400">
                              normalised: {s.tracking_id_normalised}
                            </span>
                          )}
                      </td>
                      <td className="py-2 pr-4 font-mono text-xs">
                        {orderRefById.get(s.order_id) ?? shortId(s.order_id)}
                      </td>
                      <td className="py-2 pr-4">
                        {s.outcome}
                        {OUTCOME_BANGLA[s.outcome as ShipmentOutcome] ? (
                          <span className="block text-xs text-slate-500">
                            {OUTCOME_BANGLA[s.outcome as ShipmentOutcome]}
                          </span>
                        ) : null}
                        {s.outcome_source && (
                          <span className="block text-xs text-slate-400">
                            {s.outcome_source}
                            {OUTCOME_SOURCE_BANGLA[s.outcome_source as OutcomeSource]
                              ? ` · ${OUTCOME_SOURCE_BANGLA[s.outcome_source as OutcomeSource]}`
                              : ''}
                          </span>
                        )}
                      </td>
                      <td className="py-2 pr-4">
                        <ExpectedCollectibleCell
                          poisha={s.expected_collectible_cod_poisha}
                          derived={s.expected_collectible_derived_poisha}
                        />
                      </td>
                      <td className="py-2 pr-4">
                        {s.needs_review ? (
                          <>
                            <Badge variant="warning">needs review · যাচাই প্রয়োজন</Badge>
                            {s.needs_review_reason && (
                              <span className="block text-xs text-slate-500">
                                {NEEDS_REVIEW_REASON[s.needs_review_reason].en}{' '}
                                {NEEDS_REVIEW_REASON[s.needs_review_reason].bn}
                              </span>
                            )}
                          </>
                        ) : (
                          <span className="text-xs text-slate-500">—</span>
                        )}
                      </td>
                      <td className="py-2 pr-4">
                        <Badge variant={s.is_cod_bearing ? 'info' : 'muted'}>
                          {s.is_cod_bearing ? 'COD-bearing' : 'not COD-bearing'}
                        </Badge>
                        {s.is_superseded && (
                          <span className="block text-xs text-slate-500">superseded</span>
                        )}
                      </td>
                      <td className="py-2">{s.shipped_on ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-4 flex items-center gap-3">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage(page - 1)}
                >
                  ← Previous
                </Button>
                <span className="text-sm text-slate-500">
                  Page {page} of {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages}
                  onClick={() => setPage(page + 1)}
                >
                  Next →
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Create an order</CardTitle>
          <CardDescription>
            The order&apos;s COD is the original order amount — leave it blank when
            it is not confirmed, and it stays unknown rather than being treated as
            zero. It is not the amount to collect on a shipment — that is decided
            per shipment, by its outcome.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              createOrderMutation.mutate()
            }}
            className="flex flex-wrap items-end gap-3"
          >
            <div className="space-y-2">
              <Label htmlFor="order-ref">Merchant order ref</Label>
              <Input
                id="order-ref"
                type="text"
                placeholder="e.g. ORD-1001"
                value={orderRef}
                onChange={(e) => setOrderRef(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="order-cod">Order COD ৳ (optional)</Label>
              <Input
                id="order-cod"
                type="text"
                inputMode="decimal"
                placeholder="e.g. 1250.50"
                value={orderCod}
                onChange={(e) => setOrderCod(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={createOrderMutation.isPending}>
              {createOrderMutation.isPending ? 'Creating order…' : 'Create order'}
            </Button>
          </form>
          {orderError && <p className="mt-3 text-sm text-red-600">{orderError}</p>}
          {orderSuccess && <p className="mt-3 text-sm text-emerald-700">{orderSuccess}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Add a shipment to an order</CardTitle>
          <CardDescription>
            One shipment per form submit. The amount to collect is only for delivered
            and partially delivered shipments — for a partial delivery it counts
            only together with an evidence reference. Leave amounts blank when they
            are not confirmed, and they stay unknown rather than being treated as
            zero.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              createShipmentMutation.mutate()
            }}
            className="flex flex-wrap items-end gap-3"
          >
            <div className="space-y-2">
              <Label htmlFor="shipment-order">Order</Label>
              <select
                id="shipment-order"
                value={shipmentOrderId}
                onChange={(e) => setShipmentOrderId(e.target.value)}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="">Choose an order…</option>
                {(orders.data?.orders ?? []).map((o) => (
                  <option key={o.order_id} value={o.order_id}>
                    {o.merchant_order_ref} · {formatTaka(o.order_cod_poisha)}
                  </option>
                ))}
              </select>
              {orders.isError && (
                <p className="text-xs text-red-600">Could not load orders.</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="shipment-tracking">Tracking id</Label>
              <Input
                id="shipment-tracking"
                type="text"
                placeholder="e.g. TRK-1001"
                value={shipmentTracking}
                onChange={(e) => setShipmentTracking(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="shipment-outcome">Outcome</Label>
              <select
                id="shipment-outcome"
                value={shipmentOutcome}
                onChange={(e) => setShipmentOutcome(e.target.value as ShipmentOutcome)}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                {SHIPMENT_OUTCOMES.map((o) => (
                  <option key={o} value={o}>
                    {o} · {OUTCOME_BANGLA[o]}
                  </option>
                ))}
              </select>
              <p className="text-xs text-slate-500">
                {OUTCOME_COLLECTIBLE_RULE[shipmentOutcome].en}
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="shipment-outcome-source">Outcome source</Label>
              <select
                id="shipment-outcome-source"
                value={shipmentOutcomeSource}
                onChange={(e) => setShipmentOutcomeSource(e.target.value as OutcomeSource)}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                {OUTCOME_SOURCES.map((o) => (
                  <option key={o} value={o}>
                    {o} · {OUTCOME_SOURCE_BANGLA[o]}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-2 pb-2">
              <input
                id="shipment-cod-bearing"
                type="checkbox"
                checked={shipmentIsCodBearing}
                onChange={(e) => setShipmentIsCodBearing(e.target.checked)}
                className="h-4 w-4"
              />
              <Label htmlFor="shipment-cod-bearing">COD-bearing shipment</Label>
              <span className="text-xs text-slate-500">
                MVP: one active COD-bearing shipment per order.
              </span>
            </div>
            <div className="space-y-2">
              <Label htmlFor="shipment-amount">Amount to collect ৳ (optional)</Label>
              <Input
                id="shipment-amount"
                type="text"
                inputMode="decimal"
                placeholder="e.g. 1250.50"
                value={shipmentAmount}
                onChange={(e) => setShipmentAmount(e.target.value)}
                disabled={!amountApplies}
              />
              {!amountApplies && (
                <p className="text-xs text-slate-500">
                  Not applicable for outcome “{shipmentOutcome}” — the backend would
                  reject it.
                </p>
              )}
            </div>
            {shipmentOutcome === 'partially_delivered' && (
              <div className="space-y-2">
                <Label htmlFor="shipment-evidence-ref">
                  Partial amount evidence ref (optional)
                </Label>
                <Input
                  id="shipment-evidence-ref"
                  type="text"
                  placeholder="e.g. courier message 12"
                  value={shipmentEvidenceRef}
                  onChange={(e) => setShipmentEvidenceRef(e.target.value)}
                />
                <p className="text-xs text-slate-500">
                  A partial amount counts only with an evidence reference — without
                  one, the expected collectible stays unknown (NULL).
                </p>
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="shipment-courier-account">Courier account (optional)</Label>
              <select
                id="shipment-courier-account"
                value={shipmentCourierAccountId}
                onChange={(e) => setShipmentCourierAccountId(e.target.value)}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="">No courier account linked</option>
                {(accounts.data ?? []).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.display_label ?? `${a.courier_code} (${shortId(a.id)})`}
                  </option>
                ))}
              </select>
              {accounts.isError && (
                <p className="text-xs text-red-600">Could not load courier accounts.</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="shipment-date">Shipped on (optional)</Label>
              <Input
                id="shipment-date"
                type="date"
                value={shipmentDate}
                onChange={(e) => setShipmentDate(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={createShipmentMutation.isPending}>
              {createShipmentMutation.isPending ? 'Adding shipment…' : 'Add shipment'}
            </Button>
          </form>
          {shipmentError && <p className="mt-3 text-sm text-red-600">{shipmentError}</p>}
          {shipmentSuccess && (
            <p className="mt-3 text-sm text-emerald-700">{shipmentSuccess}</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
