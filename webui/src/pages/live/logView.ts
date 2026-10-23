// Request-log view helpers shared by the live list, history list and request detail.
// `RequestLogData` carries every fact the Telegram list card (`ui.fmt_log_entry_*`) and detail page
// (`logs_menu._render_detail`) shows (docs/15-webui.md R3-2); these helpers turn them into display text.
import { effortLabel } from '@/utils/effort'
import type { Schemas } from '@/api/client'
import type { RequestLogData } from '@/api/types'
import { channelName } from '@/stores/catalog'
import { channelProvider } from '@/utils/providers'
import { compact, TICKS_PER_USD } from '@/utils/format'

export type LogRow = RequestLogData
type ModelConflict = Schemas['RequestLogModelConflictData']

export type Json = Record<string, unknown>

// ---------- identity ----------

export function rowChannelName(r: LogRow): string {
  return r.channelName || channelName(r.channelId)
}

export function rowProvider(r: LogRow): string {
  return channelProvider(r.channelId, r.channelProvider)
}

/** TG `_log_model_display`: requested → final when they differ. */
export function modelChanged(r: Pick<LogRow, 'requestedModel' | 'finalModel'>): boolean {
  return Boolean(r.finalModel && r.requestedModel && r.finalModel !== r.requestedModel)
}

/** TG `_INGRESS_TAG`: anthropic is the default and gets no tag. */
export function ingressTag(r: Pick<LogRow, 'protocol' | 'ingressProtocol'>): string {
  const p = r.ingressProtocol || r.protocol || ''
  return ({ chat: 'chat', responses: 'response', responses_ws: 'response' } as Record<string, string>)[p] || ''
}

/** TG `_log_transport_tag`: WS (ingress websocket) / ↑WS (upstream websocket only). */
export function wsTag(r: LogRow): string {
  if (r.websocket === 'ingress' || r.ingressProtocol === 'responses_ws' || r.protocol === 'responses_ws') return 'WS'
  if (r.websocket === 'upstream') return '↑WS'
  return ''
}

export function effortText(v: string | null | undefined): string {
  return v ? effortLabel(v) : ''
}

/** TG `log_fast_mode_badge`. */
export function speedBadge(r: Pick<LogRow, 'speedMode' | 'serviceTier'>): string {
  if (r.speedMode === 'ultrafast') return 'Ultrafast'
  if (r.speedMode === 'fast') return 'Fast'
  if (r.speedMode === 'tier') return `Tier ${r.serviceTier || ''}`.trim()
  return ''
}

// ---------- numbers ----------

/** Full prompt (non-cached input + cache write + cache read), TG `prompt_total_from_row`. */
export function promptTokens(r: LogRow): number {
  return r.promptTokens || (r.inputTokens || 0) + (r.cacheCreationTokens || 0) + (r.cacheReadTokens || 0)
}

export function cacheRatio(r: LogRow): number | null {
  if (r.cacheReadRatio != null) return r.cacheReadRatio
  const p = promptTokens(r)
  return p > 0 && r.cacheReadTokens ? r.cacheReadTokens / p : null
}

/** TG `calc_row_tps`. */
export function rowTps(r: LogRow): number | null {
  if (r.tokensPerSecond != null) return r.tokensPerSecond
  if (r.status !== 'success' || !r.outputTokens) return null
  const total = r.durationMilliseconds
  const first = r.firstTokenMilliseconds
  if (!total || total <= 0) return null
  if (r.isStream && first != null && total > first) return (r.outputTokens * 1000) / (total - first)
  if (r.isStream === false) return (r.outputTokens * 1000) / total
  return null
}

export function tps(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K t/s`
  if (v >= 99.95) return `${Math.round(v)} t/s`
  return `${v.toFixed(1)} t/s`
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—'
  let text = (v * 100).toFixed(digits)
  // 99.96% must not read as a full 100%.
  if (v < 1 && Number(text) >= 100) text = (100 - 10 ** -digits).toFixed(digits)
  return `${text}%`
}

/** TG `fmt_usd(decimal_places=3)` — exact amount, half-up. */
export function usdExact(ticks: number | null | undefined, places = 3): string {
  const v = Math.max(0, Number(ticks || 0)) / TICKS_PER_USD
  return `$${v.toLocaleString('en-US', { minimumFractionDigits: places, maximumFractionDigits: places })}`
}

/** TG `_fmt_bytes`. */
export function tgBytes(n: number | null | undefined): string {
  const v = Math.max(0, Number(n || 0))
  if (v < 1024) return `${v}B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)}KB`
  return `${(v / (1024 * 1024)).toFixed(1)}MB`
}

export function tok(n: number | null | undefined): string {
  return compact(n ?? 0)
}

/** Whether a failed/cancelled row still carries a billed amount (TG shows「计费」then). */
export function hasBilling(r: LogRow): boolean {
  if (r.status === 'success') return true
  const b = r.billing
  return Boolean(b && (b.costedSuccess > 0 || b.unpricedSuccess > 0))
}

// ---------- errors ----------

/** TG `_extract_error_summary`: unwrap `HTTP nnn: {json error}`. */
export function errorSummary(raw: string | null | undefined): string {
  if (!raw) return '未知错误'
  let prefix = ''
  let jsonPart = raw
  if (raw.startsWith('HTTP ')) {
    const i = raw.indexOf(': ')
    if (i <= 0) return raw
    prefix = raw.slice(0, i)
    jsonPart = raw.slice(i + 2)
  }
  let obj: unknown
  try {
    obj = JSON.parse(jsonPart)
  } catch {
    return raw
  }
  const err = obj && typeof obj === 'object' ? (obj as Json).error : null
  if (err && typeof err === 'object') {
    const e = err as Json
    if (e.message) {
      const summary = e.type ? `${e.type}: ${e.message}` : String(e.message)
      return prefix ? `${prefix} — ${summary}` : summary
    }
  }
  return prefix ? `${prefix} — ${jsonPart}` : jsonPart
}

export function rowError(r: LogRow): string {
  return r.errorSummary || errorSummary(r.error)
}

// ---------- upstream observation (TG `log_upstream_observation_lines`) ----------

export function rerouteLabel(channelType: string | null | undefined): string {
  return channelType === 'oauth' ? '上游模型变更' : '与调用模型不一致'
}

export const SAFETY_REVIEW_TEXT = '本次请求被标记为需要额外安全审查（响应可能较慢）'

export function conflictLines(c: ModelConflict | null | undefined): string[] {
  if (!c) return []
  const out: string[] = []
  if (c.httpHeader?.length) out.push(`HTTP 模型头: ${c.httpHeader.join(' → ')}`)
  if (c.eventHeaders?.length) out.push(`事件模型头: ${c.eventHeaders.join(' → ')}`)
  if (c.bodyModels?.length) out.push(`正文模型: ${c.bodyModels.join(' → ')}`)
  return out
}

// ---------- execution chain (TG `_render_detail`) ----------

export function chainMark(outcome: string | null | undefined): 'ok' | 'search' | 'budget' | 'guard' | 'wait' | 'fail' {
  if (outcome === 'success') return 'ok'
  if (outcome === 'local_web_tool_round') return 'search'
  if (outcome === 'local_web_tool_limit') return 'budget'
  if (outcome === 'candidate_guard' || outcome === 'guard_error' || outcome === 'request_invalid') return 'guard'
  if (outcome === 'queue_wait' || outcome === 'pending') return 'wait'
  return 'fail'
}

const CHAIN_LABEL: Record<string, string> = {
  success: '成功',
  local_web_tool_round: '本地搜索轮',
  local_web_tool_limit: '搜索预算用尽',
  candidate_guard: '候选不兼容',
  guard_error: '请求拦截',
  request_invalid: '请求无效',
  queue_wait: '排队等待',
  pending: '进行中',
  http_error: 'HTTP 错误',
  connected: '已连接',
}

export function chainLabel(outcome: string | null | undefined): string {
  return (outcome && CHAIN_LABEL[outcome]) || outcome || '?'
}

/** TG `_attempt_outbound_model`: dispatch.outbound_model_id from the binding JSON. */
export function outboundModel(attempt: Json): string {
  const raw = attempt.bindingJson
  if (typeof raw !== 'string' || !raw) return ''
  try {
    const payload = JSON.parse(raw) as Json
    const dispatch = payload?.dispatch as Json | undefined
    return String(dispatch?.outbound_model_id ?? '').trim()
  } catch {
    return ''
  }
}

export interface Metric {
  label: string
  value: number
}

/** TG `_detail_metric_line`: 连接 · 首字 · 空闲 · 总计. */
export function timingMetrics(
  row: Json,
  keys: { connect: string; first: string; idle: string; total: string },
  firstApplicable = true,
): Metric[] {
  const out: Metric[] = []
  const pairs: [string, string][] = [
    ['连接', keys.connect],
    ['首字', keys.first],
    ['空闲', keys.idle],
    ['总计', keys.total],
  ]
  for (const [label, key] of pairs) {
    if (label === '首字' && !firstApplicable) continue
    const v = row[key]
    if (typeof v === 'number') out.push({ label, value: v })
  }
  return out
}

export const ATTEMPT_KEYS = { connect: 'connectMs', first: 'firstByteMs', idle: 'idleMs', total: 'totalMs' }

/** TG `_detail_stage_line` on camelCase attempt/round rows (one reliable TLS phase only). */
export function stageMetrics(row: Json): Metric[] {
  const keys: [string, string][] = [
    ['DNS', 'dnsMs'],
    ['TCP', 'tcpMs'],
    ['代理 TCP', 'proxyTcpMs'],
    ['代理隧道', 'proxyTunnelMs'],
  ]
  keys.push(row.targetTlsMs != null ? ['目标 TLS', 'targetTlsMs'] : ['TLS', 'tlsMs'])
  keys.push(['WS 握手', 'wsHandshakeMs'], ['请求上传', 'requestUploadMs'], ['等待响应头', 'responseHeadersWaitMs'], ['头后首 Body', 'responseBodyFirstByteWaitMs'])
  const out: Metric[] = []
  for (const [label, key] of keys) {
    const v = row[key]
    if (typeof v === 'number') out.push({ label, value: v })
  }
  return out
}

/** Request-level stages from `log.stageMilliseconds` (round-3 names). */
export function requestStageMetrics(stages: Record<string, number | null> | null | undefined): Metric[] {
  if (!stages) return []
  const order: [string, string][] = [
    ['DNS', 'dns'],
    ['TCP', 'tcp'],
    ['代理 TCP', 'proxyTcp'],
    ['代理隧道', 'proxyTunnel'],
    ['目标 TLS', 'targetTls'],
    ['TLS', 'tls'],
    ['WS 握手', 'wsHandshake'],
    ['请求上传', 'requestUpload'],
    ['等待响应头', 'responseHeadersWait'],
    ['头后首 Body', 'responseBodyFirstByteWait'],
  ]
  const out: Metric[] = []
  for (const [label, key] of order) {
    const v = stages[key]
    if (typeof v === 'number') out.push({ label, value: v })
  }
  return out
}

// ---------- billing (TG `_billing_detail_lines`) ----------

const BILLING = [
  ['inputTokens', 'input_per_token', '输入'],
  ['outputTokens', 'output_per_token', '输出'],
  ['cacheCreationTokens', 'cache_write_per_token', '缓存写入'],
  ['cacheReadTokens', 'cache_read_per_token', '缓存读取'],
] as const
const BILLING_TTL = [
  ['inputTokens', 'input_per_token', '输入'],
  ['outputTokens', 'output_per_token', '输出'],
  ['cacheCreation5mTokens', 'cache_write_5m_per_token', '缓存写入 5m'],
  ['cacheCreation1hTokens', 'cache_write_1h_per_token', '缓存写入 1h'],
  ['cacheReadTokens', 'cache_read_per_token', '缓存读取'],
] as const

function perMillion(price: unknown): string | null {
  const v = Number(price)
  if (price === null || price === undefined || !Number.isFinite(v) || v < 0) return null
  const m = Math.round(v * 1_000_000 * 1e9) / 1e9
  return `$${m.toLocaleString('en-US', { maximumFractionDigits: 9 })}/M`
}

/** Estimated-cost formula of one billed attempt: "输入 1,234 × $3/M + …". */
export function costFormula(attempt: Json): string {
  if (String(attempt.costSource || '') !== 'estimated') return ''
  let snapshot: Json
  try {
    snapshot = JSON.parse(String(attempt.pricingSnapshotJson || '')) as Json
  } catch {
    return ''
  }
  if (!snapshot || typeof snapshot !== 'object') return ''
  const split = attempt.cacheCreation5mTokens != null && attempt.cacheCreation1hTokens != null
  const terms: string[] = []
  for (const [tokenKey, priceKey, label] of split ? BILLING_TTL : BILLING) {
    const tokens = Number(attempt[tokenKey] || 0)
    const price = perMillion(snapshot[priceKey])
    if (tokens > 0 && price && price !== '$0/M') terms.push(`${label} ${tokens.toLocaleString('en-US')} × ${price}`)
  }
  return terms.join(' + ')
}

export function pricedAttempts(list: Json[]): Json[] {
  return list.filter((a) => a.costTicks !== null && a.costTicks !== undefined && ['actual', 'estimated'].includes(String(a.costSource || '')))
}
