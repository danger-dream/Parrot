import { reactive } from 'vue'
import { api, ApiError, type Schemas } from '@/api/client'
import { waitOperation, type Operation } from '@/api/operations'
import { errorDetail, errorTitle } from '@/utils/errors'

export type Channel = Schemas['ChannelData']
/** listChannels item: ChannelData plus the shared month snapshot (monthStats / monthStatsLoading). */
export type ChannelListItem = Schemas['ChannelListItemData']
export type ChannelDetailData = Schemas['ChannelDetailData']
export type Protocol = Schemas['ChannelProtocol']
export type Health = Schemas['ChannelHealth']
export type Catalog = Schemas['ChannelCatalogData']
export type Provider = Schemas['ChannelProviderData']
export type Preset = Schemas['ChannelPresetData']
export type ModelPair = { real: string; alias: string }
export type Tone = 'neutral' | 'blue' | 'green' | 'orange' | 'red' | 'purple' | 'teal' | 'yellow' | 'pink'

/** Bumped by detail panels / dialogs so the list page reloads. */
export const channelBus = reactive({ version: 0 })
export function notifyChannelsChanged(): void {
  channelBus.version++
}

/** `family` + `api` render the compact tag (family icon, API name); `path` is the upstream endpoint. */
export const PROTOCOLS: { value: Protocol; label: string; short: string; desc: string; family: 'anthropic' | 'openai'; api: string; path: string }[] = [
  { value: 'anthropic', label: 'Anthropic 协议', short: 'Anthropic', desc: '对接 Claude 风格的 /v1/messages 接口，支持 CC 伪装', family: 'anthropic', api: 'Messages', path: '/v1/messages' },
  { value: 'openai-chat', label: 'OpenAI Chat 协议', short: 'OpenAI Chat', desc: '对接 /v1/chat/completions 兼容接口（DeepSeek、智谱等）', family: 'openai', api: 'Chat', path: '/v1/chat/completions' },
  { value: 'openai-responses', label: 'OpenAI Responses 协议', short: 'OpenAI Responses', desc: '对接 /v1/responses 接口（gpt-5、o 系列等）', family: 'openai', api: 'Responses', path: '/v1/responses' },
]

export function protocolInfo(p: string | null | undefined) {
  return PROTOCOLS.find((x) => x.value === p) || null
}

export function protocolLabel(p: string | null | undefined): string {
  return PROTOCOLS.find((x) => x.value === p)?.short || '未知协议'
}

const HEALTH: Record<string, { label: string; tone: Tone }> = {
  healthy: { label: '正常', tone: 'green' },
  unknown: { label: '暂无数据', tone: 'neutral' },
  degraded: { label: '成功率下降', tone: 'orange' },
  cooldown: { label: '冷却中', tone: 'orange' },
  quotaCooldown: { label: '额度冷却', tone: 'orange' },
  unhealthy: { label: '异常', tone: 'red' },
  permanentCooldown: { label: '永久冷却', tone: 'red' },
  disabled: { label: '已停用', tone: 'neutral' },
}

export function healthLabel(h: string | null | undefined, enabled = true): string {
  if (!enabled) return '已停用'
  return HEALTH[h || 'unknown']?.label || '状态未知'
}
export function healthTone(h: string | null | undefined, enabled = true): Tone {
  if (!enabled) return 'neutral'
  return HEALTH[h || 'unknown']?.tone || 'neutral'
}
export function dotTone(t: Tone): 'green' | 'orange' | 'red' | 'blue' | 'gray' {
  if (t === 'yellow') return 'orange'
  return t === 'green' || t === 'orange' || t === 'red' || t === 'blue' ? t : 'gray'
}

/** Success-rate colour tiers used by the Telegram menus: ≥80 green, ≥50 yellow, else red. */
export function rateTone(rate: number | null | undefined): Tone {
  if (rate === null || rate === undefined) return 'neutral'
  return rate >= 80 ? 'green' : rate >= 50 ? 'yellow' : 'red'
}

/** Channel status line, same wording as the Telegram channel list. */
export function channelStatus(c: Pick<Channel, 'enabled' | 'health' | 'cooldownCount' | 'recentSuccessRate' | 'permanentCooldownCount'>): { text: string; tone: Tone } {
  if (!c.enabled || c.health === 'disabled') return { text: '已停用', tone: 'neutral' }
  if (c.health === 'permanentCooldown') return { text: `永久冷却 · ${c.permanentCooldownCount} 个模型`, tone: 'red' }
  if (c.health === 'quotaCooldown') return { text: `额度冷却 · ${c.cooldownCount} 个模型`, tone: 'orange' }
  if (c.health === 'cooldown') return { text: `冷却中 · ${c.cooldownCount} 个模型`, tone: 'orange' }
  const r = c.recentSuccessRate
  if (r === null || r === undefined) return { text: '暂无数据', tone: 'neutral' }
  return { text: `近期成功率 ${r.toFixed(0)}%`, tone: rateTone(r) }
}

/** Token counts like the bot: 1.2K / 3.4M / 5.6B. */
export function tokens(n: number | null | undefined): string {
  const v = Number(n || 0)
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return String(Math.round(v))
}

/** Generation speed like the bot: 42.3 t/s, 512 t/s, 1.2K t/s. */
export function tps(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K t/s`
  if (v >= 100) return `${v.toFixed(0)} t/s`
  return `${v.toFixed(1)} t/s`
}

export function promptTotal(input?: number | null, cacheCreation?: number | null, cacheRead?: number | null): number {
  return Number(input || 0) + Number(cacheCreation || 0) + Number(cacheRead || 0)
}


export const USAGE_STATUS: Record<string, string> = {
  fresh: '已更新',
  partial: '部分数据暂未获取',
  stale: '数据已过期，可刷新',
  stale_error: '最近一次更新失败，显示的是上次数据',
  error: '上游额度获取失败',
  refreshing: '正在更新…',
  not_fetched: '尚未获取',
  unsupported: '该渠道不支持额度查询',
}

const SUFFIX: Record<string, Protocol> = { messages: 'anthropic', completions: 'openai-chat', responses: 'openai-responses' }

/** Mirrors channel/url_utils.split_base_url: a full call path is split into (base, apiPath). */
export function splitUrl(raw: string): { base: string; path: string | null; detected: Protocol | null; error?: string } {
  const s = raw.trim().replace(/\/+$/, '')
  if (!s) return { base: '', path: null, detected: null }
  let u: URL
  try {
    u = new URL(s)
  } catch {
    return { base: s, path: null, detected: null, error: '地址格式不正确' }
  }
  if (!/^https?:$/.test(u.protocol)) return { base: s, path: null, detected: null, error: '地址需要以 http:// 或 https:// 开头' }
  if (u.search) return { base: s, path: null, detected: null, error: '地址不能包含 ? 查询参数' }
  if (u.hash) return { base: s, path: null, detected: null, error: '地址不能包含 # 片段' }
  if (u.username || u.password) return { base: s, path: null, detected: null, error: '地址里不能包含用户名或密码' }
  const last = s.split('/').pop()?.toLowerCase() || ''
  const detected = SUFFIX[last] || null
  if (!detected) return { base: s, path: null, detected: null }
  const path = u.pathname.replace(/\/+$/, '')
  return { base: `${u.protocol}//${u.host}`, path: path || null, detected }
}

/** Parse "real[:alias]" items separated by , ， ; ； or newlines. */
export function parseModelsInput(raw: string): ModelPair[] {
  const out: ModelPair[] = []
  for (const part of raw.split(/[,，;；\n]+/)) {
    const t = part.trim()
    if (!t) continue
    const i = t.indexOf(':')
    const real = (i >= 0 ? t.slice(0, i) : t).trim()
    const alias = (i >= 0 ? t.slice(i + 1) : '').trim() || real
    if (real) out.push({ real, alias })
  }
  return out
}

export interface OpOutcome<R> {
  ok: boolean
  result?: R
  error?: string
}

/** Start a 202 operation and wait for it; never throws. */
export async function runOperation<R>(start: () => Promise<{ data: Operation }>, onProgress?: (op: Operation) => void): Promise<OpOutcome<R>> {
  try {
    const res = await start()
    const op = await waitOperation(res.data, { onProgress, intervalMs: 800 })
    if (op.status === 'succeeded') return { ok: true, result: op.result as R }
    const e = op.error
    if (e) {
      const err = new ApiError(500, { code: e.code, message: e.message })
      const detail = errorDetail(err)
      return { ok: false, error: detail && detail !== e.code ? `${errorTitle(err)}：${detail}` : errorTitle(err) }
    }
    return { ok: false, error: op.status === 'cancelled' ? '任务已取消' : '任务失败' }
  } catch (err) {
    const detail = errorDetail(err)
    return { ok: false, error: detail ? `${errorTitle(err)}：${detail}` : errorTitle(err) }
  }
}

export interface ProbeResult {
  ok: boolean
  elapsed_ms: number
  reason: string | null
  cooldown_cleared: boolean
  permanent_cooldown_cleared: boolean
}

export interface DiscoveryResult {
  models: string[]
  source: string
  error: string | null
  retry_available: boolean
}

/** USD with three decimals, the precision the bot uses for channel and key stats. */
export function cost3(ticks: number | null | undefined): string {
  const v = Number(ticks || 0) / 1e10
  return `$${v.toLocaleString('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 })}`
}

/** Normalised monthly usage, fed by a channel's monthStats or an API key's usage. */
export interface Usage {
  total: number
  success: number
  error: number
  /** Full prompt: input + cache write + cache read (TG "↑"). */
  prompt: number
  output: number
  cacheRead: number
  /** 0–1, null when there were no requests / no prompt. */
  successRate: number | null
  cacheReadRatio: number | null
  avgTps: number | null
  maxTps: number | null
  minTps: number | null
  costTicks: number
}

type ChannelMonth = Schemas['ChannelMonthStatsData']
type KeyUsage = Schemas['ApiKeyUsageData']

export function toUsage(m: ChannelMonth | KeyUsage | null | undefined): Usage | null {
  if (!m) return null
  const channel = 'averageTokensPerSecond' in m || !('averageTps' in m)
  const c = m as ChannelMonth
  const k = m as KeyUsage
  return {
    total: m.total,
    success: m.successCount,
    error: m.errorCount,
    prompt: m.promptTokens || promptTotal(m.inputTokens, m.cacheCreationTokens, m.cacheReadTokens),
    output: m.outputTokens,
    cacheRead: m.cacheReadTokens,
    successRate: m.successRate ?? (m.total ? m.successCount / m.total : null),
    cacheReadRatio: m.cacheReadRatio ?? null,
    avgTps: (channel ? c.averageTokensPerSecond : k.averageTps) ?? null,
    maxTps: (channel ? c.maximumTokensPerSecond : k.maximumTps) ?? null,
    minTps: (channel ? c.minimumTokensPerSecond : k.minimumTps) ?? null,
    costTicks: m.costTicks,
  }
}

/** "51.7K（60.8%）" from a server-side ratio (0–1). */
export function cacheText(cacheRead: number, ratio: number | null | undefined, prompt: number): string {
  const r = ratio ?? (prompt ? cacheRead / prompt : 0)
  return `${tokens(cacheRead)}（${(r * 100).toFixed(1)}%）`
}

let catalogPromise: Promise<Catalog> | null = null
/** Channel catalog (providers and presets), fetched once per page load. */
export function loadCatalog(): Promise<Catalog> {
  if (!catalogPromise) {
    catalogPromise = api
      .get<Catalog>('/channel-catalog')
      .then((r) => r.data)
      .catch((err) => {
        catalogPromise = null
        throw err
      })
  }
  return catalogPromise
}
