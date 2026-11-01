import type { ReconcileResult } from '../lib/api'
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
 * Full per-shipment breakdown from one POST /api/reconcile result.
 * The five variances (C, V_R, S, V_cond, V_total) are shown separately
 * and never summed — each answers a different question.
 */
export function ReconcileDetail({ result }: { result: ReconcileResult }) {
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
          <Row label="Tracking" value={result.tracking_id ?? '—'} />
          <Row label="Order" value={result.merchant_order_id ?? '—'} />
          <Row label="Outcome" value={result.outcome ?? '—'} />
          <Row label="Rule fixture" value={result.courier_fixture ?? '—'} note="SYNTHETIC FIXTURE — not a real courier contract" />
          <Row label="COD fee base" value={result.cod_fee_base ?? '—'} />
          <Row label="COD fee rate" value={result.cod_fee_rate_bp === null ? '—' : `${result.cod_fee_rate_bp} bp`} />
          <Row label="Expected collectible (E)" value={formatTaka(result.E)} note="NULL means it cannot be fixed from the given information." />
          <Row label="Reported collected (R)" value={formatTaka(result.R)} />
          <Row label="Expected delivery fee (D_e)" value={formatTaka(result.D_e)} />
          <Row label="Reported delivery fee (D_r)" value={formatTaka(result.D_r)} />
          <Row
            label="Contractual delivery fee (D_c)"
            value={formatTaka(result.D_c)}
            note={
              result.D_c_confirmed
                ? `Confirmed by merchant (evidence: ${result.D_c_evidence ?? '—'}).`
                : 'Not confirmed — no silent fallback to D_e or D_r.'
            }
          />
          <Row label="Expected COD fee (F_e)" value={formatTaka(result.F_e)} note="Scenario-only when the fee base is reported_collected." />
          <Row label="Reported-basis COD fee (F_R)" value={formatTaka(result.F_R)} />
          <Row label="Expected payout (P_e)" value={formatTaka(result.P_e)} note="Scenario-only when the fee base is reported_collected." />
          <Row label="Payout conditional on reported collection (P_cond)" value={formatTaka(result.P_cond)} />
          <Row label="Reported payout (P_r)" value={formatTaka(result.P_r)} />
        </tbody>
      </table>

      <div>
        <h3 className="text-sm font-semibold text-slate-900">
          Variances — shown separately, never summed
        </h3>
        <table className="mt-2 w-full text-left text-sm">
          <tbody>
            <Row label="C — collection variance" value={formatTaka(result.C)} note="Reported collected minus expected collectible." />
            <Row label="V_R — fee/tax variance (reported basis)" value={formatTaka(result.V_R)} note="Fee judgement uses this, never the expected basis." />
            <Row label="S — statement arithmetic residual" value={formatTaka(result.S)} note="Reported payout minus (reported collected minus reported deductions). The statement's own inconsistency." />
            <Row label="V_cond — contractual payout variance" value={formatTaka(result.V_cond)} note="Decides the contractual payout status, on its own." />
            <Row label="V_total — scenario total variance" value={formatTaka(result.V_total)} note="Scenario-only (reported_collected base). Never triggers a contractual payout mismatch." />
          </tbody>
        </table>
        <p className="mt-2 text-xs text-slate-500">
          Contractual payout status: <strong>{result.contractual_payout_status}</strong>
          {' · '}Data quality: <strong>{result.data_quality_status}</strong>
        </p>
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
                <Badge variant={f.kind === 'data_quality' ? 'warning' : 'danger'}>
                  {f.kind === 'data_quality' ? 'data quality' : 'financial'}
                </Badge>
                <code className="rounded bg-slate-100 px-1 text-xs">{f.code}</code>
                {f.basis && <span className="text-xs text-slate-500">{f.basis}</span>}
                {f.amount_poisha !== null && f.amount_poisha !== undefined && (
                  <span className="font-mono text-xs">{formatTaka(f.amount_poisha)}</span>
                )}
              </div>
              <p className="mt-1 text-slate-600">{f.message}</p>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
