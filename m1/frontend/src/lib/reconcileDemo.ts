import { useQuery } from '@tanstack/react-query'
import { postReconcile, type ReconcileResult } from './api'
import { DEMO_RECONCILE_SHIPMENTS } from './demo'

/**
 * Runs the labelled synthetic demo shipments through the live engine
 * (POST /api/reconcile). Nothing is persisted — the endpoint computes
 * and returns only.
 *
 * The five demo shipments cover all five V2.2.1 statuses. Demo E needs
 * a 500-poisha tolerance, the others run at 0, so the batch is sent as
 * two calls and the results are merged in demo order.
 */
export function useDemoReconcile(): {
  results: ReconcileResult[]
  isPending: boolean
  isError: boolean
  error: Error | null
} {
  const query = useQuery({
    queryKey: ['demo-reconcile'],
    queryFn: async () => {
      const main = DEMO_RECONCILE_SHIPMENTS.filter((s) => !s.tolerance_poisha)
      const tolerant = DEMO_RECONCILE_SHIPMENTS.filter((s) => s.tolerance_poisha)
      const [r0, r1] = await Promise.all([
        postReconcile(main as unknown as Record<string, unknown>[], 0),
        tolerant.length > 0
          ? postReconcile(
              tolerant as unknown as Record<string, unknown>[],
              tolerant[0].tolerance_poisha ?? 0,
            )
          : Promise.resolve({ results: [], count: 0 }),
      ])
      return [...r0.results, ...r1.results]
    },
    retry: false,
    staleTime: 60_000,
  })

  return {
    results: query.data ?? [],
    isPending: query.isPending,
    isError: query.isError,
    error: query.error instanceof Error ? query.error : null,
  }
}
