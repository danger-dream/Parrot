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
  costedSuccess?: number | null
}

type WindowQuotaContext = {
  label?: string | null
  usedPercent?: number | null
  remainingPercent?: number | null
} | null | undefined

function isWeeklyWindowLabel(label: string | null | undefined): boolean {
  if (!label) return false
  return label.includes('7 天') || label.includes('每周') || label.includes('周额度') || /\b7d\b/i.test(label)
}

export function windowQuotaProjection(
  l: LocalUsage | null | undefined,
  item?: WindowQuotaContext,
): { tokens: number; costTicks: number | null } | null {
  if (!l || !item || !isWeeklyWindowLabel(item.label)) return null
  const used =
    typeof item.usedPercent === 'number' && Number.isFinite(item.usedPercent)
      ? item.usedPercent
      : typeof item.remainingPercent === 'number' && Number.isFinite(item.remainingPercent)
        ? 100 - item.remainingPercent
        : null
  if (used === null || used <= 0 || used > 100) return null
  const totalTokens = promptTokens(l) + (l.outputTokens || 0)
  if (totalTokens <= 0) return null
  const tokens = Math.round((totalTokens * 100) / used)
  const costRaw = l.costTicks || 0
  const costed = costRaw > 0 || (l.costedSuccess || 0) > 0
  const costTicks = costed ? Math.round((costRaw * 100) / used) : null
  return { tokens, costTicks }
}

/** "↑37.0M ↓111.5K · 缓存 36.8M (99.4%) · 均 108 t/s · $11.226": what went through Parrot in a window. */
export function localUsageLine(l: LocalUsage | null | undefined, item?: WindowQuotaContext): string {
  if (!l) return ''
  const prompt = promptTokens(l)
  const output = l.outputTokens || 0
  // An idle window still gets its line (all zeros) so every bar in a list has the same shape.
  const parts = [`↑${compact(prompt)} ↓${compact(output)}`]
  const share = cacheShare(l)
  parts.push(`缓存 ${compact(l.cacheReadTokens || 0)}${share != null ? `（${pctValue(share)}）` : ''}`)
  parts.push(`均 ${l.averageTokensPerSecond ? tps(l.averageTokensPerSecond) : '—'}`)
  parts.push(usd(l.costTicks || 0))
  const proj = windowQuotaProjection(l, item)
  if (proj) {
    parts.push(`7 天额度预测 ${compact(proj.tokens)}${proj.costTicks ? `（${usd(proj.costTicks)}）` : ''}`)
  }
  return parts.join(' · ')
}
