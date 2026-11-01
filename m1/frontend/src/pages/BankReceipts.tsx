import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  ApiError,
  addBankReceipt,
  listBankReceipts,
  listSettlementBatches,
} from '../lib/api'
import { formatTaka } from '../lib/demo'
import { parseRequiredTakaToPoisha } from '../lib/settlement'
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

/**
 * Bank receipts across the workspace. Bank-confirmed receipts are kept
 * separate from courier-reported payouts: a courier saying it paid an
 * amount is not the same as the bank confirming the money arrived. The
 * two are only compared on a settlement batch, and a difference is shown
 * for review — never automatically labelled a loss.
 *
 * Receipts are normally added from a settlement batch's detail page;
 * this page also offers a standalone form that requires choosing a batch.
 * No bank account number is ever asked for or stored.
 */
export function BankReceiptsPage() {
  const { selectedId } = useSelectedWorkspace()
  const queryClient = useQueryClient()

  const [batchId, setBatchId] = useState('')
  const [amount, setAmount] = useState('')
  const [receivedOn, setReceivedOn] = useState('')
  const [label, setLabel] = useState('')
  const [evidenceNote, setEvidenceNote] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)

  const receipts = useQuery({
    queryKey: ['bank-receipts', selectedId],
    queryFn: () => listBankReceipts(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const batches = useQuery({
    queryKey: ['settlement-batches', selectedId],
    queryFn: () => listSettlementBatches(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const createReceipt = useMutation({
    mutationFn: () => {
      if (!batchId) throw new Error('Choose a settlement batch for this receipt.')
      const amountPoisha = parseRequiredTakaToPoisha(amount)
      return addBankReceipt(selectedId!, batchId, {
        amount_poisha: amountPoisha,
        received_on: receivedOn === '' ? null : receivedOn,
        receipt_ref_label: label.trim() === '' ? null : label.trim(),
        evidence_note: evidenceNote.trim() === '' ? null : evidenceNote.trim(),
      })
    },
    onSuccess: () => {
      setCreateError(null)
      setAmount('')
      setReceivedOn('')
      setLabel('')
      setEvidenceNote('')
      queryClient.invalidateQueries({ queryKey: ['bank-receipts', selectedId] })
      queryClient.invalidateQueries({ queryKey: ['settlement-batch', selectedId, batchId] })
    },
    onError: (err) => {
      setCreateError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Could not add the receipt.',
      )
    },
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Bank Receipts</h1>
        <p className="mt-1 text-sm text-slate-500">
          Bank-confirmed receipts, kept separate from courier-reported payouts. A courier
          saying it paid an amount is not the same as the bank confirming the money
          arrived — the two are only compared on a settlement batch, and a difference is
          shown for review, never automatically labelled a loss.
        </p>
      </div>

      <WorkspacePicker />

      {receipts.isPending && <p className="text-sm text-slate-500">Loading…</p>}
      {receipts.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load bank receipts from the backend — the endpoint may not be
              deployed yet.
            </p>
          </CardContent>
        </Card>
      )}

      {receipts.isSuccess && (
        <Card>
          <CardHeader>
            <CardTitle>Receipts ({receipts.data.total})</CardTitle>
            <CardDescription>
              Every bank-confirmed receipt in this workspace, newest first. Each receipt
              links to its settlement batch, where it is allocated and compared.
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            {receipts.data.receipts.length === 0 && (
              <p className="text-sm text-slate-500">
                No bank receipts yet — add one from a settlement batch, or use the form
                below.
              </p>
            )}
            {receipts.data.receipts.length > 0 && (
              <table className="w-full min-w-[700px] text-left text-sm">
                <thead>
                  <tr className="border-b text-slate-500">
                    <th className="py-2 pr-4 font-medium">Receipt</th>
                    <th className="py-2 pr-4 font-medium">Amount</th>
                    <th className="py-2 pr-4 font-medium">Settlement batch</th>
                    <th className="py-2 pr-4 font-medium">Received on</th>
                    <th className="py-2 pr-4 font-medium">Label</th>
                    <th className="py-2 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {receipts.data.receipts.map((r) => (
                    <tr key={r.receipt_id} className="border-b last:border-0">
                      <td className="py-2 pr-4 font-mono text-xs">{shortId(r.receipt_id)}</td>
                      <td className="py-2 pr-4 font-medium">{formatTaka(r.amount_poisha)}</td>
                      <td className="py-2 pr-4">
                        {r.settlement_batch_id ? (
                          <Link
                            to={`/settlement-batches/${r.settlement_batch_id}`}
                            className="font-mono text-xs font-medium text-primary hover:underline"
                          >
                            {shortId(r.settlement_batch_id)}
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="py-2 pr-4">
                        {r.received_on ? r.received_on.slice(0, 10) : '—'}
                      </td>
                      <td className="py-2 pr-4">{r.receipt_ref_label ?? '—'}</td>
                      <td className="py-2">
                        <Badge variant={r.is_complete ? 'success' : 'warning'}>
                          {r.is_complete ? 'complete · সম্পূর্ণ' : 'incomplete · অসম্পূর্ণ'}
                        </Badge>
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
          <CardTitle>Add a receipt</CardTitle>
          <CardDescription>
            Record money the bank confirmed as received. A receipt always belongs to a
            settlement batch. No bank account number is asked for — only the amount, the
            date, a label, and an evidence note.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              createReceipt.mutate()
            }}
            className="flex flex-wrap items-end gap-3"
          >
            <div className="space-y-2">
              <Label htmlFor="receipt-batch">Settlement batch</Label>
              <select
                id="receipt-batch"
                value={batchId}
                onChange={(e) => setBatchId(e.target.value)}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="">Choose a settlement batch…</option>
                {(batches.data?.settlement_batches ?? []).map((b) => (
                  <option key={b.batch_id} value={b.batch_id}>
                    {shortId(b.batch_id)}
                    {b.period_start ? ` · ${b.period_start.slice(0, 10)}` : ''}
                    {b.period_end ? ` → ${b.period_end.slice(0, 10)}` : ''}
                  </option>
                ))}
              </select>
              {batches.isError && (
                <p className="text-xs text-red-600">Could not load settlement batches.</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-receipt-amount">Amount ৳</Label>
              <Input
                id="bank-receipt-amount"
                type="text"
                inputMode="decimal"
                placeholder="e.g. 1250.50"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-receipt-date">Received on (optional)</Label>
              <Input
                id="bank-receipt-date"
                type="date"
                value={receivedOn}
                onChange={(e) => setReceivedOn(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-receipt-label">Label (optional)</Label>
              <Input
                id="bank-receipt-label"
                type="text"
                placeholder="e.g. Bank transfer Jan"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="bank-receipt-evidence">Evidence note (optional)</Label>
              <Input
                id="bank-receipt-evidence"
                type="text"
                placeholder="e.g. Bank statement line 12"
                value={evidenceNote}
                onChange={(e) => setEvidenceNote(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={createReceipt.isPending}>
              {createReceipt.isPending ? 'Adding receipt…' : 'Add receipt'}
            </Button>
          </form>
          {createError && <p className="mt-3 text-sm text-red-600">{createError}</p>}
        </CardContent>
      </Card>
    </div>
  )
}
