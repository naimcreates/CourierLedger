// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import '@testing-library/jest-dom/vitest'
import { formatTaka, STATUS_BANGLA } from './lib/demo'

describe('demo helpers', () => {
  it('formats integer poisha as taka', () => {
    expect(formatTaka(125000)).toBe('৳1,250.00')
    expect(formatTaka(0)).toBe('৳0.00')
    expect(formatTaka(null)).toBe('—')
  })

  it('has a Bangla label for every reconciliation status', () => {
    expect(STATUS_BANGLA.matched).toBe('মিলেছে')
    expect(STATUS_BANGLA.mismatch).toBe('অমিল')
    expect(STATUS_BANGLA.needs_review).toBe('যাচাই প্রয়োজন')
    expect(STATUS_BANGLA.open).toBe('খোলা')
    expect(STATUS_BANGLA.matched_with_tolerance).toBe('সহনশীলতার মধ্যে মিলেছে')
  })
})

