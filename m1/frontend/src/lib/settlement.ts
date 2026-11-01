/**
 * Settlement batches & bank receipts (M7) — display labels and money
 * helpers. No figures are invented here: every amount shown comes from
 * the backend, and a null variance is shown as a reason, never a number.
 */

import type { AllocationStatus, BankStatus, VarianceStatus } from './api'

/** Bangla explanation for each allocation status. */
export const ALLOCATION_STATUS_BANGLA: Record<AllocationStatus, string> = {
  unallocated: 'বরাদ্দ হয়নি',
  partially_allocated: 'আংশিক বরাদ্দ',
  fully_allocated: 'সম্পূর্ণ বরাদ্দ',
}

/** Bangla explanation for each bank status. */
export const BANK_STATUS_BANGLA: Record<BankStatus, string> = {
  no_receipt: 'রসিদ নেই',
  partial_receipt: 'আংশিক রসিদ',
  receipt_complete_unallocated: 'রসিদ সম্পূর্ণ, বরাদ্দ বাকি',
  fully_reconciled: 'সম্পূর্ণ মিলেছে',
}

/**
 * Plain-language reason shown when the backend returns no current
 * variance (null). Each reason says why the variance cannot be shown —
 * never a stale or invented number. English first, Bangla alongside.
 */
export const VARIANCE_STATUS_REASON: Record<VarianceStatus, { en: string; bn: string }> = {
  ok: {
    en: 'Variance calculated.',
    bn: 'ভ্যারিয়েন্স হিসাব করা হয়েছে।',
  },
  no_calculation: {
    en: 'No variance calculated yet — run “Calculate variance” once the batch is fully allocated and its receipts are complete.',
    bn: 'এখনো ভ্যারিয়েন্স হিসাব করা হয়নি — ব্যাচ সম্পূর্ণ বরাদ্দ হয়ে রসিদ সম্পূর্ণ হলে “Calculate variance” চাপুন।',
  },
  not_fully_allocated: {
    en: 'Cannot calculate yet — the batch is not fully allocated.',
    bn: 'হিসাব করা যায়নি — এখনো সম্পূর্ণ বরাদ্দ হয়নি।',
  },
  receipt_incomplete: {
    en: 'Cannot calculate yet — a receipt is not marked complete.',
    bn: 'হিসাব করা যায়নি — একটি রসিদ এখনো সম্পূর্ণ হিসেবে চিহ্নিত হয়নি।',
  },
  expected_changed: {
    en: 'The expected payout changed after the last calculation — calculate the variance again to get a current figure.',
    bn: 'শেষ হিসাবের পর প্রত্যাশিত পেমেন্ট বদলেছে — বর্তমান সংখ্যা পেতে আবার হিসাব করুন।',
  },
  input_set_changed: {
    en: 'The receipts or allocations changed after the last calculation — calculate the variance again to get a current figure.',
    bn: 'শেষ হিসাবের পর রসিদ বা বরাদ্দ বদলেছে — বর্তমান সংখ্যা পেতে আবার হিসাব করুন।',
  },
}

/**
 * Parse a user-typed taka amount (e.g. "1250.50") into integer poisha.
 * Returns null for an empty string (the field is optional) and throws a
 * plain-English Error for anything that is not a valid non-negative
 * amount with at most two decimal places.
 */
export function parseTakaToPoisha(input: string): number | null {
  const trimmed = input.trim()
  if (trimmed === '') return null
  if (trimmed.startsWith('-')) {
    throw new Error('Amount must be 0 or more.')
  }
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) {
    throw new Error('Enter an amount in taka, e.g. 1250.50 — up to two decimal places.')
  }
  const poisha = Math.round(Number(trimmed) * 100)
  if (!Number.isFinite(poisha) || poisha < 0) {
    throw new Error('Amount must be 0 or more.')
  }
  return poisha
}

/**
 * Validate the optional expected payout for the create-batch form.
 * Returns null when the field is blank (optional) or a valid
 * non-negative poisha amount; otherwise returns the error message.
 */
export function validateExpectedPayout(input: string): string | null {
  try {
    parseTakaToPoisha(input)
    return null
  } catch (err) {
    return err instanceof Error ? err.message : 'Invalid amount.'
  }
}

/**
 * Validate a required taka amount (receipts, allocations). Returns the
 * poisha value, or throws with a plain-English message.
 */
export function parseRequiredTakaToPoisha(input: string): number {
  const poisha = parseTakaToPoisha(input)
  if (poisha === null) throw new Error('Enter an amount in taka.')
  if (poisha <= 0) throw new Error('Amount must be more than ৳0.00.')
  return poisha
}
