import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  ApiError,
  addBankReceipt,
  calculateVariance,
  completeBankReceipt,
  createAllocation,
  createSettlementBatch,
  getSettlementBatch,
  listCourierAccounts,
  listRuns,
  listSettlementBatches,
  reverseAllocation,
  type SettlementBatch,
} from '../lib/api'
import { formatTaka } from '../lib/demo'
import {
  ALLOCATION_STATUS_BANGLA,
  BANK_STATUS_BANGLA,
  VARIANCE_STATUS_REASON,
  parseRequiredTakaToPoisha,
  parseTakaToPoisha,
  validateExpectedPayout,
} from '../lib/settlement'
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

function formatDate(value: string | null | undefined): string {
  return value ? value.slice(0, 10) : '—'
}

function periodLabel(batch: SettlementBatch): string {
  const start = formatDate(batch.period_start)
  const end = formatDate(batch.period_end)
  if (start === '—' && end === '—') return '—'
  return `${start} → ${end}`
}

function courierLabel(accounts: { id: string; courier_code: string; display_label: string | null }[], id: string): string {
  const account = accounts.find((a) => a.id === id)
  if (!account) return shortId(id)
  return account.display_label ?? `${account.courier_code} (${shortId(id)})`
}

/**
 * Settlement batches: groups of statement lines the courier settled
 * together, compared against bank-confirmed receipts. The current bank
 * variance is shown ONLY when the backend returns a non-null value;
 * when it is null the backend's variance_status reason is shown instead
 * — never a stale or invented number. A difference is shown for review,
 * never automatically labelled a loss.
 */
export function SettlementBatchesPage() {
  const { selectedId } = useSelectedWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [courierAccountId, setCourierAccountId] = useState('')
  const [runId, setRunId] = useState('')
  const [periodStart, setPeriodStart] = useState('')
  const [periodEnd, setPeriodEnd] = useState('')
  const [expectedPayout, setExpectedPayout] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)

  const batches = useQuery({
    queryKey: ['settlement-batches', selectedId],
    queryFn: () => listSettlementBatches(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const accounts = useQuery({
    queryKey: ['courier-accounts', selectedId],
    queryFn: () => listCourierAccounts(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const runs = useQuery({
    queryKey: ['runs', selectedId, 0],
    queryFn: () => listRuns(selectedId!, 20, 0),
    enabled: !!selectedId,
    retry: false,
  })

  const createBatch = useMutation({
    mutationFn: () => {
      if (!courierAccountId) {
        throw new Error('Choose a courier account for this batch.')
      }
      const payoutError = validateExpectedPayout(expectedPayout)
      if (payoutError) throw new Error(payoutError)
      return createSettlementBatch(selectedId!, {
        merchant_courier_account_id: courierAccountId,
        run_id: runId === '' ? null : runId,
        period_start: periodStart === '' ? null : periodStart,
        period_end: periodEnd === '' ? null : periodEnd,
        expected_batch_payout_poisha: parseTakaToPoisha(expectedPayout),
      })
    },
    onSuccess: (batch) => {
      setCreateError(null)
      queryClient.invalidateQueries({ queryKey: ['settlement-batches', selectedId] })
      navigate(`/settlement-batches/${batch.batch_id}`)
    },
    onError: (err) => {
      setCreateError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Could not create the batch.',
      )
    },
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Settlement Batches</h1>
        <p className="mt-1 text-sm text-slate-500">
          Groups of statement lines settled together by the courier, compared against
          bank-confirmed receipts. A difference between the courier&apos;s expected payout
          and the bank-confirmed total is shown for review — never automatically labelled
          a loss.
        </p>
      </div>

      <WorkspacePicker />

      {batches.isPending && <p className="text-sm text-slate-500">Loading…</p>}
      {batches.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load settlement batches from the backend — the endpoint may not be
              deployed yet.
            </p>
          </CardContent>
        </Card>
      )}

      {batches.isSuccess && (
        <Card>
          <CardHeader>
            <CardTitle>Batches ({batches.data.total})</CardTitle>
            <CardDescription>
              Expected payout is the courier-reported figure; allocated total is what has
              been allocated from bank-confirmed receipts. Current variance is shown only
              when the backend has calculated one.
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            {batches.data.settlement_batches.length === 0 && (
              <p className="text-sm text-slate-500">No settlement batches yet.</p>
            )}
            {batches.data.settlement_batches.length > 0 && (
              <table className="w-full min-w-[900px] text-left text-sm">
                <thead>
                  <tr className="border-b text-slate-500">
                    <th className="py-2 pr-4 font-medium">Batch</th>
                    <th className="py-2 pr-4 font-medium">Period</th>
                    <th className="py-2 pr-4 font-medium">Courier account</th>
                    <th className="py-2 pr-4 font-medium">Expected payout</th>
                    <th className="py-2 pr-4 font-medium">Allocated total</th>
                    <th className="py-2 pr-4 font-medium">Allocation status</th>
                    <th className="py-2 pr-4 font-medium">Bank status</th>
                    <th className="py-2 font-medium">Current variance</th>
                  </tr>
                </thead>
                <tbody>
                  {batches.data.settlement_batches.map((b) => (
                    <tr key={b.batch_id} className="border-b last:border-0">
                      <td className="py-2 pr-4">
                        <Link
                          to={`/settlement-batches/${b.batch_id}`}
                          className="font-mono text-xs font-medium text-primary hover:underline"
                        >
                          {shortId(b.batch_id)}
                        </Link>
                        {b.run_id && (
                          <span className="block text-xs text-slate-400">
                            run {shortId(b.run_id)}
                          </span>
                        )}
                      </td>
                      <td className="py-2 pr-4">{periodLabel(b)}</td>
                      <td className="py-2 pr-4">
                        {courierLabel(accounts.data ?? [], b.merchant_courier_account_id)}
                      </td>
                      <td className="py-2 pr-4">{formatTaka(b.expected_batch_payout_poisha)}</td>
                      <td className="py-2 pr-4">{formatTaka(b.allocated_total_poisha)}</td>
                      <td className="py-2 pr-4">
                        <Badge variant={b.allocation_status === 'fully_allocated' ? 'success' : b.allocation_status === 'partially_allocated' ? 'warning' : 'muted'}>
                          {b.allocation_status} · {ALLOCATION_STATUS_BANGLA[b.allocation_status]}
                        </Badge>
                      </td>
                      <td className="py-2 pr-4">
                        <Badge variant={b.bank_status === 'fully_reconciled' ? 'success' : b.bank_status === 'no_receipt' ? 'muted' : 'warning'}>
                          {b.bank_status} · {BANK_STATUS_BANGLA[b.bank_status]}
                        </Badge>
                      </td>
                      <td className="py-2">
                        {b.current_bank_variance_poisha !== null ? (
                          <span className="font-mono text-xs font-medium">
                            {formatTaka(b.current_bank_variance_poisha)}
                          </span>
                        ) : (
                          <span className="text-xs text-slate-500">not calculated yet</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Create a settlement batch</CardTitle>
          <CardDescription>
            Group statement lines the courier settled together. The expected payout is
            optional — leave it blank when the courier has not confirmed one, and it
            stays unknown rather than being treated as zero.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              createBatch.mutate()
            }}
            className="flex flex-wrap items-end gap-3"
          >
            <div className="space-y-2">
              <Label htmlFor="batch-courier-account">Courier account</Label>
              <select
                id="batch-courier-account"
                value={courierAccountId}
                onChange={(e) => setCourierAccountId(e.target.value)}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="">Choose a courier account…</option>
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
              <Label htmlFor="batch-run">Reconciliation run (optional)</Label>
              <select
                id="batch-run"
                value={runId}
                onChange={(e) => setRunId(e.target.value)}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="">No run linked</option>
                {(runs.data?.runs ?? []).map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {shortId(r.run_id)} · {r.status}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="batch-period-start">Period start (optional)</Label>
              <Input
                id="batch-period-start"
                type="date"
                value={periodStart}
                onChange={(e) => setPeriodStart(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="batch-period-end">Period end (optional)</Label>
              <Input
                id="batch-period-end"
                type="date"
                value={periodEnd}
                onChange={(e) => setPeriodEnd(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="batch-expected-payout">Expected payout ৳ (optional)</Label>
              <Input
                id="batch-expected-payout"
                type="text"
                inputMode="decimal"
                placeholder="e.g. 1250.50"
                value={expectedPayout}
                onChange={(e) => setExpectedPayout(e.target.value)}
              />
              <p className="text-xs text-slate-500">
                Must be 0 or more when supplied.
              </p>
            </div>
            <Button type="submit" disabled={createBatch.isPending}>
              {createBatch.isPending ? 'Creating batch…' : 'Create batch'}
            </Button>
          </form>
          {createError && <p className="mt-3 text-sm text-red-600">{createError}</p>}
        </CardContent>
      </Card>
    </div>
  )
}

/**
 * Batch detail: receipts, allocations with net total, variance
 * calculation history, and the current variance with its status reason.
 * Receipts are added here — no bank account number is ever asked for.
 */
export function SettlementBatchDetailPage() {
  const { batchId } = useParams<{ batchId: string }>()
  const { selectedId } = useSelectedWorkspace()
  const queryClient = useQueryClient()

  const [receiptAmount, setReceiptAmount] = useState('')
  const [receiptDate, setReceiptDate] = useState('')
  const [receiptLabel, setReceiptLabel] = useState('')
  const [receiptEvidence, setReceiptEvidence] = useState('')
  const [receiptError, setReceiptError] = useState<string | null>(null)

  const [allocationReceiptId, setAllocationReceiptId] = useState('')
  const [allocationAmount, setAllocationAmount] = useState('')
  const [allocationError, setAllocationError] = useState<string | null>(null)

  const [calcError, setCalcError] = useState<string | null>(null)
  const [calcResult, setCalcResult] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const detail = useQuery({
    queryKey: ['settlement-batch', selectedId, batchId],
    queryFn: () => getSettlementBatch(selectedId!, batchId!),
    enabled: !!selectedId && !!batchId,
    retry: false,
  })

  function refresh() {
    queryClient.invalidateQueries({ queryKey: ['settlement-batch', selectedId, batchId] })
    queryClient.invalidateQueries({ queryKey: ['settlement-batches', selectedId] })
  }

  function mutationError(err: unknown): string {
    return err instanceof ApiError
      ? err.message
      : err instanceof Error
        ? err.message
        : 'That did not work — please try again.'
  }

  const addReceipt = useMutation({
    mutationFn: () => {
      const amountPoisha = parseRequiredTakaToPoisha(receiptAmount)
      return addBankReceipt(selectedId!, batchId!, {
        amount_poisha: amountPoisha,
        received_on: receiptDate === '' ? null : receiptDate,
        receipt_ref_label: receiptLabel.trim() === '' ? null : receiptLabel.trim(),
        evidence_note: receiptEvidence.trim() === '' ? null : receiptEvidence.trim(),
      })
    },
    onSuccess: () => {
      setReceiptError(null)
      setReceiptAmount('')
      setReceiptDate('')
      setReceiptLabel('')
      setReceiptEvidence('')
      refresh()
    },
    onError: (err) => setReceiptError(mutationError(err)),
  })

  const markComplete = useMutation({
    mutationFn: (receiptId: string) => completeBankReceipt(selectedId!, batchId!, receiptId),
    onSuccess: () => {
      setActionError(null)
      refresh()
    },
    onError: (err) => setActionError(mutationError(err)),
  })

  const allocate = useMutation({
    mutationFn: () => {
      if (!allocationReceiptId) throw new Error('Choose a receipt to allocate from.')
      const allocatedPoisha = parseRequiredTakaToPoisha(allocationAmount)
      return createAllocation(selectedId!, batchId!, {
        bank_receipt_id: allocationReceiptId,
        allocated_poisha: allocatedPoisha,
      })
    },
    onSuccess: () => {
      setAllocationError(null)
      setAllocationAmount('')
      refresh()
    },
    onError: (err) => setAllocationError(mutationError(err)),
  })

  const reverse = useMutation({
    mutationFn: (allocationId: string) => reverseAllocation(selectedId!, batchId!, allocationId),
    onSuccess: () => {
      setActionError(null)
      refresh()
    },
    onError: (err) => setActionError(mutationError(err)),
  })

  const calculate = useMutation({
    mutationFn: () => calculateVariance(selectedId!, batchId!),
    onSuccess: (result) => {
      setCalcError(null)
      setCalcResult(
        `Calculation #${result.calculation_seq}: expected ${formatTaka(result.expected_batch_payout_poisha)}, ` +
          `bank-confirmed allocated ${formatTaka(result.allocated_bank_confirmed_total_poisha)}, ` +
          `variance ${formatTaka(result.bank_variance_poisha)}.`,
      )
      refresh()
    },
    onError: (err) => {
      // A 422 carries the backend's reason verbatim — show it as-is.
      setCalcResult(null)
      setCalcError(mutationError(err))
    },
  })

  // GET /settlement-batches/{id} returns the FLAT batch dict — the
  // batch's own fields at top level, plus receipts / allocations /
  // net_allocated_total_poisha / variance history (see lib/api.ts).
  const batch = detail.data ?? null
  const varianceReason = detail.data ? VARIANCE_STATUS_REASON[detail.data.variance_status] : null

  return (
    <div className="space-y-6">
      <div>
        <Link to="/settlement-batches" className="text-sm font-medium text-primary hover:underline">
          ← Back to Settlement Batches
        </Link>
        <h1 className="mt-2 text-2xl font-bold text-slate-900">Settlement batch</h1>
        <p className="mt-1 font-mono text-xs text-slate-500">Batch {batchId}</p>
      </div>

      <WorkspacePicker />

      {detail.isPending && <p className="text-sm text-slate-500">Loading…</p>}
      {detail.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load this batch (it may belong to another workspace, or the backend
              settlement endpoints may not be deployed yet).
            </p>
          </CardContent>
        </Card>
      )}

      {detail.isSuccess && batch && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Period</p>
                <p className="mt-1 font-medium">{periodLabel(batch)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Expected payout</p>
                <p className="mt-1 text-xl font-bold">{formatTaka(batch.expected_batch_payout_poisha)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Allocated total</p>
                <p className="mt-1 text-xl font-bold">{formatTaka(batch.allocated_total_poisha)}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Net allocated</p>
                <p className="mt-1 text-xl font-bold">{formatTaka(detail.data.net_allocated_total_poisha)}</p>
                <p className="mt-1 text-xs text-slate-500">Allocations minus reversals.</p>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Status</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              <Badge variant={batch.allocation_status === 'fully_allocated' ? 'success' : batch.allocation_status === 'partially_allocated' ? 'warning' : 'muted'}>
                {batch.allocation_status} · {ALLOCATION_STATUS_BANGLA[batch.allocation_status]}
              </Badge>
              <Badge variant={batch.bank_status === 'fully_reconciled' ? 'success' : batch.bank_status === 'no_receipt' ? 'muted' : 'warning'}>
                {batch.bank_status} · {BANK_STATUS_BANGLA[batch.bank_status]}
              </Badge>
              {batch.run_id && (
                <span className="text-sm text-slate-500">
                  Linked run:{' '}
                  <Link to={`/runs/${batch.run_id}`} className="font-mono text-xs font-medium text-primary hover:underline">
                    {shortId(batch.run_id)}
                  </Link>
                </span>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Current bank variance</CardTitle>
              <CardDescription>
                Expected payout minus the bank-confirmed allocated total, as last calculated
                by the backend. A difference is shown for review — never automatically
                labelled a loss.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div data-testid="current-variance">
              {detail.data.current_bank_variance_poisha !== null ? (
                <>
                  <p className="text-2xl font-bold">
                    {formatTaka(detail.data.current_bank_variance_poisha)}
                  </p>
                  {varianceReason && (
                    <p className="mt-1 text-sm text-slate-500">
                      {varianceReason.en} {varianceReason.bn}
                    </p>
                  )}
                </>
              ) : (
                <p className="text-sm text-slate-600">
                  {varianceReason ? `${varianceReason.en} ${varianceReason.bn}` : 'No variance calculated yet.'}
                </p>
              )}
              </div>
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <Button
                  type="button"
                  disabled={calculate.isPending}
                  onClick={() => calculate.mutate()}
                >
                  {calculate.isPending ? 'Calculating…' : 'Calculate variance'}
                </Button>
                {calcResult && <p className="text-sm text-emerald-700">{calcResult}</p>}
                {calcError && <p className="text-sm text-red-600">{calcError}</p>}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Bank receipts ({detail.data.receipts.length})</CardTitle>
              <CardDescription>
                Bank-confirmed money received for this batch, kept separate from
                courier-reported payouts. No bank account number is ever asked for or
                stored — only the amount, the date, a label, and an evidence note.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {actionError && <p className="mb-3 text-sm text-red-600">{actionError}</p>}
              {detail.data.receipts.length === 0 && (
                <p className="text-sm text-slate-500">No receipts yet for this batch.</p>
              )}
              {detail.data.receipts.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[700px] text-left text-sm">
                    <thead>
                      <tr className="border-b text-slate-500">
                        <th className="py-2 pr-4 font-medium">Receipt</th>
                        <th className="py-2 pr-4 font-medium">Amount</th>
                        <th className="py-2 pr-4 font-medium">Received on</th>
                        <th className="py-2 pr-4 font-medium">Label</th>
                        <th className="py-2 pr-4 font-medium">Status</th>
                        <th className="py-2 font-medium">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.data.receipts.map((r) => (
                        <tr key={r.receipt_id} className="border-b last:border-0">
                          <td className="py-2 pr-4 font-mono text-xs">{shortId(r.receipt_id)}</td>
                          <td className="py-2 pr-4 font-medium">{formatTaka(r.amount_poisha)}</td>
                          <td className="py-2 pr-4">{formatDate(r.received_on)}</td>
                          <td className="py-2 pr-4">{r.receipt_ref_label ?? '—'}</td>
                          <td className="py-2 pr-4">
                            <Badge variant={r.is_complete ? 'success' : 'warning'}>
                              {r.is_complete ? 'complete · সম্পূর্ণ' : 'incomplete · অসম্পূর্ণ'}
                            </Badge>
                          </td>
                          <td className="py-2">
                            {!r.is_complete && (
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={markComplete.isPending}
                                onClick={() => markComplete.mutate(r.receipt_id)}
                              >
                                Mark complete
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  addReceipt.mutate()
                }}
                className="mt-6 flex flex-wrap items-end gap-3 border-t pt-4"
              >
                <div className="space-y-2">
                  <Label htmlFor="receipt-amount">Amount ৳</Label>
                  <Input
                    id="receipt-amount"
                    type="text"
                    inputMode="decimal"
                    placeholder="e.g. 1250.50"
                    value={receiptAmount}
                    onChange={(e) => setReceiptAmount(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="receipt-date">Received on (optional)</Label>
                  <Input
                    id="receipt-date"
                    type="date"
                    value={receiptDate}
                    onChange={(e) => setReceiptDate(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="receipt-label">Label (optional)</Label>
                  <Input
                    id="receipt-label"
                    type="text"
                    placeholder="e.g. Bank transfer Jan"
                    value={receiptLabel}
                    onChange={(e) => setReceiptLabel(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="receipt-evidence">Evidence note (optional)</Label>
                  <Input
                    id="receipt-evidence"
                    type="text"
                    placeholder="e.g. Bank statement line 12"
                    value={receiptEvidence}
                    onChange={(e) => setReceiptEvidence(e.target.value)}
                  />
                </div>
                <Button type="submit" disabled={addReceipt.isPending}>
                  {addReceipt.isPending ? 'Adding receipt…' : 'Add receipt'}
                </Button>
              </form>
              {receiptError && <p className="mt-3 text-sm text-red-600">{receiptError}</p>}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Allocations ({detail.data.allocations.length})</CardTitle>
              <CardDescription>
                How bank-confirmed receipts have been allocated to this batch. A reversal
                undoes an allocation — the original row is kept, never deleted.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {detail.data.allocations.length === 0 && (
                <p className="text-sm text-slate-500">No allocations yet for this batch.</p>
              )}
              {detail.data.allocations.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[700px] text-left text-sm">
                    <thead>
                      <tr className="border-b text-slate-500">
                        <th className="py-2 pr-4 font-medium">Allocation</th>
                        <th className="py-2 pr-4 font-medium">Receipt</th>
                        <th className="py-2 pr-4 font-medium">Amount</th>
                        <th className="py-2 pr-4 font-medium">Kind</th>
                        <th className="py-2 pr-4 font-medium">Reverses</th>
                        <th className="py-2 font-medium">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.data.allocations.map((a) => (
                        <tr key={a.allocation_id} className="border-b last:border-0">
                          <td className="py-2 pr-4 font-mono text-xs">{shortId(a.allocation_id)}</td>
                          <td className="py-2 pr-4 font-mono text-xs">{shortId(a.bank_receipt_id)}</td>
                          <td className="py-2 pr-4 font-medium">{formatTaka(a.allocated_poisha)}</td>
                          <td className="py-2 pr-4">
                            <Badge variant={a.kind === 'reversal' ? 'muted' : 'info'}>{a.kind}</Badge>
                          </td>
                          <td className="py-2 pr-4 font-mono text-xs">
                            {a.reverses_allocation_id ? shortId(a.reverses_allocation_id) : '—'}
                          </td>
                          <td className="py-2">
                            {a.kind === 'allocation' && (
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={reverse.isPending}
                                onClick={() => reverse.mutate(a.allocation_id)}
                              >
                                Reverse
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  allocate.mutate()
                }}
                className="mt-6 flex flex-wrap items-end gap-3 border-t pt-4"
              >
                <div className="space-y-2">
                  <Label htmlFor="allocation-receipt">Receipt</Label>
                  <select
                    id="allocation-receipt"
                    value={allocationReceiptId}
                    onChange={(e) => setAllocationReceiptId(e.target.value)}
                    className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                  >
                    <option value="">Choose a receipt…</option>
                    {detail.data.receipts.map((r) => (
                      <option key={r.receipt_id} value={r.receipt_id}>
                        {formatTaka(r.amount_poisha)}
                        {r.receipt_ref_label ? ` · ${r.receipt_ref_label}` : ''} ·{' '}
                        {r.is_complete ? 'complete' : 'incomplete'}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="allocation-amount">Amount ৳</Label>
                  <Input
                    id="allocation-amount"
                    type="text"
                    inputMode="decimal"
                    placeholder="e.g. 1250.50"
                    value={allocationAmount}
                    onChange={(e) => setAllocationAmount(e.target.value)}
                  />
                </div>
                <Button type="submit" disabled={allocate.isPending}>
                  {allocate.isPending ? 'Allocating…' : 'Allocate'}
                </Button>
              </form>
              {allocationError && <p className="mt-3 text-sm text-red-600">{allocationError}</p>}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Variance calculation history ({detail.data.variance_calculations.length})</CardTitle>
              <CardDescription>
                Every variance calculation the backend has stored for this batch, newest
                first — none hides another. The current variance above is the latest one
                that still applies.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {detail.data.variance_calculations.length === 0 && (
                <p className="text-sm text-slate-500">No variance calculated yet for this batch.</p>
              )}
              {detail.data.variance_calculations.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[700px] text-left text-sm">
                    <thead>
                      <tr className="border-b text-slate-500">
                        <th className="py-2 pr-4 font-medium">#</th>
                        <th className="py-2 pr-4 font-medium">Expected payout</th>
                        <th className="py-2 pr-4 font-medium">Bank-confirmed allocated</th>
                        <th className="py-2 pr-4 font-medium">Variance</th>
                        <th className="py-2 font-medium">Calculated at</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.data.variance_calculations.map((c) => (
                        <tr key={c.calculation_seq} className="border-b last:border-0">
                          <td className="py-2 pr-4 font-mono text-xs">{c.calculation_seq}</td>
                          <td className="py-2 pr-4">{formatTaka(c.expected_batch_payout_poisha)}</td>
                          <td className="py-2 pr-4">{formatTaka(c.allocated_bank_confirmed_total_poisha)}</td>
                          <td className="py-2 pr-4 font-medium">{formatTaka(c.bank_variance_poisha)}</td>
                          <td className="py-2">
                            {c.computed_at ? c.computed_at.slice(0, 19).replace('T', ' ') : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
