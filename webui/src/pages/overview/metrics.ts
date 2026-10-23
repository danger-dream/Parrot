// Aggregate-metric helpers for the overview, same formulas as Telegram's statistics summary
// (`stats_menu._section_overall` / `_section_overall_compact` / `_summary_dim_block`).
// The backend now ships the derived values (docs/15-webui.md R3-5); these read them and only fall
// back to computing when a value is absent.
import type { StatsMetricData } from '@/api/types'

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

export function pctText(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

export function tpsText(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K t/s`
  if (v >= 99.95) return `${Math.round(v)} t/s`
  return `${v.toFixed(1)} t/s`
}
