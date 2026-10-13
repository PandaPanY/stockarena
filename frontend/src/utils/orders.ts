import type { OrderItem, Side } from '@/api/arena'

const TYPE_LABELS: Record<OrderItem['order_type'], string> = {
  market: '市价',
  limit: '限价',
  stop: '止损',
  take_profit: '止盈',
}

/** 订单类型与价格，如「限价 101」「止损 触发 90」 */
export function orderTypeText(o: OrderItem): string {
  const label = TYPE_LABELS[o.order_type] ?? o.order_type
  if (o.order_type === 'limit') return `${label} ${o.limit_price}`
  if (o.order_type === 'stop' || o.order_type === 'take_profit') return `${label} 触发 ${o.stop_price}`
  return label
}

/** 开仓单附带的止损止盈与有效期 */
export function orderExtraText(o: OrderItem): string {
  const parts: string[] = []
  if (o.attach_stop) parts.push(`附带止损 ${o.attach_stop}`)
  if (o.attach_take_profit) parts.push(`附带止盈 ${o.attach_take_profit}`)
  if (o.tif === 'gtc') parts.push(o.expires_on ? `长期有效至 ${o.expires_on}` : '长期有效')
  return parts.join('，')
}

const SIDE_LABELS: Record<Side, string> = { buy: '买入', sell: '卖出', short: '卖空', cover: '平空' }

/** 买卖方向文字 */
export function sideText(side: string): string {
  return SIDE_LABELS[side as Side] ?? side
}

/** 资金流出方向（买入、平空）用红色，流入方向（卖出、卖空）用绿色，与 K 线买卖点一致 */
export function sideClass(side: string): string {
  return side === 'buy' || side === 'cover' ? 'sa-up' : 'sa-down'
}
