import { api } from '@/api/client'
import { compact, pctValue, tps, usd } from '@/utils/format'
import type { StatsMetricData } from '@/api/types'
export { tps }

export type StatsPeriod = 'today' | '3d' | '7d' | 'month' | 'lifetime'
export const MODEL_PERIODS: { value: StatsPeriod; label: string }[] = [
  { value: 'today', label: '今天' },
  { value: '7d', label: '7 天' },
  { value: 'month', label: '本月' },
  { value: 'lifetime', label: '全部' },
]

export interface ModelRow {
  key: string
  metrics: StatsMetricData
}

/** Per-model stats of one account (TG 详情「按模型 Top 3」): getStatsBreakdown with a channel filter. */
export async function loadAccountModelStats(accountId: string, period: StatsPeriod): Promise<ModelRow[]> {
  const res = await api.get<ModelRow[]>('/stats/breakdown', {
    dimension: 'model',
    period,
    channel: `oauth:${accountId}`,
    pageSize: 20,
  })
  return res.data
}

/** Structural: works for StatsMetricData, OAuthLocalUsageStatsData and OAuthLocalStatsData. */
type AnyStats = {
  promptTokens?: number | null
  inputTokens?: number | null
  cacheCreationTokens?: number | null
  cacheReadTokens?: number | null
  cacheReadRatio?: number | null
} | null | undefined

/** TG prompt_total (input + cache creation + cache read); the server sends it as promptTokens. */
export function promptTokens(m: AnyStats): number {
  if (!m) return 0
  return m.promptTokens ?? (m.inputTokens || 0) + (m.cacheCreationTokens || 0) + (m.cacheReadTokens || 0)
}

/** Cache-read share of prompt tokens in percent (server: cacheReadRatio 0–1). */
export function cacheShare(m: AnyStats): number | null {
  if (!m) return null
  if (m.cacheReadRatio !== null && m.cacheReadRatio !== undefined) return m.cacheReadRatio > 0 ? m.cacheReadRatio * 100 : null
  const p = promptTokens(m)
  return p && m.cacheReadTokens ? (m.cacheReadTokens / p) * 100 : null
}


type LocalUsage = AnyStats & {
  outputTokens?: number | null
  averageTokensPerSecond?: number | null
  costTicks?: number | null
}

/** "↑37.0M ↓111.5K · 缓存 36.8M (99.4%) · 均 108 t/s · $11.226": what went through Parrot in a window. */
export function localUsageLine(l: LocalUsage | null | undefined): string {
  if (!l) return ''
  const prompt = promptTokens(l)
  const output = l.outputTokens || 0
  // An idle window still gets its line (all zeros) so every bar in a list has the same shape.
  const parts = [`↑${compact(prompt)} ↓${compact(output)}`]
  const share = cacheShare(l)
  parts.push(`缓存 ${compact(l.cacheReadTokens || 0)}${share != null ? `（${pctValue(share)}）` : ''}`)
  parts.push(`均 ${l.averageTokensPerSecond ? tps(l.averageTokensPerSecond) : '—'}`)
  parts.push(usd(l.costTicks || 0))
  return parts.join(' · ')
}
