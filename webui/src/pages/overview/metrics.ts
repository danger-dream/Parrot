// Aggregate-metric helpers for the overview, same formulas as Telegram's statistics summary
// (`stats_menu._section_overall` / `_section_overall_compact` / `_summary_dim_block`).
// The backend now ships the derived values (docs/15-webui.md R3-5); these read them and only fall
// back to computing when a value is absent.
import type { StatsMetricData } from '@/api/types'
import { pct as pctText, tps as tpsText } from '@/utils/format'
export { pctText, tpsText }

export type Metrics = StatsMetricData

/** ↑ total prompt = non-cached input + cache write + cache read (TG `total_prompt_tokens`). */
export function promptOf(m: Metrics | null | undefined): number {
  if (!m) return 0
  return m.promptTokens || (m.inputTokens || 0) + (m.cacheCreationTokens || 0) + (m.cacheReadTokens || 0)
}

/** TG `fmt_cache_phrase(cache_read, prompt)`: cache read share of the full prompt. */
export function cacheShare(m: Metrics | null | undefined): number | null {
  if (!m) return null
  if (m.cacheReadRatio != null) return m.cacheReadRatio
  const p = promptOf(m)
  return p > 0 ? (m.cacheReadTokens || 0) / p : null
}

export function successRateOf(m: Metrics | null | undefined): number | null {
  if (!m) return null
  return m.successRate ?? rate(m.successCount, m.total)
}

type Outcomes = { total: number; successCount: number; errorCount: number; pendingCount?: number; cancelledCount?: number }

/** Client-cancelled requests. Summaries carry pendingCount, comparisons cancelledCount; the other is the remainder. */
export function cancelledOf(m: Outcomes | null | undefined): number {
  if (!m) return 0
  return m.cancelledCount ?? Math.max(0, m.total - m.successCount - m.errorCount - (m.pendingCount || 0))
}
export function pendingOf(m: Outcomes | null | undefined): number {
  if (!m) return 0
  if (m.pendingCount != null) return m.pendingCount
  // Without either count the remainder is already reported as cancelled.
  return m.cancelledCount != null ? Math.max(0, m.total - m.successCount - m.errorCount - m.cancelledCount) : 0
}

/** "成功 6,305 · 失败 33 · 取消 24 · 进行中 1": every request of `total` accounted for. */
export function outcomeLine(m: Outcomes): string {
  const parts = [`成功 ${m.successCount.toLocaleString()}`, `失败 ${m.errorCount.toLocaleString()}`]
  const cancelled = cancelledOf(m)
  const pending = pendingOf(m)
  if (cancelled) parts.push(`取消 ${cancelled.toLocaleString()}`)
  if (pending) parts.push(`进行中 ${pending.toLocaleString()}`)
  return parts.join(' · ')
}

/** 命中请求 / 成功请求. */
export function cacheHitRateOf(m: Metrics | null | undefined): number | null {
  if (!m) return null
  return m.cacheHitRate ?? rate(m.cacheHitRequests, m.successCount)
}

export function affinityRateOf(m: Metrics | null | undefined): number | null {
  if (!m) return null
  return m.affinityHitRate ?? rate(m.affinityHits, m.total)
}

export function rate(part: number, whole: number): number | null {
  return whole > 0 ? part / whole : null
}


