import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { getRules, listRuns } from '../lib/api'
import { useDemoReconcile } from '../lib/reconcileDemo'
import { STATUS_BANGLA, STATUS_EXPLANATION, formatTaka, type ReconciliationStatus } from '../lib/demo'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge, StatusBadge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { ReconcileDetail } from '../components/ReconcileDetail'
import { WorkspacePicker } from '../components/WorkspacePicker'
import { countsSummary, totalResults } from '../components/RunCounts'

const ALL_STATUSES: ReconciliationStatus[] = [
  'matched',
  'mismatch',
  'needs_review',
  'open',
  'matched_with_tolerance',
]

const PAGE_SIZE = 20

function shortId(id: string | null | undefined): string {
  return id ? `${id.slice(0, 8)}…` : '—'
}

/**
 * Reconciliation page: persisted reconciliation runs FIRST (created from
 * an import via POST /api/workspaces/:id/imports/:batchId/runs, listed
 * via GET /api/workspaces/:id/runs), then the rule fixtures and the
 * clearly-labelled synthetic demo reconcile below. A run's five
 * variances (C, V_R, S, V_cond, V_total) are kept separate and never
 * summed.
 */
export function ReconciliationPage() {
  const [selectedTracking, setSelectedTracking] = useState<string | null>(null)
  const [runsOffset, setRunsOffset] = useState(0)
  const { selectedId } = useSelectedWorkspace()
  const demo = useDemoReconcile()
  const rules = useQuery({ queryKey: ['rules'], queryFn: getRules, retry: false })

  const runs = useQuery({
    queryKey: ['runs', selectedId, runsOffset],
    queryFn: () => listRuns(selectedId!, PAGE_SIZE, runsOffset),
    enabled: !!selectedId,
    retry: false,
  })

  const selected = demo.results.find((r) => r.tracking_id === selectedTracking) ?? null

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Reconciliation</h1>
        <p className="mt-1 text-sm text-slate-500">
          Compare expected COD against what the courier reported collecting. An unexplained
          difference is shown for review — it is never automatically labelled a confirmed
          loss. The five variances (C, V_R, S, V_cond, V_total) are kept separate and
          never summed.
        </p>
      </div>

      <WorkspacePicker />

      <Card>
        <CardHeader>
          <CardTitle>Reconciliation runs</CardTitle>
          <CardDescription>
            Persisted runs created from an import, newest first. Open a run to see
            every result with its variances, fee lines and findings.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!selectedId && (
            <p className="text-sm text-slate-500">
              Create a workspace first (Workspaces page), then come back to see runs.
            </p>
          )}
          {selectedId && runs.isPending && <p className="text-sm text-slate-500">Loading…</p>}
          {selectedId && runs.isError && (
            <p className="text-sm text-red-600">
              Could not load reconciliation runs from the backend. The run endpoints
              may not be deployed yet — runs are created from an import once they are.
            </p>
          )}
          {selectedId && runs.isSuccess && runs.data.runs.length === 0 && (
            <p className="text-sm text-slate-500">
              No reconciliation runs yet — create one from an import.
            </p>
          )}
          {selectedId && runs.isSuccess && runs.data.runs.length > 0 && (
            <>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-left text-sm">
                  <thead>
                    <tr className="border-b text-slate-500">
                      <th className="py-2 pr-4 font-medium">Run</th>
                      <th className="py-2 pr-4 font-medium">Status</th>
                      <th className="py-2 pr-4 font-medium">Created</th>
                      <th className="py-2 pr-4 font-medium">Import batch</th>
                      <th className="py-2 pr-4 font-medium">Results</th>
                      <th className="py-2 font-medium">Counts by status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.data.runs.map((r) => (
                      <tr key={r.run_id} className="border-b last:border-0">
                        <td className="py-2 pr-4">
                          <Link
                            to={`/runs/${r.run_id}`}
                            className="font-mono text-xs font-medium text-primary hover:underline"
                          >
                            {shortId(r.run_id)}
                          </Link>
                        </td>
                        <td className="py-2 pr-4">
                          <Badge>{r.status}</Badge>
                        </td>
                        <td className="py-2 pr-4">
                          {r.created_at ? r.created_at.slice(0, 19).replace('T', ' ') : '—'}
                        </td>
                        <td className="py-2 pr-4">
                          <Link
                            to={`/imports/${r.import_batch_id}`}
                            className="font-mono text-xs font-medium text-primary hover:underline"
                          >
                            {shortId(r.import_batch_id)}
                          </Link>
                        </td>
                        <td className="py-2 pr-4">{totalResults(r.result_counts)}</td>
                        <td className="py-2 text-xs text-slate-600">{countsSummary(r.result_counts)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-4 flex items-center gap-3 text-sm">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={runsOffset === 0}
                  onClick={() => setRunsOffset((o) => Math.max(0, o - PAGE_SIZE))}
                >
                  Previous
                </Button>
                <span className="text-slate-500">
                  Showing {runsOffset + 1}–{runsOffset + runs.data.runs.length} of{' '}
                  {runs.data.total}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={runsOffset + runs.data.runs.length >= runs.data.total}
                  onClick={() => setRunsOffset((o) => o + PAGE_SIZE)}
                >
                  Next
                </Button>
              </div>
              <p className="mt-4 text-xs text-slate-500">
                {ALL_STATUSES.map((s) => `${s} = ${STATUS_BANGLA[s]} — ${STATUS_EXPLANATION[s]}`).join(' · ')}
              </p>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Rule fixtures (from GET /api/rules)</CardTitle>
          <CardDescription>
            The engine&apos;s synthetic fixture rules. These are invented for testing — not
            real courier contracts.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {rules.isPending && <p className="text-sm text-slate-500">Loading…</p>}
          {rules.isError && (
            <p className="text-sm text-red-600">
              Could not load rules from the backend. Is it running on port 8000?
            </p>
          )}
          {rules.isSuccess && (
            <ul className="divide-y">
              {rules.data.fixtures.map((f) => (
                <li key={f.courier_fixture} className="py-3 text-sm">
                  <p className="font-medium text-slate-900">{f.courier_fixture}</p>
                  <p className="text-xs text-slate-500">
                    COD fee base: {f.cod_fee_base} · rate: {f.cod_fee_rate_bp} bp ·{' '}
                    {f.label}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Demo reconciliation run</CardTitle>
          <CardDescription>
            <Badge variant="warning">SYNTHETIC DEMO</Badge> — five invented shipments, one
            for each status, reconciled live by POST /api/reconcile. Nothing is saved:
            the endpoint returns the computed result only.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {demo.isPending && <p className="text-sm text-slate-500">Reconciling…</p>}
          {demo.isError && (
            <p className="text-sm text-red-600">
              Could not reach the reconcile endpoint: {demo.error?.message ?? 'unknown error'}
            </p>
          )}
          {!demo.isPending && !demo.isError && (
            <>
              <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
                {ALL_STATUSES.map((s) => {
                  const count = demo.results.filter((r) => r.overall_status === s).length
                  return (
                    <div key={s} className="rounded-md border border-slate-200 px-3 py-2">
                      <p className="text-xs text-slate-500">
                        {s} · {STATUS_BANGLA[s]}
                      </p>
                      <p className="mt-1 text-2xl font-bold">{count}</p>
                    </div>
                  )
                })}
              </div>
              <p className="mt-4 text-xs text-slate-500">
                {ALL_STATUSES.map((s) => `${s} = ${STATUS_BANGLA[s]} — ${STATUS_EXPLANATION[s]}`).join(' · ')}
              </p>
              <div className="mt-4 overflow-x-auto">
                <table className="w-full min-w-[720px] text-left text-sm">
                  <thead>
                    <tr className="border-b text-slate-500">
                      <th className="py-2 pr-4 font-medium">Tracking</th>
                      <th className="py-2 pr-4 font-medium">Expected (E)</th>
                      <th className="py-2 pr-4 font-medium">Reported (R)</th>
                      <th className="py-2 pr-4 font-medium">C</th>
                      <th className="py-2 pr-4 font-medium">V_R</th>
                      <th className="py-2 pr-4 font-medium">S</th>
                      <th className="py-2 pr-4 font-medium">V_cond</th>
                      <th className="py-2 pr-4 font-medium">V_total</th>
                      <th className="py-2 font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {demo.results.map((r) => (
                      <tr key={r.tracking_id ?? ''} className="border-b last:border-0">
                        <td className="py-2 pr-4">
                          <button
                            type="button"
                            onClick={() =>
                              setSelectedTracking(
                                selectedTracking === r.tracking_id ? null : r.tracking_id,
                              )
                            }
                            className="font-mono text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                            aria-expanded={selectedTracking === r.tracking_id}
                          >
                            {r.tracking_id}
                          </button>
                        </td>
                        <td className="py-2 pr-4">{formatTaka(r.E)}</td>
                        <td className="py-2 pr-4">{formatTaka(r.R)}</td>
                        <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.C)}</td>
                        <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.V_R)}</td>
                        <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.S)}</td>
                        <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.V_cond)}</td>
                        <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.V_total)}</td>
                        <td className="py-2">
                          <StatusBadge status={r.overall_status as ReconciliationStatus} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-xs text-slate-500">
                Click a tracking ID to see the full breakdown and findings for that
                shipment. C, V_R, S, V_cond and V_total are shown separately — they are
                never added together.
              </p>
            </>
          )}
        </CardContent>
      </Card>

      {selected && (
        <Card>
          <CardHeader>
            <CardTitle>Shipment detail — {selected.tracking_id}</CardTitle>
            <CardDescription>
              Full breakdown from the reconcile response. <Badge variant="warning">SYNTHETIC DEMO</Badge>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ReconcileDetail result={selected} />
            <Button variant="outline" size="sm" className="mt-4" onClick={() => setSelectedTracking(null)}>
              Close detail
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
