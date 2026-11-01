import { STATUS_BANGLA, type ReconciliationStatus } from '../lib/demo'
import type { CountsByStatus } from '../lib/api'

const ALL_STATUSES: ReconciliationStatus[] = [
  'matched',
  'mismatch',
  'needs_review',
  'open',
  'matched_with_tolerance',
]

/**
 * The five V2.2.1 overall-status counts for one run, with Bangla labels.
 * Missing counts are shown as 0 — never invented, just absent from the
 * payload. Reuses STATUS_BANGLA from src/lib/demo.ts.
 */
export function RunCounts({ counts }: { counts: CountsByStatus | null | undefined }) {
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
      {ALL_STATUSES.map((s) => (
        <div key={s} className="rounded-md border border-slate-200 px-3 py-2">
          <p className="text-xs text-slate-500">
            {s} · {STATUS_BANGLA[s]}
          </p>
          <p className="mt-1 text-2xl font-bold">{counts?.[s] ?? 0}</p>
        </div>
      ))}
    </div>
  )
}

/** One-line summary of the five counts, e.g. "matched 2 · open 1". */
export function countsSummary(counts: CountsByStatus | null | undefined): string {
  if (!counts) return '—'
  return ALL_STATUSES.map((s) => `${s} ${counts[s] ?? 0}`).join(' · ')
}

/**
 * Total result count for a run, derived by adding its five
 * result_counts. The backend sends no separate `result_count` field,
 * so the total is always computed here — never a separate number that
 * could disagree with the counts.
 */
export function totalResults(counts: CountsByStatus | null | undefined): number {
  if (!counts) return 0
  return ALL_STATUSES.reduce((sum, s) => sum + (counts[s] ?? 0), 0)
}
