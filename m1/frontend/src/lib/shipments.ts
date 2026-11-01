/**
 * Shipments & orders (M8) — display labels and the V2.1 expected
 * collectible rule, in plain language. No figures are invented here:
 * every amount shown comes from the backend, and a NULL expected
 * collectible is shown as unknown, never as zero.
 */

import type { NeedsReviewReason, OutcomeSource, ShipmentOutcome } from './api'

/** Bangla explanation for each shipment outcome. */
export const OUTCOME_BANGLA: Record<ShipmentOutcome, string> = {
  delivered: 'ডেলিভারি হয়েছে',
  partially_delivered: 'আংশিক ডেলিভারি হয়েছে',
  returned: 'ফেরত এসেছে',
  cancelled: 'বাতিল হয়েছে',
  unknown: 'অজানা',
  in_transit: 'পথে আছে',
}

/** Bangla explanation for each outcome source. */
export const OUTCOME_SOURCE_BANGLA: Record<OutcomeSource, string> = {
  merchant_confirmed: 'বিক্রেতা নিশ্চিত করেছেন',
  statement: 'কুরিয়ার বিবৃতি থেকে',
  system_inferred_label_only: 'সিস্টেমের অনুমান (লেবেল মাত্র)',
}

/**
 * Plain-language explanation of why the backend flagged a shipment
 * as needs_review. English first, Bangla alongside.
 */
export const NEEDS_REVIEW_REASON: Record<NeedsReviewReason, { en: string; bn: string }> = {
  outcome_not_confirmed: {
    en: 'The outcome is not confirmed yet, so no collectible amount can be fixed.',
    bn: 'ফলাফল এখনো নিশ্চিত হয়নি, তাই আদায়ের টাকা ঠিক করা যায়নি।',
  },
  order_cod_unknown: {
    en: 'The order COD is unknown, so a delivered shipment has no amount to fall back on.',
    bn: 'অর্ডারের COD অজানা, তাই ডেলিভারি হওয়া শিপমেন্টের কোনো টাকা ধরা যায়নি।',
  },
  split_amount_differs_from_order_total: {
    en: 'The stated amount to collect differs from the order total — a split, held for review rather than silently assumed.',
    bn: 'বলা আদায়ের টাকা অর্ডারের মোট থেকে আলাদা — এটি একটি ভাগ, নীরবে ধরে নেওয়া হয়নি, যাচাইয়ের জন্য রাখা হয়েছে।',
  },
  partial_amount_not_documented: {
    en: 'A partial delivery needs both the partial amount and an evidence reference — one without the other fixes nothing.',
    bn: 'আংশিক ডেলিভারির জন্য আংশিক টাকা ও প্রমাণের রেফারেন্স দুটোই লাগে — একটি ছাড়া অন্যটি কিছু ঠিক করে না।',
  },
}

/**
 * Plain-language explanation of the V2.1 outcome rule: what the
 * expected collectible COD is for each confirmed shipment outcome.
 * English first, Bangla alongside.
 */
export const OUTCOME_COLLECTIBLE_RULE: Record<ShipmentOutcome, { en: string; bn: string }> = {
  delivered: {
    en: 'Delivered — the full amount was to be collected.',
    bn: 'ডেলিভারি হয়েছে — পুরো টাকা আদায়ের কথা ছিল।',
  },
  partially_delivered: {
    en: 'Partially delivered — only a documented partial amount counts (amount plus an evidence reference); without both, the expected collectible is unknown (NULL), never guessed.',
    bn: 'আংশিক ডেলিভারি হয়েছে — শুধু নথিভুক্ত আংশিক টাকাই গণ্য হয় (টাকা ও প্রমাণের রেফারেন্স দুটোই লাগে); তা না থাকলে প্রত্যাশিত আদায় অজানা (NULL), অনুমান করা হয় না।',
  },
  returned: {
    en: 'Confirmed return — nothing was to be collected (verified ৳0.00).',
    bn: 'ফেরত নিশ্চিত — কিছুই আদায়ের কথা ছিল না (যাচাইকৃত ৳০.০০)।',
  },
  cancelled: {
    en: 'Confirmed cancellation — nothing was to be collected (verified ৳0.00).',
    bn: 'বাতিল নিশ্চিত — কিছুই আদায়ের কথা ছিল না (যাচাইকৃত ৳০.০০)।',
  },
  unknown: {
    en: 'Outcome unknown — the expected collectible is unknown (NULL), never assumed to be the full order COD.',
    bn: 'ফলাফল অজানা — প্রত্যাশিত আদায় অজানা (NULL), পুরো অর্ডারের COD ধরে নেওয়া হয় না।',
  },
  in_transit: {
    en: 'Still in transit — the expected collectible is unknown (NULL) until the outcome is confirmed.',
    bn: 'এখনো পথে আছে — ফলাফল নিশ্চিত না হওয়া পর্যন্ত প্রত্যাশিত আদায় অজানা (NULL)।',
  },
}

/** The outcome options the create-shipment form offers. */
export const SHIPMENT_OUTCOMES: ShipmentOutcome[] = [
  'delivered',
  'partially_delivered',
  'returned',
  'cancelled',
  'unknown',
  'in_transit',
]
