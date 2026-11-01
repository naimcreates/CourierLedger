import type { PersistedRunResult } from '../lib/api'
import { formatTaka, STATUS_EXPLANATION, type ReconciliationStatus } from '../lib/demo'
import { Badge, StatusBadge } from './ui/Badge'

function Row({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <tr className="border-b last:border-0">
      <td className="py-2 pr-4 font-medium text-slate-700">{label}</td>
      <td className="py-2 pr-4 font-mono text-xs">{value}</td>
      <td className="py-2 text-xs text-slate-500">{note ?? ''}</td>
    </tr>
  )
}

/**
 * Full per-result breakdown from a persisted reconciliation run.
 * The five variances (C, V_R, S, V_cond, V_total) are shown separately
 * and never summed — each answers a different question.
 *
 * tracking_id is rendered only when the backend provides it; otherwise
 * the shipment id (short form) identifies the result.
 */
export function RunResultDetail({ result }: { result: PersistedRunResult }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={result.overall_status as ReconciliationStatus} />
        <span className="text-sm text-slate-600">
          {STATUS_EXPLANATION[result.overall_status as ReconciliationStatus] ?? ''}
        </span>
      </div>

      <table className="w-full text-left text-sm">
        <tbody>
          <Row
            label="Tracking"
            value={result.tracking_id ?? '—'}
            note={
              result.tracking_id
                ? undefined
                : 'No tracking id in the run payload — showing the shipment id instead.'
            }
          />
          <Row label="Shipment" value={result.shipment_id ? `${result.shipment_id.slice(0, 8)}…` : '—'} />
          <Row label="Statement line" value={result.statement_line_id ? `${result.statement_line_id.slice(0, 8)}…` : '—'} />
          <Row label="Expected collectible" value={formatTaka(result.expected_collectible_poisha)} note="NULL means it cannot be fixed from the given information." />
          <Row label="Reported collected" value={formatTaka(result.reported_collected_poisha)} />
          <Row label="Expected deductions (X_e)" value={formatTaka(result.x_e_poisha ?? null)} />
          <Row label="Reported deductions (X_R)" value={formatTaka(result.x_r_poisha ?? null)} />
          <Row label="Reported deductions, upper bound (X_upper_R)" value={formatTaka(result.x_upper_r_poisha ?? null)} />
          <Row label="Expected payout (P_e)" value={formatTaka(result.p_e_poisha)} note="Scenario-only when the fee base is reported_collected." />
          <Row label="Payout conditional on reported collection (P_cond)" value={formatTaka(result.p_cond_poisha)} />
          <Row label="Reported payout (P_r)" value={formatTaka(result.p_r_poisha)} />
        </tbody>
      </table>

      <div>
        <h3 className="text-sm font-semibold text-slate-900">
          Variances — shown separately, never summed
        </h3>
        <table className="mt-2 w-full text-left text-sm">
          <tbody>
            <Row label="C — collection variance" value={formatTaka(result.c_poisha)} note="Reported collected minus expected collectible." />
            <Row label="Δ — deduction change" value={formatTaka(result.delta_poisha)} note="Deduction change due to a fee-basis change." />
            <Row label="V_R — fee/tax variance (reported basis)" value={formatTaka(result.v_r_poisha)} note="Fee judgement uses this, never the expected basis." />
            <Row label="S — statement arithmetic residual" value={formatTaka(result.s_poisha)} note="Reported payout minus (reported collected minus reported deductions). The statement's own inconsistency." />
            <Row label="V_cond — contractual payout variance" value={formatTaka(result.v_cond_poisha)} note="Decides the contractual payout status, on its own." />
            <Row label="V_total — scenario total variance" value={formatTaka(result.v_total_poisha)} note="Scenario-only (reported_collected base). Never triggers a contractual payout mismatch." />
          </tbody>
        </table>
        <p className="mt-2 text-xs text-slate-500">
          Contractual payout status: <strong>{result.contractual_payout_status}</strong>
          {' · '}Data quality: <strong>{result.data_quality_status}</strong>
        </p>
      </div>

      <div>
        <h3 className="text-sm font-semibold text-slate-900">
          Fee lines ({result.fee_lines.length})
        </h3>
        {result.fee_lines.length === 0 && (
          <p className="mt-1 text-sm text-slate-500">No fee lines recorded for this result.</p>
        )}
        {result.fee_lines.length > 0 && (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[640px] text-left text-sm">
              <thead>
                <tr className="border-b text-slate-500">
                  <th className="py-2 pr-4 font-medium">Fee</th>
                  <th className="py-2 pr-4 font-medium">Seq</th>
                  <th className="py-2 pr-4 font-medium">Expected basis</th>
                  <th className="py-2 pr-4 font-medium">Reported basis</th>
                  <th className="py-2 pr-4 font-medium">Reported actual</th>
                  <th className="py-2 font-medium">Included tax</th>
                </tr>
              </thead>
              <tbody>
                {result.fee_lines.map((f, i) => (
                  <tr key={i} className="border-b last:border-0">
                    <td className="py-2 pr-4 font-mono text-xs">{f.fee_code}</td>
                    <td className="py-2 pr-4">{f.component_seq ?? '—'}</td>
                    <td className="py-2 pr-4">
                      {formatTaka(f.expected_basis_poisha ?? null)}
                      {f.expected_basis_state && (
                        <span className="block text-xs text-slate-400">{f.expected_basis_state}</span>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      {formatTaka(f.reported_basis_poisha ?? null)}
                      {f.reported_basis_state && (
                        <span className="block text-xs text-slate-400">{f.reported_basis_state}</span>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      {formatTaka(f.reported_actual_poisha ?? null)}
                      {f.reported_actual_state && (
                        <span className="block text-xs text-slate-400">{f.reported_actual_state}</span>
                      )}
                    </td>
                    <td className="py-2">{formatTaka(f.included_tax_poisha ?? null)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div>
        <h3 className="text-sm font-semibold text-slate-900">
          Findings ({result.findings.length}) — none hides another
        </h3>
        {result.findings.length === 0 && (
          <p className="mt-1 text-sm text-slate-500">No findings — everything agrees.</p>
        )}
        <ul className="mt-2 space-y-2">
          {result.findings.map((f, i) => (
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
      </div>
    </div>
  )
}
