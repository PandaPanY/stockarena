/**
 * 策略规格（与后端 backend/app/quant/spec.py 的结构和默认值一致）：表单编辑时先补全默认值，
 * 保存 / 试算时原样提交，由后端最终校验。
 */
import type { FactorInfo } from '@/api/quant'

export type Op = '>' | '>=' | '<' | '<='

export interface FactorTerm {
  name: string
  params: Record<string, number>
  weight: number
  direction: 'high' | 'low'
  neutralize: 'none' | 'industry'
}

export interface FilterRule {
  name: string
  params: Record<string, number>
  op: Op
  value: number
}

export interface SpecForm {
  universe: {
    types: ('stock' | 'etf')[]
    symbols: string[]
    exclude: string[]
    exclude_st: boolean
    min_listed_days: number
    min_amount: number
    industries: string[]
    exclude_industries: string[]
  }
  factors: FactorTerm[]
  filters: FilterRule[]
  selection: { top_n: number; buffer: number; max_per_industry: number | null }
  weighting: { method: 'equal' | 'score' | 'inverse_vol'; vol_window: number; max_weight: number }
  rebalance: { every_days: number; min_trade_pct: number }
  timing: {
    enabled: boolean
    name: string
    params: Record<string, number>
    op: Op
    value: number
    exposure_off: number
    defensive: string | null
  }
  risk: { exposure: number; stop_loss: number | null; trailing_stop: number | null }
}

export const MAX_FACTORS = 6
export const MAX_FILTERS = 6
export const OPS: Op[] = ['>', '>=', '<', '<=']

function defaults(): SpecForm {
  return {
    universe: {
      types: ['stock'],
      symbols: [],
      exclude: [],
      exclude_st: true,
      min_listed_days: 120,
      min_amount: 0.5,
      industries: [],
      exclude_industries: [],
    },
    factors: [],
    filters: [],
    selection: { top_n: 10, buffer: 0, max_per_industry: null },
    weighting: { method: 'equal', vol_window: 60, max_weight: 0.2 },
    rebalance: { every_days: 5, min_trade_pct: 0.01 },
    timing: {
      enabled: false,
      name: 'ma_bias',
      params: { window: 60 },
      op: '>',
      value: 0,
      exposure_off: 0,
      defensive: null,
    },
    risk: { exposure: 0.98, stop_loss: null, trailing_stop: null },
  }
}

/** 空白模板：一个动量因子、持有 20 只（股票策略默认最少持仓 20）、每 20 个交易日调仓 */
export function blankSpec(): SpecForm {
  const s = defaults()
  s.factors = [
    { name: 'momentum', params: { window: 120, skip: 20 }, weight: 1, direction: 'high', neutralize: 'none' },
  ]
  s.selection.top_n = 20
  s.weighting.max_weight = 0.1
  s.rebalance.every_days = 20
  return s
}

export function defaultParams(f: FactorInfo | undefined): Record<string, number> {
  return Object.fromEntries(Object.entries(f?.params ?? {}).map(([k, p]) => [k, p.default]))
}

type Obj = Record<string, unknown>
/** 深拷贝（structuredClone 不能复制 Vue 的响应式代理） */
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)

/** 把任意规格（可能只写了部分字段）补全成完整的表单数据 */
export function toForm(spec: Obj | null | undefined): SpecForm {
  const out = defaults()
  const src = spec ?? {}
  for (const key of ['universe', 'selection', 'weighting', 'rebalance', 'timing', 'risk'] as const) {
    if (isObj(src[key])) Object.assign(out[key], clone(src[key]))
  }
  out.factors = (Array.isArray(src.factors) ? src.factors : []).map((f: Obj) => ({
    name: String(f.name ?? ''),
    params: { ...((f.params as Record<string, number>) ?? {}) },
    weight: Number(f.weight ?? 1),
    direction: f.direction === 'low' ? 'low' : 'high',
    neutralize: f.neutralize === 'industry' ? 'industry' : 'none',
  }))
  out.filters = (Array.isArray(src.filters) ? src.filters : []).map((f: Obj) => ({
    name: String(f.name ?? ''),
    params: { ...((f.params as Record<string, number>) ?? {}) },
    op: (OPS.includes(f.op as Op) ? f.op : '>') as Op,
    value: Number(f.value ?? 0),
  }))
  return out
}

/** 表单 → 提交给后端的规格（去掉未启用的择时细节，保持规格简洁） */
export function fromForm(form: SpecForm): Obj {
  const s = clone(form) as unknown as Obj
  if (!form.timing.enabled) s.timing = { enabled: false }
  return s
}
