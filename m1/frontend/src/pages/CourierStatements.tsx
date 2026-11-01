import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  getStatement,
  listImports,
  listStatements,
  type StatementLine,
  type StatementMatchStatus,
} from '../lib/api'
import { formatTaka } from '../lib/demo'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { Label } from '../components/ui/Label'
import { WorkspacePicker } from '../components/WorkspacePicker'
import { MoneyCell } from './ImportDetail'

const MATCH_STATUSES: StatementMatchStatus[] = [
  'strong_matched',
  'human_confirmed',
  'ambiguous',
  'duplicate',
  'unmatched',
]

/** Bangla explanation for each statement match status. */
const MATCH_STATUS_BANGLA: Record<StatementMatchStatus, string> = {
  strong_matched: 'দৃঢ়ভাবে মিলেছে',
  human_confirmed: 'মানুষের দ্বারা নিশ্চিত',
  ambiguous: 'অস্পষ্ট — যাচাই প্রয়োজন',
  duplicate: 'ডুপ্লিকেট',
  unmatched: 'মেলেনি',
}

const MATCH_VARIANT: Record<string, 'success' | 'warning' | 'danger' | 'muted' | 'info'> = {
  strong_matched: 'success',
  human_confirmed: 'success',
  ambiguous: 'warning',
  duplicate: 'danger',
  unmatched: 'muted',
}

function shortId(id: string | null | undefined): string {
  return id ? `${id.slice(0, 8)}…` : '—'
}

/** The courier label for a line: code, else the account's display label. */
function courierLabel(line: StatementLine): string {
  const account = line.courier_account
  if (!account) return '—'
  return (
    account.courier_code ??
    account.display_label ??
    shortId(account.merchant_courier_account_id)
  )
}

const PAGE_SIZE = 20

/**
 * Courier statements: one row per imported statement line, across all
 * import batches. Raw values are preserved verbatim; reported money is
 * shown with its per-field data-quality state (blank or invalid is
 * unknown, never a silent zero). Matching never uses amounts alone.
 */
export function CourierStatementsPage() {
  const { selectedId } = useSelectedWorkspace()
  const [batchFilter, setBatchFilter] = useState('')
  const [matchFilter, setMatchFilter] = useState('')
  const [page, setPage] = useState(1)
  const [selectedId_, setSelectedId_] = useState<string | null>(null)

  const batches = useQuery({
    queryKey: ['imports', selectedId],
    queryFn: () => listImports(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const statements = useQuery({
    queryKey: ['statements', selectedId, batchFilter, matchFilter, page],
    queryFn: () =>
      listStatements(selectedId!, {
        import_batch_id: batchFilter === '' ? undefined : batchFilter,
        match_status: matchFilter === '' ? undefined : matchFilter,
        page,
        page_size: PAGE_SIZE,
      }),
    enabled: !!selectedId,
    retry: false,
  })

  // The detail view fetches the single line in full (raw values
  // verbatim, linked shipment, courier account, import batch).
  const detail = useQuery({
    queryKey: ['statement', selectedId, selectedId_],
    queryFn: () => getStatement(selectedId!, selectedId_!),
    enabled: !!selectedId && selectedId_ !== null,
    retry: false,
  })

  const selected: StatementLine | null = detail.data ?? null

  function applyFilter(batch: string, match: string) {
    setBatchFilter(batch)
    setMatchFilter(match)
    setPage(1)
    setSelectedId_(null)
  }

  const totalPages =
    statements.data && statements.data.total > 0
      ? Math.max(1, Math.ceil(statements.data.total / PAGE_SIZE))
      : 1

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Courier Statements</h1>
        <p className="mt-1 text-sm text-slate-500">
          Courier-reported payout statements, one row per statement line, across all
          imports. Reported money is shown with its data-quality state — blank or
          invalid is unknown, never a silent zero — and matching never uses amounts
          alone.
        </p>
      </div>

      <WorkspacePicker />

      <Card>
        <CardHeader>
          <CardTitle>Filter</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="space-y-2">
            <Label htmlFor="statements-batch">Import batch</Label>
            <select
              id="statements-batch"
              value={batchFilter}
              onChange={(e) => applyFilter(e.target.value, matchFilter)}
              className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            >
              <option value="">All batches</option>
              {(batches.data ?? []).map((b) => (
                <option key={b.batch_id} value={b.batch_id}>
                  {shortId(b.batch_id)} · {b.status} · {b.row_count} rows
                </option>
              ))}
            </select>
            {batches.isError && (
              <p className="text-xs text-red-600">Could not load import batches.</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="statements-match">Match status</Label>
            <select
              id="statements-match"
              value={matchFilter}
              onChange={(e) => applyFilter(batchFilter, e.target.value)}
              className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            >
              <option value="">All statuses</option>
              {MATCH_STATUSES.map((m) => (
                <option key={m} value={m}>
                  {m} · {MATCH_STATUS_BANGLA[m]}
                </option>
              ))}
            </select>
          </div>
        </CardContent>
      </Card>

      {statements.isPending && <p className="text-sm text-slate-500">Loading…</p>}
      {statements.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load courier statements from the backend — the endpoint may not
              be deployed yet.
            </p>
          </CardContent>
        </Card>
      )}

      {statements.isSuccess && (
        <Card>
          <CardHeader>
            <CardTitle>
              Statement lines ({statements.data.total})
            </CardTitle>
            <CardDescription>
              Page {statements.data.page} of {totalPages}. Select a row to see its raw
              values verbatim and its linked shipment.
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            {statements.data.statements.length === 0 && (
              <p className="text-sm text-slate-500">
                No statement lines match these filters yet.
              </p>
            )}
            {statements.data.statements.length > 0 && (
              <table className="w-full min-w-[900px] text-left text-sm">
                <thead>
                  <tr className="border-b text-slate-500">
                    <th className="py-2 pr-4 font-medium">Tracking</th>
                    <th className="py-2 pr-4 font-medium">Order ref</th>
                    <th className="py-2 pr-4 font-medium">Reported status</th>
                    <th className="py-2 pr-4 font-medium">Collected</th>
                    <th className="py-2 pr-4 font-medium">Payout</th>
                    <th className="py-2 pr-4 font-medium">Match</th>
                    <th className="py-2 pr-4 font-medium">Import batch</th>
                    <th className="py-2 font-medium">Courier</th>
                  </tr>
                </thead>
                <tbody>
                  {statements.data.statements.map((line) => (
                    <tr
                      key={line.statement_line_id}
                      className={`cursor-pointer border-b last:border-0 hover:bg-slate-50 ${
                        selectedId_ === line.statement_line_id ? 'bg-indigo-50' : ''
                      }`}
                      onClick={() =>
                        setSelectedId_(
                          selectedId_ === line.statement_line_id
                            ? null
                            : line.statement_line_id,
                        )
                      }
                    >
                      <td className="py-2 pr-4">
                        <span className="font-mono text-xs">
                          {line.tracking_id_normalised ?? line.tracking_id_raw ?? '—'}
                        </span>
                        {line.tracking_id_raw &&
                          line.tracking_id_normalised !== line.tracking_id_raw && (
                            <span className="block text-xs text-slate-400">
                              raw: {line.tracking_id_raw}
                            </span>
                          )}
                      </td>
                      <td className="py-2 pr-4 font-mono text-xs">
                        {line.merchant_order_ref_raw ?? '—'}
                      </td>
                      <td className="py-2 pr-4">{line.reported_status_raw ?? '—'}</td>
                      <td className="py-2 pr-4">
                        {line.reported_collected_state ? (
                          <MoneyCell
                            poisha={line.reported_collected_poisha}
                            state={line.reported_collected_state}
                          />
                        ) : (
                          <span className="font-medium">
                            {formatTaka(line.reported_collected_poisha)}
                          </span>
                        )}
                      </td>
                      <td className="py-2 pr-4">
                        {line.reported_payout_state ? (
                          <MoneyCell
                            poisha={line.reported_payout_poisha}
                            state={line.reported_payout_state}
                          />
                        ) : (
                          <span className="font-medium">
                            {formatTaka(line.reported_payout_poisha)}
                          </span>
                        )}
                      </td>
                      <td className="py-2 pr-4">
                        <Badge variant={MATCH_VARIANT[line.match_status] ?? 'muted'}>
                          {line.match_status}
                          {MATCH_STATUS_BANGLA[line.match_status as StatementMatchStatus]
                            ? ` · ${MATCH_STATUS_BANGLA[line.match_status as StatementMatchStatus]}`
                            : ''}
                        </Badge>
                        <span className="block text-xs text-slate-500">
                          parse: {line.parse_status ?? '—'}
                        </span>
                      </td>
                      <td className="py-2 pr-4">
                        <Link
                          to={`/imports/${line.import_batch_id}`}
                          className="font-mono text-xs font-medium text-primary hover:underline"
                          onClick={(e) => e.stopPropagation()}
                        >
                          {shortId(line.import_batch_id)}
                        </Link>
                      </td>
                      <td className="py-2">{courierLabel(line)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
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
          </CardContent>
        </Card>
      )}

      {selectedId_ !== null && detail.isPending && (
        <p className="text-sm text-slate-500">Loading statement line…</p>
      )}
      {selectedId_ !== null && detail.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load this statement line from the backend.
            </p>
          </CardContent>
        </Card>
      )}

      {selected && (
        <Card>
          <CardHeader>
            <CardTitle>Statement line detail</CardTitle>
            <CardDescription>
              <span className="font-mono text-xs">{selected.statement_line_id}</span>
              {selected.row_number !== null && (
                <span className="text-xs"> · row {selected.row_number}</span>
              )}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4 text-sm lg:grid-cols-4">
              <div>
                <p className="text-slate-500">Parse status</p>
                <p className="mt-1 font-medium">{selected.parse_status ?? '—'}</p>
              </div>
              <div>
                <p className="text-slate-500">Match status</p>
                <p className="mt-1">
                  <Badge variant={MATCH_VARIANT[selected.match_status] ?? 'muted'}>
                    {selected.match_status}
                  </Badge>
                </p>
              </div>
              <div>
                <p className="text-slate-500">Linked shipment</p>
                <p className="mt-1 font-medium">
                  {selected.shipment ? (
                    <>
                      <span className="font-mono text-xs">
                        {selected.shipment.tracking_id_normalised ??
                          selected.shipment.tracking_id_raw ??
                          shortId(selected.shipment.shipment_id)}
                      </span>
                      <span className="block text-xs font-normal text-slate-500">
                        outcome: {selected.shipment.outcome} · expected collectible:{' '}
                        {formatTaka(selected.shipment.expected_collectible_cod_poisha)}
                        {selected.shipment.is_cod_bearing ? ' · COD-bearing' : ''}
                      </span>
                    </>
                  ) : (
                    'No shipment linked — needs human confirmation.'
                  )}
                </p>
              </div>
              <div>
                <p className="text-slate-500">Statement date</p>
                <p className="mt-1 font-medium">{selected.statement_date ?? '—'}</p>
              </div>
            </div>

            <div>
              <p className="text-sm font-medium text-slate-700">Reported money</p>
              <div className="mt-2 grid grid-cols-2 gap-4 text-sm lg:grid-cols-3">
                {(
                  [
                    ['Collected', selected.reported_collected_poisha, selected.reported_collected_state],
                    ['Payout', selected.reported_payout_poisha, selected.reported_payout_state],
                    ['Delivery fee', selected.reported_delivery_fee_poisha, selected.reported_delivery_fee_state],
                    ['COD fee', selected.reported_cod_fee_poisha, selected.reported_cod_fee_state],
                    ['Return fee', selected.reported_return_fee_poisha, selected.reported_return_fee_state],
                    ['Other fee', selected.reported_other_fee_poisha, selected.reported_other_fee_state],
                  ] as const
                ).map(([label, poisha, state]) => (
                  <div key={label}>
                    <p className="text-slate-500">{label}</p>
                    <p className="mt-1">
                      {state ? (
                        <MoneyCell poisha={poisha} state={state} />
                      ) : (
                        <span className="font-medium">{formatTaka(poisha)}</span>
                      )}
                    </p>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <p className="text-sm font-medium text-slate-700">Raw values (verbatim)</p>
              <p className="mt-1 text-xs text-slate-500">
                The exact values as read from the file — nothing normalised or dropped.
              </p>
              <code className="mt-2 block break-all rounded bg-slate-100 px-2 py-1 text-xs">
                {selected.raw_values ? JSON.stringify(selected.raw_values) : '—'}
              </code>
            </div>

            <p className="text-xs text-slate-500">
              Import batch:{' '}
              <Link
                to={`/imports/${selected.import_batch_id}`}
                className="font-mono font-medium text-primary hover:underline"
              >
                {shortId(selected.import_batch_id)}
              </Link>
              {selected.import_batch && (
                <>
                  {' '}
                  · {selected.import_batch.status} · {selected.import_batch.row_count}{' '}
                  rows ({selected.import_batch.parsed_row_count} parsed,{' '}
                  {selected.import_batch.error_row_count} errors)
                </>
              )}
              {' · '}Courier account:{' '}
              {selected.courier_account ? (
                <span className="font-mono">
                  {selected.courier_account.display_label ??
                    selected.courier_account.courier_code ??
                    shortId(selected.courier_account.merchant_courier_account_id)}
                  {selected.courier_account.merchant_account_ref &&
                    ` (${selected.courier_account.merchant_account_ref})`}
                </span>
              ) : (
                '—'
              )}
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
