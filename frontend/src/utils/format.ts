const MARKET_LABELS: Record<string, string> = { CN: 'A股', HK: '港股', US: '美股' }

export function marketLabel(market: string): string {
  return MARKET_LABELS[market] ?? market
}

/** 把 ISO 时间格式化为指定时区的本地时间：2026-10-01 09:30:00 */
export function formatDateTime(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return '-'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '-'
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date)
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')}`
}

/** 0.1234 → "+12.34%" */
export function formatPercent(value: number | null | undefined, digits = 2, withSign = true): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-'
  const text = `${(value * 100).toFixed(digits)}%`
  return withSign && value > 0 ? `+${text}` : text
}

/** 金额千分位。后端金额以字符串传输（Decimal），前端仅用于展示 */
export function formatMoney(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = typeof value === 'string' ? Number(value) : value
  if (Number.isNaN(num)) return '-'
  return num.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
