// Totals shown above the request list, with the change against the previous period. Any time
// window and any combination of channel / model / API key goes through /stats/compare; only
// 全部 (lifetime) has no previous period and falls back to the per-dimension statistics, which
// can narrow by one dimension only. Status and free-text filters only narrow the list itself.
import { api } from '@/api/client'
import type { StatsBreakdownData, StatsSummaryData } from '@/api/types'
import { channelName } from '@/stores/catalog'
import { loadCompare, type Compare, type ComparePeriod } from '@/utils/compare'

export type Period = 'today' | '3d' | '7d' | 'month' | 'lifetime'

export const PERIODS: { value: Period; label: string }[] = [
  { value: 'today', label: '今天' },
  { value: '3d', label: '3 天' },
  { value: '7d', label: '7 天' },
  { value: 'month', label: '本月' },
  { value: 'lifetime', label: '全部' },
]

/** Start of a period in the same Beijing-time sense the statistics use. */
export function periodStart(p: Period, now = Date.now()): number | null {
  const offset = 8 * 3600_000
  const dayStart = Math.floor((now + offset) / 86400_000) * 86400_000 - offset
  if (p === 'today') return dayStart
  if (p === '3d') return now - 3 * 86400_000
  if (p === '7d') return now - 7 * 86400_000
  if (p === 'month') {
    const d = new Date(now + offset)
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) - offset
  }
  return null
}

export interface ScopeFilter {
  channel?: string
  model?: string
  apiKey?: string
}

/** The figures the tiles read; both the period statistics and /stats/compare carry them. */
export interface ScopeMetrics {
  total: number
  successCount: number
  errorCount: number
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheCreationTokens: number
  costTicks: number
  unpricedSuccess: number
  pendingCount?: number
  cancelledCount?: number
  averageFirstTokenMilliseconds?: number | null
  averageTotalMilliseconds?: number | null
  averageTokensPerSecond?: number | null
}

export interface ScopeResult {
  metrics: ScopeMetrics | null
  label: string
  partial: boolean
  /** Current against previous period; null for 全部. */
  compare: Compare | null
  period: ComparePeriod | null
}

export async function loadScope(period: Period | 'custom', f: ScopeFilter, range: { from?: string; to?: string } = {}): Promise<ScopeResult | null> {
  const dims: [('channel' | 'model' | 'apiKey'), string, string][] = []
  if (f.channel) dims.push(['channel', f.channel, `渠道 ${channelName(f.channel)}`])
  if (f.apiKey) dims.push(['apiKey', f.apiKey, `Key ${f.apiKey}`])
  if (f.model) dims.push(['model', f.model, `模型 ${f.model}`])
  const label = dims.length ? dims.map((d) => d[2]).join(' · ') : '全部请求'
  const filters = { channel: f.channel || null, model: f.model || null, apiKey: f.apiKey || null }
  if (period === 'custom') {
    if (!range.from) return null
    const compare = await loadCompare({ from: range.from, to: range.to || new Date().toISOString(), ...filters })
    return { metrics: compare.current, label, partial: false, compare, period: null }
  }
  if (period !== 'lifetime') {
    const compare = await loadCompare({ period, ...filters })
    return { metrics: compare.current, label, partial: false, compare, period }
  }
  if (!dims.length) {
    const res = await api.get<StatsSummaryData>('/stats/summary', { period })
    return { metrics: res.data.overall, label, partial: false, compare: null, period: null }
  }
  const [dimension, key, one] = dims[0]
  const res = await api.get<StatsBreakdownData[]>('/stats/breakdown', { dimension, period, sort: 'total', pageSize: 200 })
  const row = res.data.find((r) => r.key === key)
  return { metrics: row?.metrics ?? null, label: one, partial: dims.length > 1, compare: null, period: null }
}
