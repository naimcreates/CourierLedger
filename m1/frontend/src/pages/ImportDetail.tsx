import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  ApiError,
  createRunForImport,
  getImportDetail,
  listBatchRuns,
  type MoneyDqState,
} from '../lib/api'
import { formatTaka } from '../lib/demo'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { Input } from '../components/ui/Input'
import { Label } from '../components/ui/Label'
import { WorkspacePicker } from '../components/WorkspacePicker'
import { countsSummary, totalResults } from '../components/RunCounts'

/** The synthetic rule fixtures the run endpoint accepts (GET /api/rules). */
const FIXTURES = ['Fixture Alpha', 'Fixture Beta', 'Fixture Gamma']

export const DQ_LABEL: Record<MoneyDqState, string> = {
  column_absent: 'column absent',
  blank: 'blank — unknown, not zero',
  invalid: 'invalid — unknown',
  explicit_zero: 'explicit zero',
  present_valid: 'present',
}

export const DQ_VARIANT: Record<MoneyDqState, 'muted' | 'warning' | 'danger' | 'success'> = {
  column_absent: 'muted',
  blank: 'warning',
  invalid: 'danger',
  explicit_zero: 'muted',
  present_valid: 'success',
}

const MATCH_VARIANT: Record<string, 'success' | 'warning' | 'danger' | 'muted' | 'info'> = {
  strong_matched: 'success',
  ambiguous: 'warning',
  duplicate: 'danger',
  unmatched: 'muted',
}

export function MoneyCell({ poisha, state }: { poisha: number | null; state: MoneyDqState }) {
  return (
    <span className="inline-flex flex-col gap-1">
      <span className="font-medium">{formatTaka(poisha)}</span>
      <Badge variant={DQ_VARIANT[state]}>{DQ_LABEL[state]}</Badge>
    </span>
  )
}

export function ImportDetailPage() {
  const { batchId } = useParams<{ batchId: string }>()
  const { selectedId } = useSelectedWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [fixture, setFixture] = useState(FIXTURES[0])
  const [tolerance, setTolerance] = useState('0')
  const [createError, setCreateError] = useState<string | null>(null)
  // One idempotency key per form instance: a retry after a failure keeps
  // the same key, a fresh page load gets a fresh one.
  const [idempotencyKey] = useState(() =>
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `run-${Date.now()}`,
  )

  const detail = useQuery({
    queryKey: ['import-detail', selectedId, batchId],
    queryFn: () => getImportDetail(selectedId!, batchId!),
    enabled: !!selectedId && !!batchId,
    retry: false,
  })

  const batchRuns = useQuery({
    queryKey: ['batch-runs', selectedId, batchId],
    queryFn: () => listBatchRuns(selectedId!, batchId!),
    enabled: !!selectedId && !!batchId,
    retry: false,
  })

  const createRun = useMutation({
    mutationFn: () => {
      const tolerancePoisha = Number(tolerance)
      if (!Number.isInteger(tolerancePoisha) || tolerancePoisha < 0) {
        throw new Error('Tolerance must be a whole number of poisha, 0 or more.')
      }
      return createRunForImport(selectedId!, batchId!, {
        courier_fixture: fixture,
        tolerance_poisha: tolerancePoisha,
        idempotency_key: idempotencyKey,
      })
    },
    onSuccess: (run) => {
      setCreateError(null)
      queryClient.invalidateQueries({ queryKey: ['batch-runs', selectedId, batchId] })
      navigate(`/runs/${run.run_id}`)
    },
    onError: (err) => {
      setCreateError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Could not create the run.',
      )
    },
  })

  return (
    <div className="space-y-6">
      <div>
        <Link to="/imports" className="text-sm font-medium text-primary hover:underline">
          ← Back to Imports
        </Link>
        <h1 className="mt-2 text-2xl font-bold text-slate-900">Import detail</h1>
        <p className="mt-1 font-mono text-xs text-slate-500">Batch {batchId}</p>
      </div>

      <WorkspacePicker />

      {detail.isPending && <p className="text-sm text-slate-500">Loading…</p>}
      {detail.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load this import (it may belong to another workspace, or the backend
              may be down).
            </p>
          </CardContent>
        </Card>
      )}

      {detail.isSuccess && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Status</p>
                <p className="mt-1">
                  <Badge variant={detail.data.batch.status === 'parsed' ? 'success' : 'warning'}>
                    {detail.data.batch.status}
                  </Badge>
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Rows</p>
                <p className="mt-1 text-2xl font-bold">{detail.data.batch.row_count}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Parsed rows</p>
                <p className="mt-1 text-2xl font-bold">{detail.data.batch.parsed_row_count}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Error rows</p>
                <p className="mt-1 text-2xl font-bold">{detail.data.batch.error_row_count}</p>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Reconciliation runs</CardTitle>
              <CardDescription>
                Persisted runs created from this import, newest first. A run is
                immutable once created — it is the stored history of this batch.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {batchRuns.isPending && <p className="text-sm text-slate-500">Loading…</p>}
              {batchRuns.isError && (
                <p className="text-sm text-red-600">
                  Could not load this import&apos;s runs from the backend. The run
                  endpoints may not be deployed yet.
                </p>
              )}
              {batchRuns.isSuccess && batchRuns.data.runs.length > 0 && (
                <ul className="divide-y">
                  {batchRuns.data.runs.map((r) => (
                    <li key={r.run_id} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
                      <span>
                        <Link
                          to={`/runs/${r.run_id}`}
                          className="font-mono text-xs font-medium text-primary hover:underline"
                        >
                          {r.run_id.slice(0, 8)}…
                        </Link>
                        <span className="ml-2 text-slate-500">
                          {r.created_at ? r.created_at.slice(0, 19).replace('T', ' ') : '—'}
                        </span>
                      </span>
                      <span className="flex items-center gap-2">
                        <Badge>{r.status}</Badge>
                        <span className="text-xs text-slate-600">
                          {countsSummary(r.result_counts)}
                        </span>
                        <span className="text-xs text-slate-500">
                          {totalResults(r.result_counts)} result
                          {totalResults(r.result_counts) === 1 ? '' : 's'}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {batchRuns.isSuccess && batchRuns.data.runs.length === 0 && (
                <div className="space-y-4">
                  <p className="text-sm text-slate-500">No run yet for this import.</p>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault()
                      createRun.mutate()
                    }}
                    className="flex flex-wrap items-end gap-3"
                  >
                    <div className="space-y-2">
                      <Label htmlFor="run-fixture">Rule fixture</Label>
                      <select
                        id="run-fixture"
                        value={fixture}
                        onChange={(e) => setFixture(e.target.value)}
                        className="h-10 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                      >
                        {FIXTURES.map((f) => (
                          <option key={f} value={f}>
                            {f}
                          </option>
                        ))}
                      </select>
                      <p className="text-xs text-slate-500">
                        Synthetic fixture rules — not real courier contracts.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="run-tolerance">Tolerance (poisha)</Label>
                      <Input
                        id="run-tolerance"
                        type="number"
                        min={0}
                        step={1}
                        value={tolerance}
                        onChange={(e) => setTolerance(e.target.value)}
                      />
                      <p className="text-xs text-slate-500">
                        Default 0 — differences outside tolerance are flagged.
                      </p>
                    </div>
                    <Button type="submit" disabled={createRun.isPending}>
                      {createRun.isPending ? 'Creating run…' : 'Create run'}
                    </Button>
                  </form>
                  {createError && <p className="text-sm text-red-600">{createError}</p>}
                  {createRun.isSuccess && (
                    <p className="text-sm text-emerald-700">
                      Run created — opening it now.
                    </p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          {detail.data.truncated && (
            <div className="rounded-md bg-amber-50 px-4 py-3 text-sm text-amber-800">
              Showing the first 500 rows only — this batch has more rows than the detail
              endpoint returns (truncated).
            </div>
          )}

          <Card>
            <CardHeader>
              <CardTitle>Rows</CardTitle>
              <CardDescription>
                Raw values are preserved verbatim from the file. Reported money is shown in
                taka with its per-field data-quality state: blank or invalid is unknown,
                never a silent zero. Matching never uses amounts alone.
              </CardDescription>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-left text-sm">
                <thead>
                  <tr className="border-b text-slate-500">
                    <th className="py-2 pr-4 font-medium">Row</th>
                    <th className="py-2 pr-4 font-medium">Tracking</th>
                    <th className="py-2 pr-4 font-medium">Order ref</th>
                    <th className="py-2 pr-4 font-medium">Reported status</th>
                    <th className="py-2 pr-4 font-medium">Collected</th>
                    <th className="py-2 pr-4 font-medium">Payout</th>
                    <th className="py-2 pr-4 font-medium">Delivery fee</th>
                    <th className="py-2 pr-4 font-medium">COD fee</th>
                    <th className="py-2 pr-4 font-medium">Match</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data.rows.map((row) => {
                    const line = row.statement_line
                    return (
                      <tr key={row.row_number} className="border-b align-top last:border-0">
                        <td className="py-2 pr-4 font-mono text-xs">{row.row_number}</td>
                        <td className="py-2 pr-4">
                          <span className="font-mono text-xs">
                            {line?.tracking_id_normalised ?? line?.tracking_id_raw ?? '—'}
                          </span>
                          {line?.tracking_id_raw &&
                            line.tracking_id_normalised !== line.tracking_id_raw && (
                              <span className="block text-xs text-slate-400">
                                raw: {line.tracking_id_raw}
                              </span>
                            )}
                        </td>
                        <td className="py-2 pr-4 font-mono text-xs">
                          {line?.merchant_order_ref_raw ?? '—'}
                        </td>
                        <td className="py-2 pr-4">{line?.reported_status_raw ?? '—'}</td>
                        <td className="py-2 pr-4">
                          {line ? (
                            <MoneyCell
                              poisha={line.reported_collected_poisha}
                              state={line.reported_collected_state}
                            />
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="py-2 pr-4">
                          {line ? (
                            <MoneyCell
                              poisha={line.reported_payout_poisha}
                              state={line.reported_payout_state}
                            />
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="py-2 pr-4">
                          {line ? (
                            <MoneyCell
                              poisha={line.reported_delivery_fee_poisha}
                              state={line.reported_delivery_fee_state}
                            />
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="py-2 pr-4">
                          {line ? (
                            <MoneyCell
                              poisha={line.reported_cod_fee_poisha}
                              state={line.reported_cod_fee_state}
                            />
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="py-2">
                          <div className="flex flex-col items-start gap-1">
                            <Badge variant={MATCH_VARIANT[line?.match_status ?? ''] ?? 'muted'}>
                              {line?.match_status ?? 'no statement line'}
                            </Badge>
                            <span className="text-xs text-slate-500">{row.parse_status}</span>
                            {line?.statement_date && (
                              <span className="text-xs text-slate-400">{line.statement_date}</span>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {detail.data.rows.length === 0 && (
                <p className="py-4 text-sm text-slate-500">This batch has no rows.</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Raw values (verbatim)</CardTitle>
              <CardDescription>
                The exact values as read from the file, per row — nothing normalised or
                dropped.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="space-y-3">
                {detail.data.rows.map((row) => (
                  <li key={row.row_number} className="text-sm">
                    <span className="font-mono text-xs text-slate-500">
                      Row {row.row_number} ({row.parse_status}):
                    </span>{' '}
                    <code className="break-all rounded bg-slate-100 px-1 text-xs">
                      {JSON.stringify(row.raw_values)}
                    </code>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Import errors ({detail.data.errors.length})</CardTitle>
              <CardDescription>
                Data-quality warnings and errors recorded while parsing this batch.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {detail.data.errors.length === 0 && (
                <p className="text-sm text-slate-500">No errors recorded for this batch.</p>
              )}
              {detail.data.errors.length > 0 && (
                <ul className="divide-y">
                  {detail.data.errors.map((e, i) => (
                    <li key={i} className="py-3 text-sm">
                      <div className="flex items-center gap-2">
                        <Badge variant={e.severity === 'error' ? 'danger' : 'warning'}>
                          {e.severity}
                        </Badge>
                        <code className="rounded bg-slate-100 px-1 text-xs">{e.error_code}</code>
                        <span className="font-medium text-slate-900">{e.field_name}</span>
                      </div>
                      <p className="mt-1 text-slate-600">{e.message}</p>
                      {e.raw_value !== null && e.raw_value !== undefined && (
                        <p className="mt-1 text-xs text-slate-500">
                          Raw value: <code className="rounded bg-slate-100 px-1">{e.raw_value}</code>
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Mapping snapshot</CardTitle>
              <CardDescription>
                The column mapping stored with this batch, immutable — it is never changed
                after upload.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="overflow-x-auto rounded-md bg-slate-50 p-4 font-mono text-xs text-slate-800">
                {JSON.stringify(detail.data.batch.mapping_snapshot, null, 2)}
              </pre>
              <p className="mt-3 text-xs text-slate-500">
                File object: <code className="rounded bg-slate-100 px-1">{detail.data.batch.file_object_id}</code>
                {detail.data.batch.created_at &&
                  ` · Created ${detail.data.batch.created_at.slice(0, 19).replace('T', ' ')}`}
                {detail.data.batch.idempotency_outcome &&
                  ` · Idempotency: ${detail.data.batch.idempotency_outcome}`}
              </p>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
