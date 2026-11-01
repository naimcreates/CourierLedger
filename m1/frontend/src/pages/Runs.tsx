import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { getRun, getShipment, listRunFindings, listRunResults } from '../lib/api'
import { formatTaka, STATUS_EXPLANATION, type ReconciliationStatus } from '../lib/demo'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge, StatusBadge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { RunCounts } from '../components/RunCounts'
import { RunResultDetail } from '../components/RunResultDetail'
import { WorkspacePicker } from '../components/WorkspacePicker'

const PAGE_SIZE = 20

function shortId(id: string | null | undefined): string {
  return id ? `${id.slice(0, 8)}…` : '—'
}

/**
 * Run detail page: one persisted reconciliation run created from an
 * import. Header + counts from GET /runs/{runId}; results and findings
 * from the paginated /results and /findings endpoints. The five
 * variances (C, V_R, S, V_cond, V_total) are shown separately, never
 * summed. Tracking is shown only when the backend provides it — the
 * shipment id (short form) is the fallback identifier.
 */
export function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const { selectedId } = useSelectedWorkspace()
  const [resultsOffset, setResultsOffset] = useState(0)
  const [findingsOffset, setFindingsOffset] = useState(0)
  const [selectedResultId, setSelectedResultId] = useState<string | null>(null)

  const run = useQuery({
    queryKey: ['run', selectedId, runId],
    queryFn: () => getRun(selectedId!, runId!),
    enabled: !!selectedId && !!runId,
    retry: false,
  })

  const results = useQuery({
    queryKey: ['run-results', selectedId, runId, resultsOffset],
    queryFn: () => listRunResults(selectedId!, runId!, PAGE_SIZE, resultsOffset),
    enabled: !!selectedId && !!runId,
    retry: false,
  })

  const findings = useQuery({
    queryKey: ['run-findings', selectedId, runId, findingsOffset],
    queryFn: () => listRunFindings(selectedId!, runId!, PAGE_SIZE, findingsOffset),
    enabled: !!selectedId && !!runId,
    retry: false,
  })

  /**
   * The run payload carries no tracking id (tracking_id is not a
   * column of reconciliation_results), so the tracking id shown for
   * each result is resolved through GET /shipments/{id} — the raw
   * tracking id as the merchant entered it. A lookup that fails
   * leaves the result without a tracking id, and the shipment id
   * short form is shown instead — never an invented tracking id.
   */
  const resultShipments = useQuery({
    queryKey: ['run-result-shipments', selectedId, runId, resultsOffset],
    queryFn: async () => {
      const ids = [...new Set((results.data?.results ?? []).map((r) => r.shipment_id).filter((id): id is string => !!id))]
      const entries = await Promise.all(
        ids.map(async (id) => {
          try {
            const s = await getShipment(selectedId!, id)
            return [id, s.tracking_id ?? s.tracking_id_normalised] as const
          } catch {
            return [id, null] as const
          }
        }),
      )
      return Object.fromEntries(entries) as Record<string, string | null>
    },
    enabled: !!selectedId && results.isSuccess,
    retry: false,
  })

  function trackingFor(r: { tracking_id?: string | null; shipment_id: string | null }): string | null {
    if (r.tracking_id) return r.tracking_id
    if (r.shipment_id) return resultShipments.data?.[r.shipment_id] ?? null
    return null
  }

  const selectedResult =
    (results.data?.results ?? []).find((r) => r.result_id === selectedResultId) ?? null

  return (
    <div className="space-y-6">
      <div>
        <Link to="/reconciliation" className="text-sm font-medium text-primary hover:underline">
          ← Back to Reconciliation
        </Link>
        <h1 className="mt-2 text-2xl font-bold text-slate-900">Reconciliation run</h1>
        <p className="mt-1 font-mono text-xs text-slate-500">Run {runId}</p>
      </div>

      <WorkspacePicker />

      {run.isPending && <p className="text-sm text-slate-500">Loading…</p>}
      {run.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load this run (it may belong to another workspace, or the backend
              run endpoints may not be deployed yet).
            </p>
          </CardContent>
        </Card>
      )}

      {run.isSuccess && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>
                Run {shortId(run.data.run_id)} · <Badge>{run.data.status}</Badge>
              </CardTitle>
              <CardDescription>
                Created from import{' '}
                <Link
                  to={`/imports/${run.data.import_batch_id}`}
                  className="font-mono text-xs font-medium text-primary hover:underline"
                >
                  {shortId(run.data.import_batch_id)}
                </Link>
                {run.data.created_at &&
                  ` · Created ${run.data.created_at.slice(0, 19).replace('T', ' ')}`}
                {run.data.completed_at &&
                  ` · Completed ${run.data.completed_at.slice(0, 19).replace('T', ' ')}`}
                {` · Tolerance ${formatTaka(run.data.tolerance_poisha)}`}
                {run.data.engine_version && ` · Engine ${run.data.engine_version}`}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <RunCounts counts={run.data.result_counts} />
              <p className="mt-4 text-xs text-slate-500">
                {(['matched', 'mismatch', 'needs_review', 'open', 'matched_with_tolerance'] as ReconciliationStatus[])
                  .map((s) => `${s} = ${STATUS_EXPLANATION[s]}`)
                  .join(' · ')}
              </p>
              {run.data.rule_snapshot !== undefined && run.data.rule_snapshot !== null && (
                <details className="mt-4">
                  <summary className="cursor-pointer text-sm font-medium text-slate-700">
                    Rule snapshot (as stored with the run)
                  </summary>
                  <pre className="mt-2 overflow-x-auto rounded-md bg-slate-50 p-4 font-mono text-xs text-slate-800">
                    {JSON.stringify(run.data.rule_snapshot, null, 2)}
                  </pre>
                </details>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>
                Results ({results.data?.total ?? run.data.results.length})
              </CardTitle>
              <CardDescription>
                One row per reconciled shipment. The five variances are shown
                separately — they are never added together. Click a shipment to
                see its full breakdown, fee lines and findings.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {results.isPending && <p className="text-sm text-slate-500">Loading…</p>}
              {results.isError && (
                <p className="text-sm text-red-600">
                  Could not load this run&apos;s results from the backend.
                </p>
              )}
              {results.isSuccess && results.data.results.length === 0 && (
                <p className="text-sm text-slate-500">
                  This run has no results — every eligible record was excluded, or
                  the run produced none.
                </p>
              )}
              {results.isSuccess && results.data.results.length > 0 && (
                <>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[900px] text-left text-sm">
                      <thead>
                        <tr className="border-b text-slate-500">
                          <th className="py-2 pr-4 font-medium">Shipment</th>
                          <th className="py-2 pr-4 font-medium">Expected</th>
                          <th className="py-2 pr-4 font-medium">Reported</th>
                          <th className="py-2 pr-4 font-medium">C</th>
                          <th className="py-2 pr-4 font-medium">V_R</th>
                          <th className="py-2 pr-4 font-medium">S</th>
                          <th className="py-2 pr-4 font-medium">V_cond</th>
                          <th className="py-2 pr-4 font-medium">V_total</th>
                          <th className="py-2 font-medium">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {results.data.results.map((r) => (
                          <tr key={r.result_id} className="border-b last:border-0">
                            <td className="py-2 pr-4">
                              <button
                                type="button"
                                onClick={() =>
                                  setSelectedResultId(
                                    selectedResultId === r.result_id ? null : r.result_id,
                                  )
                                }
                                className="font-mono text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                                aria-expanded={selectedResultId === r.result_id}
                              >
                                {trackingFor(r) ?? shortId(r.shipment_id)}
                              </button>
                              {!trackingFor(r) && r.shipment_id && (
                                <span className="block text-xs text-slate-400">
                                  shipment id — no tracking id for this result
                                </span>
                              )}
                            </td>
                            <td className="py-2 pr-4">{formatTaka(r.expected_collectible_poisha)}</td>
                            <td className="py-2 pr-4">{formatTaka(r.reported_collected_poisha)}</td>
                            <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.c_poisha)}</td>
                            <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.v_r_poisha)}</td>
                            <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.s_poisha)}</td>
                            <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.v_cond_poisha)}</td>
                            <td className="py-2 pr-4 font-mono text-xs">{formatTaka(r.v_total_poisha)}</td>
                            <td className="py-2">
                              <StatusBadge status={r.overall_status as ReconciliationStatus} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="mt-4 flex items-center gap-3 text-sm">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={resultsOffset === 0}
                      onClick={() => setResultsOffset((o) => Math.max(0, o - PAGE_SIZE))}
                    >
                      Previous
                    </Button>
                    <span className="text-slate-500">
                      Showing {resultsOffset + 1}–{resultsOffset + results.data.results.length} of{' '}
                      {results.data.total}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={resultsOffset + results.data.results.length >= results.data.total}
                      onClick={() => setResultsOffset((o) => o + PAGE_SIZE)}
                    >
                      Next
                    </Button>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          {selectedResult && (
            <Card>
              <CardHeader>
                <CardTitle>
                  Result detail — {trackingFor(selectedResult) ?? shortId(selectedResult.shipment_id)}
                </CardTitle>
                <CardDescription>
                  Full breakdown from the persisted run result.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <RunResultDetail result={selectedResult} />
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-4"
                  onClick={() => setSelectedResultId(null)}
                >
                  Close detail
                </Button>
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle>Findings ({findings.data?.total ?? 0})</CardTitle>
              <CardDescription>
                Every finding recorded for this run — none hides another. Each
                finding carries its English and Bangla explanation from the
                backend.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {findings.isPending && <p className="text-sm text-slate-500">Loading…</p>}
              {findings.isError && (
                <p className="text-sm text-red-600">
                  Could not load this run&apos;s findings from the backend.
                </p>
              )}
              {findings.isSuccess && findings.data.findings.length === 0 && (
                <p className="text-sm text-slate-500">No findings recorded for this run.</p>
              )}
              {findings.isSuccess && findings.data.findings.length > 0 && (
                <>
                  <ul className="space-y-2">
                    {findings.data.findings.map((f, i) => (
                      <li key={i} className="rounded-md bg-slate-50 px-3 py-2 text-sm">
                        <div className="flex flex-wrap items-center gap-2">
                          {f.category && <Badge variant="info">{f.category}</Badge>}
                          {f.finding_type && <Badge variant="muted">{f.finding_type}</Badge>}
                          <code className="rounded bg-slate-100 px-1 text-xs">{f.finding_code}</code>
                          {f.amount_poisha !== null && f.amount_poisha !== undefined && (
                            <span className="font-mono text-xs">{formatTaka(f.amount_poisha)}</span>
                          )}
                          {f.is_within_tolerance !== null && f.is_within_tolerance !== undefined && (
                            <Badge variant={f.is_within_tolerance ? 'success' : 'warning'}>
                              {f.is_within_tolerance ? 'within tolerance' : 'outside tolerance'}
                            </Badge>
                          )}
                        </div>
                        {f.explanation_en && <p className="mt-1 text-slate-600">{f.explanation_en}</p>}
                        {f.explanation_bn && <p className="mt-1 text-slate-600">{f.explanation_bn}</p>}
                      </li>
                    ))}
                  </ul>
                  <div className="mt-4 flex items-center gap-3 text-sm">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={findingsOffset === 0}
                      onClick={() => setFindingsOffset((o) => Math.max(0, o - PAGE_SIZE))}
                    >
                      Previous
                    </Button>
                    <span className="text-slate-500">
                      Showing {findingsOffset + 1}–{findingsOffset + findings.data.findings.length} of{' '}
                      {findings.data.total}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={findingsOffset + findings.data.findings.length >= findings.data.total}
                      onClick={() => setFindingsOffset((o) => o + PAGE_SIZE)}
                    >
                      Next
                    </Button>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
