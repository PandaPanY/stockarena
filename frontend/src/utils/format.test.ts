import { describe, expect, it } from 'vitest'
import { formatDateTime, formatMoney, formatPercent, marketLabel } from './format'

describe('format', () => {
  it('formatDateTime 按市场时区显示', () => {
    expect(formatDateTime('2026-10-01T01:30:00+00:00', 'Asia/Shanghai')).toBe('2026-10-01 09:30:00')
    expect(formatDateTime('2026-07-01T13:30:00Z', 'America/New_York')).toBe('2026-07-01 09:30:00')
    expect(formatDateTime(null)).toBe('-')
    expect(formatDateTime('not-a-date')).toBe('-')
  })

  it('formatPercent 带符号', () => {
    expect(formatPercent(0.1234)).toBe('+12.34%')
    expect(formatPercent(-0.05)).toBe('-5.00%')
    expect(formatPercent(0)).toBe('0.00%')
    expect(formatPercent(null)).toBe('-')
  })

  it('formatMoney 千分位', () => {
    expect(formatMoney('1234567.891')).toBe('1,234,567.89')
    expect(formatMoney(100000)).toBe('100,000.00')
    expect(formatMoney('abc')).toBe('-')
  })

  it('marketLabel', () => {
    expect(marketLabel('CN')).toBe('A股')
    expect(marketLabel('XX')).toBe('XX')
  })
})
