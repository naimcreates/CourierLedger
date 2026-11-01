import { useQuery } from '@tanstack/react-query'
import { getReportSummary, type ReportVarianceKey } from '../lib/api'
import { formatTaka, STATUS_EXPLANATION } from '../lib/demo'
import { ALLOCATION_STATUS_BANGLA, BANK_STATUS_BANGLA } from '../lib/settlement'
import { useSelectedWorkspace } from '../lib/workspace'
import { Badge } from '../components/ui/Badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/Card'
import { RunCounts } from '../components/RunCounts'
import { WorkspacePicker } from '../components/WorkspacePicker'

/**
 * One variance total: what it measures, in plain English and Bangla.
 * The five totals are separate measures of different things — they are
 * never added together, and no combined "total difference" is shown.
 * Each total sums only that variance's non-NULL values; the NULL count
 * is shown beside it so unknown values are visible, never hidden.
 */
const VARIANCE_TOTALS: {
  key: ReportVarianceKey
  label: string
  en: string
  bn: string
}[] = [
  {
    key: 'collection_c',
    label: 'C — collection variance',
    en: 'Reported collected minus expected collectible, summed over results.',
    bn: 'রিপোর্ট করা আদায় বিয়োগ প্রত্যাশিত আদায়, সব ফলাফলের যোগফল।',
  },
  {
    key: 'fee_v_r',
    label: 'V_R — fee / tax variance (reported basis)',
    en: 'Fee and tax difference on the reported basis, summed over results.',
    bn: 'রিপোর্ট করা ভিত্তিতে ফি ও করের পার্থক্য, সব ফলাফলের যোগফল।',
  },
  {
    key: 'statement_s',
    label: 'S — statement arithmetic residual',
    en: 'Reported payout minus (reported collected minus reported deductions), summed over results. A non-zero S means the statement itself does not add up.',
    bn: 'রিপোর্ট করা পেমেন্ট বিয়োগ (রিপোর্ট করা আদায় বিয়োগ রিপোর্ট করা কাটছাঁট), সব ফলাফলের যোগফল। শূন্য না হলে বিবৃতিটি নিজেই মেলে না।',
  },
  {
    key: 'conditional_payout_v_cond',
    label: 'V_cond — contractual payout variance',
    en: 'The contractual payout difference — the one that decides whether the payout matches. Summed over results.',
    bn: 'চুক্তিগত পেমেন্টের পার্থক্য — এটিই নির্ধারণ করে পেমেন্ট মিলেছে কি না। সব ফলাফলের যোগফল।',
  },
  {
    key: 'scenario_v_total',
    label: 'V_total — scenario-only total variance',
    en: 'Scenario-only total (reported_collected base). It never decides a contractual payout mismatch — V_cond does.',
    bn: 'শুধুমাত্র পরিস্থিতি-ভিত্তিক মোট (reported_collected ভিত্তি)। এটি কখনো চুক্তিগত পেমেন্টের অমিল নির্ধারণ করে না — V_cond করে।',
  },
]

function StatusCountList({
  counts,
  bangla,
  emptyLabel,
}: {
  counts: Record<string, number>
  bangla: Record<string, string>
  emptyLabel: string
}) {
  const entries = Object.entries(counts)
  if (entries.length === 0) {
    return <p className="text-sm text-slate-500">{emptyLabel}</p>
  }
  return (
    <ul className="space-y-2 text-sm">
      {entries.map(([status, count]) => (
        <li key={status} className="flex items-center justify-between gap-2">
          <span>
            <Badge variant="muted">
              {status}
              {bangla[status] ? ` · ${bangla[status]}` : ''}
            </Badge>
          </span>
          <span className="text-xl font-bold">{count}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * Reports: workspace-wide aggregates from GET /reports/summary.
 * Every figure comes from the backend; when the workspace is empty the
 * counts are 0 and each variance total is ৳0.00 over 0 results, never
 * an invented number. A difference is shown for review, never
 * automatically labelled a loss.
 */
export function ReportsPage() {
  const { selectedId } = useSelectedWorkspace()

  const summary = useQuery({
    queryKey: ['report-summary', selectedId],
    queryFn: () => getReportSummary(selectedId!),
    enabled: !!selectedId,
    retry: false,
  })

  const data = summary.data
  const isEmpty =
    data !== undefined &&
    data.reconciliation_runs.total === 0 &&
    data.reconciliation_results.total === 0 &&
    data.settlement_batches.total === 0 &&
    data.bank_receipts.total === 0 &&
    data.import_batches.total === 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Reports</h1>
        <p className="mt-1 text-sm text-slate-500">
          Workspace-wide reconciliation summaries, from the backend. Counts are 0
          when empty; each variance total sums only that variance&apos;s known
          values, with the unknown count shown beside it. A difference is shown
          for review — never automatically labelled a loss.
        </p>
      </div>

      <WorkspacePicker />

      {summary.isPending && <p className="text-sm text-slate-500">Loading…</p>}
      {summary.isError && (
        <Card>
          <CardContent>
            <p className="text-sm text-red-600">
              Could not load the report summary from the backend — the endpoint may not
              be deployed yet.
            </p>
          </CardContent>
        </Card>
      )}

      {summary.isSuccess && data && (
        <>
          {isEmpty && (
            <div className="rounded-md bg-slate-50 px-4 py-3 text-sm text-slate-600">
              Nothing to report yet — no runs, results, imports, settlement batches
              or receipts in this workspace. The counts below are all 0, honestly.
            </div>
          )}

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Import batches</p>
                <p className="mt-1 text-2xl font-bold">{data.import_batches.total}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Bank receipts</p>
                <p className="mt-1 text-2xl font-bold">{data.bank_receipts.total}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {data.bank_receipts.complete} complete ·{' '}
                  {data.bank_receipts.incomplete} incomplete
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Reconciliation runs</p>
                <p className="mt-1 text-2xl font-bold">{data.reconciliation_runs.total}</p>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-4">
                <p className="text-sm text-slate-500">Reconciliation results</p>
                <p className="mt-1 text-2xl font-bold">
                  {data.reconciliation_results.total}
                </p>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Runs by status</CardTitle>
              <CardDescription>
                How many reconciliation runs are in each run status (running,
                completed, failed) — the status of the run itself, not of its
                results. See the results below for the per-shipment outcomes.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <StatusCountList
                counts={data.reconciliation_runs.by_status}
                bangla={{}}
                emptyLabel="No reconciliation runs yet (0)."
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Results by overall status</CardTitle>
              <CardDescription>
                How many persisted reconciliation results are in each of the five
                overall statuses.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <RunCounts counts={data.reconciliation_results.by_overall_status} />
              <ul className="space-y-1 text-xs text-slate-500">
                {(Object.keys(STATUS_EXPLANATION) as Array<keyof typeof STATUS_EXPLANATION>).map(
                  (s) => (
                    <li key={s}>
                      <span className="font-medium text-slate-700">{s}:</span>{' '}
                      {STATUS_EXPLANATION[s]}
                    </li>
                  ),
                )}
              </ul>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Variance totals — five separate measures</CardTitle>
              <CardDescription>
                Each total sums one variance across all persisted results, counting
                only the values that are known — the unknown count is shown beside
                each total. They measure different things and are{' '}
                <strong>never added together</strong> — there is no single
                &ldquo;total difference&rdquo; figure. A total of ৳0.00 over 0
                results means there is nothing to total yet, not that everything
                matches.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4 lg:grid-cols-2">
                {VARIANCE_TOTALS.map((v) => {
                  const total = data.reconciliation_results.variances_poisha[v.key]
                  return (
                    <div
                      key={v.key}
                      data-testid={`variance-total-${v.key}`}
                      className="rounded-md border border-slate-200 px-4 py-3"
                    >
                      <p className="text-sm font-medium text-slate-900">{v.label}</p>
                      <p className="mt-1 text-2xl font-bold">
                        {formatTaka(total.total_poisha)}
                      </p>
                      <p className="mt-1 text-xs text-slate-500">
                        {total.non_null_count} known value
                        {total.non_null_count === 1 ? '' : 's'}
                        {total.null_count > 0 &&
                          ` · ${total.null_count} unknown (not counted)`}
                        {total.non_null_count === 0 &&
                          total.null_count === 0 &&
                          ' · no results yet'}
                      </p>
                      <p className="mt-1 text-xs text-slate-500">
                        {v.en} {v.bn}
                      </p>
                    </div>
                  )
                })}
              </div>
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>
                  Settlement batches by allocation status (
                  {data.settlement_batches.total})
                </CardTitle>
              </CardHeader>
              <CardContent>
                <StatusCountList
                  counts={data.settlement_batches.by_allocation_status}
                  bangla={ALLOCATION_STATUS_BANGLA}
                  emptyLabel="No settlement batches yet (0)."
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Settlement batches by bank status</CardTitle>
              </CardHeader>
              <CardContent>
                <StatusCountList
                  counts={data.settlement_batches.by_bank_status}
                  bangla={BANK_STATUS_BANGLA}
                  emptyLabel="No settlement batches yet (0)."
                />
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Import batches by status ({data.import_batches.total})</CardTitle>
            </CardHeader>
            <CardContent>
              <StatusCountList
                counts={data.import_batches.by_status}
                bangla={{}}
                emptyLabel="No import batches yet (0)."
              />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
