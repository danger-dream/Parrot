import type { Schemas } from '@/api/client'
import { channelName } from '@/stores/catalog'
import { goLogs, openApiKey, openChannel, openModel, type LogFilter } from '@/links'
import { pct, tps } from '@/utils/format'
export { pct, tps }

export type Period = 'today' | '3d' | '7d' | 'month' | 'lifetime'
export type Dim = 'model' | 'channel' | 'apiKey'
/** Deep-link `metric`: which figure the page leads with. */
export type Focus = 'requests' | 'cost' | 'latency' | 'cache'
export type Metric = Schemas['StatsMetricData']

export const PERIODS: { value: Period; label: string }[] = [
  { value: 'today', label: '今天' },
  { value: '3d', label: '3 天' },
  { value: '7d', label: '7 天' },
  { value: 'month', label: '本月' },
  { value: 'lifetime', label: '累计' },
]

export const PERIOD_LONG: Record<Period, string> = {
  today: '今天',
  '3d': '最近 3 天',
  '7d': '最近 7 天',
  month: '本月',
  lifetime: '累计',
}

export const DIMS: { value: Dim; label: string }[] = [
  { value: 'model', label: '按模型' },
  { value: 'channel', label: '按渠道' },
  { value: 'apiKey', label: '按 Key' },
]

export const FOCI: { value: Focus; label: string }[] = [
  { value: 'requests', label: '请求数' },
  { value: 'cost', label: '金额' },
  { value: 'latency', label: '首字耗时' },
  { value: 'cache', label: '缓存命中' },
]

export function parsePeriod(v: unknown): Period | null {
  return PERIODS.some((p) => p.value === v) ? (v as Period) : null
}
export function parseDim(v: unknown): Dim | null {
  return DIMS.some((d) => d.value === v) ? (v as Dim) : null
}
export function parseFocus(v: unknown): Focus | null {
  return FOCI.some((f) => f.value === v) ? (v as Focus) : null
}

const BJT_OFFSET = 8 * 3600_000

/** Start of the statistics window in epoch ms (same rules as the backend's period_since). */
export function periodStart(period: Period, now = Date.now()): number | undefined {
  if (period === 'lifetime') return undefined
  if (period === '3d') return now - 3 * 86400_000
  if (period === '7d') return now - 7 * 86400_000
  const bjt = new Date(now + BJT_OFFSET)
  if (period === 'month') return Date.UTC(bjt.getUTCFullYear(), bjt.getUTCMonth(), 1) - BJT_OFFSET
  return Date.UTC(bjt.getUTCFullYear(), bjt.getUTCMonth(), bjt.getUTCDate()) - BJT_OFFSET
}

export function totalTokens(m: Partial<Metric>): number {
  return (m.inputTokens ?? 0) + (m.outputTokens ?? 0) + (m.cacheReadTokens ?? 0) + (m.cacheCreationTokens ?? 0)
}

/** Cache hit rate as in the Telegram stats: successful requests that read from the cache. */
export function cacheRate(m: Partial<Metric>): number | null {
  const s = m.successCount ?? 0
  return s ? (m.cacheHitRequests ?? 0) / s : null
}

export function successRate(m: Partial<Metric>): number | null {
  const t = m.total ?? 0
  return t ? (m.successCount ?? 0) / t : null
}



export const UNKNOWN_KEYS = new Set(['?', '', '__other__'])

export function entityName(dim: Dim, key: string): string {
  if (key === '__other__') return '其他'
  if (UNKNOWN_KEYS.has(key)) return '（未记录）'
  return dim === 'channel' ? channelName(key) : key
}

export function openEntity(dim: Dim, key: string): void {
  if (UNKNOWN_KEYS.has(key)) return
  if (dim === 'model') openModel(key)
  else if (dim === 'channel') openChannel(key)
  else openApiKey(key)
}

export function logsFor(dim: Dim, key: string, extra: LogFilter = {}): void {
  const filter: LogFilter = { ...extra }
  if (!UNKNOWN_KEYS.has(key)) {
    if (dim === 'model') filter.model = key
    else if (dim === 'channel') filter.channel = key
    else filter.apiKey = key
  }
  goLogs(filter)
}

export const FAMILY_LABEL: Record<string, string> = {
  anthropic: 'Anthropic 家族',
  openai: 'OpenAI 家族',
  gemini: 'Gemini 家族',
  google: 'Google 家族',
}

export const FAMILY_HINT: Record<string, string> = {
  anthropic: 'Claude 系模型（Anthropic 协议上游）',
  openai: 'GPT、Grok、Cursor 等（OpenAI 协议上游）',
}

export function familyLabel(key: string): string {
  return FAMILY_LABEL[key] || `${key.charAt(0).toUpperCase()}${key.slice(1)} 家族`
}
