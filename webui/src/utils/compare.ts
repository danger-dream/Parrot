// Period-over-period change for metric tiles (GET /stats/compare). Both windows are aggregated by
// the backend with one set of rules and always cover the same elapsed time.
import { api, type Schemas } from '@/api/client'
import { compact, dateTime, integer, ms, usd } from '@/utils/format'

export type Compare = Schemas['StatsCompareData']
export type CompareMetrics = Schemas['CompareMetricsData']
export type ComparePeriod = 'today' | '3d' | '7d' | 'month'

export interface CompareQuery {
  period?: ComparePeriod
  from?: string | null
  to?: string | null
  model?: string | null
  channel?: string | null
  apiKey?: string | null
}

/** Now rounded down to the minute, so repeated detail-page refreshes share the server's cache. */
export function minuteNow(): string {
  return new Date(Math.floor(Date.now() / 60_000) * 60_000).toISOString()
}

export async function loadCompare(q: CompareQuery): Promise<Compare> {
  const params: Record<string, string> = {}
  for (const [k, v] of Object.entries(q)) if (v) params[k] = String(v)
  return (await api.get<Compare>('/stats/compare', params)).data
}

const PREVIOUS: Record<ComparePeriod, string> = { today: '昨天同一时段', '3d': '前 3 天', '7d': '前 7 天', month: '上月同期' }

/** "昨天同一时段（10-22 00:00 – 11:14）" — what the change is measured against. */
export function previousLabel(c: Compare, period?: ComparePeriod | null): string {
  const from = dateTime(c.previousFrom).slice(0, 11)
  const to = dateTime(c.previousTo).slice(0, 11)
  const range = from.slice(0, 5) === to.slice(0, 5) ? `${from} – ${to.slice(6)}` : `${from} – ${to}`
  return `${period ? PREVIOUS[period] : '前一段等长时间'}（${range}）`
}

/**
 * `flat` / `up` / `down` only say which way a quantity moved (grey); `good` / `bad` judge it.
 */
export interface Delta {
  text: string
  tone: 'flat' | 'up' | 'down' | 'good' | 'bad'
  title: string
}

const pctFmt = (v: number) => {
  const a = Math.abs(v)
  return a >= 1000 ? integer(Math.round(a)) : a.toFixed(1)
}

/** Relative change of a quantity; `better` judges the direction (omit for neutral counts). */
export function changeDelta(
  cur: number | null | undefined,
  prev: number | null | undefined,
  against: string,
  prevText: string,
  better?: 'higher' | 'lower',
): Delta | null {
  if (cur == null || prev == null) return null
  const title = `对比${against}：${prevText}`
  if (prev <= 0) return cur > 0 ? { text: '新增', tone: 'flat', title } : null
  const pct = ((cur - prev) / prev) * 100
  if (Math.abs(pct) < 0.05) return { text: '持平', tone: 'flat', title }
  const up = pct > 0
  const tone = better ? ((better === 'higher') === up ? 'good' : 'bad') : up ? 'up' : 'down'
  return { text: `${up ? '↗ +' : '↘ −'}${pctFmt(pct)}%`, tone, title }
}

/** Change of a ratio (0–1) in percentage points. */
export function pointDelta(
  cur: number | null | undefined,
  prev: number | null | undefined,
  against: string,
  better: 'higher' | 'lower' = 'higher',
): Delta | null {
  if (cur == null || prev == null) return null
  const title = `对比${against}：${(prev * 100).toFixed(2)}%`
  const pp = (cur - prev) * 100
  if (Math.abs(pp) < 0.05) return { text: '持平', tone: 'flat', title }
  const up = pp > 0
  return { text: `${up ? '↗ +' : '↘ −'}${Math.abs(pp).toFixed(1)} 个百分点`, tone: (better === 'higher') === up ? 'good' : 'bad', title }
}

// ---- derived values shared by every tile, so the same tile reads the same everywhere ----

export const promptOf = (m: CompareMetrics) => m.inputTokens + m.cacheReadTokens + m.cacheCreationTokens
export const tokensOf = (m: CompareMetrics) => promptOf(m) + m.outputTokens
export const successOf = (m: CompareMetrics) => (m.total ? m.successCount / m.total : null)
export const cacheOf = (m: CompareMetrics) => (promptOf(m) ? m.cacheReadTokens / promptOf(m) : null)
export const hitOf = (m: CompareMetrics) => (m.successCount ? m.cacheHitRequests / m.successCount : null)
export const affinityOf = (m: CompareMetrics) => (m.total ? m.affinityHits / m.total : null)

/** The standard deltas; tiles pick the ones they show. */
export function deltas(c: Compare | null, period?: ComparePeriod | null) {
  // Logs that do not reach back over the previous window would read as growth from zero.
  if (!c || !c.previousComplete) return {} as Partial<Record<'requests' | 'success' | 'errors' | 'tokens' | 'cache' | 'hit' | 'cost' | 'firstToken' | 'output' | 'prompt' | 'cacheRead' | 'cacheWrite' | 'tps' | 'affinity', Delta | null>>
  const a = previousLabel(c, period)
  const cur = c.current
  const prev = c.previous
  return {
    requests: changeDelta(cur.total, prev.total, a, `${integer(prev.total)} 次`),
    success: prev.total ? pointDelta(successOf(cur), successOf(prev), a) : null,
    errors: changeDelta(cur.errorCount, prev.errorCount, a, `${integer(prev.errorCount)} 次`, 'lower'),
    tokens: changeDelta(tokensOf(cur), tokensOf(prev), a, compact(tokensOf(prev))),
    prompt: changeDelta(promptOf(cur), promptOf(prev), a, compact(promptOf(prev))),
    output: changeDelta(cur.outputTokens, prev.outputTokens, a, compact(prev.outputTokens)),
    cacheRead: changeDelta(cur.cacheReadTokens, prev.cacheReadTokens, a, compact(prev.cacheReadTokens)),
    cacheWrite: changeDelta(cur.cacheCreationTokens, prev.cacheCreationTokens, a, compact(prev.cacheCreationTokens)),
    cache: prev.total ? pointDelta(cacheOf(cur), cacheOf(prev), a) : null,
    hit: prev.successCount ? pointDelta(hitOf(cur), hitOf(prev), a) : null,
    cost: changeDelta(cur.costTicks, prev.costTicks, a, usd(prev.costTicks)),
    firstToken: changeDelta(cur.averageFirstTokenMilliseconds, prev.averageFirstTokenMilliseconds, a, ms(prev.averageFirstTokenMilliseconds), 'lower'),
    tps: changeDelta(cur.averageTokensPerSecond, prev.averageTokensPerSecond, a, `${(prev.averageTokensPerSecond ?? 0).toFixed(1)} t/s`, 'higher'),
    affinity: prev.total ? pointDelta(affinityOf(cur), affinityOf(prev), a) : null,
  }
}

/** Sparkline values; buckets without requests repeat the previous ratio instead of dropping to 0. */
export function sparkOf(c: Compare | null, pick: (m: CompareMetrics) => number | null): number[] {
  if (!c) return []
  const out: number[] = []
  let last: number | null = null
  for (const p of c.points) {
    const v = pick(p)
    if (v != null) last = v
    out.push(last ?? 0)
  }
  return out
}

export function sparks(c: Compare | null) {
  return {
    requests: sparkOf(c, (p) => p.total),
    success: sparkOf(c, (p) => (p.total ? successOf(p)! * 100 : null)),
    tokens: sparkOf(c, tokensOf),
    prompt: sparkOf(c, promptOf),
    output: sparkOf(c, (p) => p.outputTokens),
    cacheRead: sparkOf(c, (p) => p.cacheReadTokens),
    cache: sparkOf(c, (p) => (promptOf(p) ? cacheOf(p)! * 100 : null)),
    hit: sparkOf(c, (p) => (p.successCount ? hitOf(p)! * 100 : null)),
    cost: sparkOf(c, (p) => p.costTicks),
    firstToken: sparkOf(c, (p) => p.averageFirstTokenMilliseconds ?? null),
    tps: sparkOf(c, (p) => p.averageTokensPerSecond ?? null),
    affinity: sparkOf(c, (p) => (p.total ? affinityOf(p)! * 100 : null)),
    errors: sparkOf(c, (p) => p.errorCount),
  }
}
