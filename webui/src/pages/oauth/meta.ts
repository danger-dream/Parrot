import { effortList } from '@/utils/effort'
import type { Schemas } from '@/api/client'
import { duration, toDate, usd, TICKS_PER_USD } from '@/utils/format'

export type Provider = Schemas['OAuthProvider']
export type AccountSummary = Schemas['OAuthAccountSummaryData']
export type UsageWindow = Schemas['OAuthUsageWindowData']
export type ModelData = Schemas['OAuthModelData']
export type LoginFlow = Schemas['OAuthLoginFlowData']
export type LoginPoll = Schemas['OAuthLoginPollData']

export interface ProviderMeta {
  label: string
  color: string
  glyph: string
  hint: string
}

export const PROVIDERS: Record<string, ProviderMeta> = {
  claude: { label: 'Claude', color: '#d97757', glyph: 'C', hint: 'Anthropic 官方订阅（Pro / Max / Team）' },
  openai: { label: 'OpenAI', color: '#10a37f', glyph: 'O', hint: 'ChatGPT 订阅（Codex 额度）' },
  xai: { label: 'Grok', color: '#8e8e93', glyph: 'X', hint: 'xAI / Grok 账号' },
  cursor: { label: 'Cursor', color: '#7c7c86', glyph: '▲', hint: 'Cursor 订阅账号' },
  antigravity: { label: 'Antigravity', color: '#4285f4', glyph: 'G', hint: 'Google Antigravity 账号' },
  workbuddy: { label: 'WorkBuddy', color: '#14b8a6', glyph: 'W', hint: '腾讯 WorkBuddy（中国区 / 国际区）' },
  zhipu: { label: '智谱', color: '#3c6cff', glyph: '智', hint: '智谱 BigModel / Z.ai Coding Plan' },
  qoder: { label: 'Qoder', color: '#a259ff', glyph: 'Q', hint: 'Qoder（国际区 / 中国区）' },
}

export const PROVIDER_ORDER: Provider[] = ['claude', 'openai', 'xai', 'cursor', 'antigravity', 'workbuddy', 'zhipu', 'qoder']

export function providerMeta(p: string | null | undefined): ProviderMeta {
  if (p && PROVIDERS[p]) return PROVIDERS[p]
  const s = p || '?'
  return { label: s, color: 'var(--text-3)', glyph: s.slice(0, 1).toUpperCase(), hint: '' }
}

export const WINDOW_LABEL: Record<string, string> = {
  fiveHour: '5 小时',
  sevenDay: '7 天',
  thirtyDay: '30 天',
  daily: '每日',
  monthly: '每月',
}

const PLAN_LABEL: Record<string, string> = {
  free: '免费版',
  plus: 'Plus',
  pro: 'Pro',
  max: 'Max',
  team: '团队版',
  business: '商业版',
  enterprise: '企业版',
  edu: '教育版',
  ultra: 'Ultra',
  personal: '个人版',
}

/** "claude_team" → "Team 团队版"-style readable label; brand tiers keep their names. */
export function planLabel(plan: string | null | undefined): string {
  if (!plan) return ''
  const key = plan.toLowerCase().replace(/^claude_/, '').replace(/^chatgpt_/, '')
  if (PLAN_LABEL[key]) return PLAN_LABEL[key]
  if (key === 'max_5x' || key === 'max5x') return 'Max 5×'
  if (key === 'max_20x' || key === 'max20x') return 'Max 20×'
  return plan.replace(/_/g, ' ')
}

/** Values double as the `/oauth?status=` deep-link query. */
export type StateKey = 'available' | 'quota' | 'invalid' | 'disabled'
export const STATE_KEYS: StateKey[] = ['available', 'quota', 'invalid', 'disabled']
export type Tone = 'green' | 'orange' | 'red' | 'neutral'

const REASON_LABEL: Record<string, string> = {
  user: '手动停用',
  quota: '额度用尽，自动暂停',
  auth_error: '认证失效，需要重新登录',
}

export function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return ''
  return REASON_LABEL[reason] || reason
}

export function accountState(a: AccountSummary): { key: StateKey; label: string; tone: Tone } {
  // Same predicate as the backend / Telegram invalid list (and the overview pool card).
  if (a.invalid) return { key: 'invalid', label: '失效', tone: 'red' }
  if (a.quotaLimited) return { key: 'quota', label: '额度受限', tone: 'orange' }
  if (!a.enabled) return { key: 'disabled', label: '已停用', tone: 'neutral' }
  if (a.disabledReason) return { key: 'quota', label: a.disabledReason === '供应商限制' ? '供应商限制' : '受限', tone: 'orange' }
  if (!a.available) return { key: 'quota', label: '暂不可用', tone: 'orange' }
  return { key: 'available', label: '可用', tone: 'green' }
}

export const STATE_FILTERS: { value: '' | StateKey; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'available', label: '可用' },
  { value: 'quota', label: '额度受限' },
  { value: 'invalid', label: '失效' },
  { value: 'disabled', label: '停用' },
]

/** "剩 3天11小时" style countdown; "已重置" once passed. */
export function countdown(value: string | null | undefined, now = Date.now()): string {
  const d = toDate(value)
  if (!d) return ''
  const s = Math.round((d.getTime() - now) / 1000)
  if (s <= 0) return '已到重置时间'
  return `${duration(s).replace(/ \d+秒$/, '')}后重置`
}

export function costText(costUsd: number | null | undefined): string {
  if (costUsd === null || costUsd === undefined) return '未计价'
  return usd(Math.round(costUsd * TICKS_PER_USD))
}

export function effortText(list: string[] | null | undefined): string {
  return effortList(list)
}

export const TIER_LABEL: Record<string, string> = {
  priority: '优先',
  flex: '弹性',
  default: '默认',
  standard: '标准',
  fast: '快速',
  auto: '自动',
}

export function metadataSourceLabel(src: string | null | undefined): string {
  if (!src) return ''
  const [kind, from] = src.split(':')
  const who = from ? providerMeta(from).label : ''
  if (kind === 'upstream') return who ? `上游同步（${who}）` : '上游同步'
  if (kind === 'static' || kind === 'builtin' || kind === 'config') return '内置资料'
  if (kind === 'catalog') return '模型目录'
  if (kind === 'manual' || kind === 'override') return '手动设置'
  if (kind === 'lkg') return '上次成功的资料'
  return src
}

/** Read a field from an untyped backend dict that may use camelCase or snake_case keys. */
export function pick<T = unknown>(obj: unknown, key: string): T | undefined {
  if (!obj || typeof obj !== 'object') return undefined
  const o = obj as Record<string, unknown>
  if (key in o) return o[key] as T
  const snake = key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)
  if (snake in o) return o[snake] as T
  const camel = key.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())
  if (camel in o) return o[camel] as T
  return undefined
}

export function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export function amount(v: unknown, digits = 2): string {
  const n = num(v)
  if (n === null) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: digits })
}

/** Epoch seconds or milliseconds → Date-compatible input. */
export function epoch(v: unknown): number | string | null {
  if (typeof v === 'string') return v
  const n = num(v)
  if (n === null) return null
  return n > 1e12 ? n : n * 1000
}

export const OP_STATUS: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

// ---- generated API shapes used across the module ----
export type UsageItem = Schemas['OAuthUsageItemData']
export type UsageSection = Schemas['OAuthUsageSectionData']

/** Antigravity model groups as Google names them, shortened for the bars. */
const AG_GROUP: Record<string, string> = { 'Gemini Models': 'Gemini', 'Claude and GPT models': 'Claude/GPT' }

/** Weekly windows read "7 天" everywhere (older payloads still say 每周 / 周额度). */
export function windowName(label: string): string {
  return label === '周额度' ? '7 天' : label.replace(/每周/g, '7 天')
}

export function sectionTitle(s: UsageSection): string {
  return s.id.startsWith('antigravityGroup') ? AG_GROUP[s.title] || s.title : s.title
}

/** One quota line's label: Antigravity windows carry their model group ("Gemini 5 小时"). */
export function quotaLabel(s: UsageSection, it: UsageItem): string {
  const name = windowName(it.label)
  return s.id.startsWith('antigravityGroup') ? `${sectionTitle(s)} ${name}` : name
}
export type AccountDetail = Schemas['OAuthAccountDetailData']
export type AccountCardData = Schemas['OAuthAccountCardData']
export type PlanFacts = Schemas['OAuthPlanFactsData']
export type LocalUsageStats = Schemas['OAuthLocalUsageStatsData']
export type RuntimeErr = Schemas['OAuthRuntimeErrorData']
export type ClaudeResetPlan = Schemas['ClaudeResetPlanData']
export type ClaudeProgram = ClaudeResetPlan['program']
export type ClaudeResetResult = Schemas['ClaudeResetResultData']
export type ResetCard = Schemas['OpenAiResetCreditCardData']
export type ResetCredits = Schemas['OpenAiResetCreditsData']
export type QuotaResetPlan = Schemas['OAuthQuotaResetPlanData']

export const CLAUDE_PROGRAM_LABEL: Record<ClaudeProgram, string> = {
  cedar_ember: '周额度重置卡',
  juniper_tide: '5 小时重置',
}

/** Server reasons for Claude reset eligibility / results (TG keeps the raw codes; we translate them). */
export const CLAUDE_REASON: Record<string, string> = {
  status_unknown: '官方未返回这项功能的状态',
  ineligible: '官方判定当前没有资格',
  control: '账号不在这项功能的试验组里',
  already_used: '本周次数已用完',
  cooldown: '还在冷却期内',
  no_grant: '没有可用的重置卡',
  not_next_grant: '不是官方指定的下一张卡',
  unknown_grant: '找不到官方指定的重置卡',
  paused: '这张卡已被暂停',
  expired: '重置卡已过期',
  not_started: '重置卡尚未生效',
  not_limited: '额度还没撞限，这张卡要撞限后才能用',
  blocking: '有其他限制阻止使用',
  unavailable: '官方暂不可用',
  organization_missing: '缺少组织信息，请刷新用量后重试',
  organization_missing_or_changed: '组织信息缺失或已变化',
  operation_in_progress: '上一次重置还在处理中',
  allowance_changed_not_attributed: '额度有变化，但无法确认是这次重置造成的',
  pending_requires_reconciliation: '上次结果未确认，需要先查询核对',
  reset_unconfirmed: '官方没有确认结果',
  stamp_indeterminate: '无法确认是否已执行',
  auth_error: '账号认证已失效',
  user: '账号被手动停用',
  quota: '账号处于额度暂停',
  mock_mode: '当前为模拟模式，未发出请求',
  unknown_program: '未知的重置类型',
}
export function claudeReason(r: string | null | undefined): string {
  if (!r) return ''
  if (CLAUDE_REASON[r]) return CLAUDE_REASON[r]
  const http = /^http_(\d+)$/.exec(r)
  if (http) return `官方返回 HTTP ${http[1]}`
  return r
}
export const CLAUDE_RESULT: Record<string, string> = {
  reset: '已重置',
  already_used: '这张卡已使用过',
  not_limited: '额度还没撞限，未使用',
  cooldown: '还在冷却期，未使用',
  ineligible: '没有资格，未使用',
  unavailable: '官方暂不可用，未使用',
  pending: '上一次重置还在处理中',
  unconfirmed: '结果未确认，请先查询核对，不要重复执行',
  rate_limited: '请求太频繁，被官方限流',
  auth_error: '官方拒绝了认证',
}

export const RESET_CARD_STATUS: Record<string, string> = { available: '可用', redeeming: '兑换中', redeemed: '已使用' }
export function resetCardType(t: string | null | undefined): string {
  if (!t) return '重置卡'
  return t === 'codex_rate_limits' || t === 'codexRateLimits' ? 'Codex 额度重置' : t
}

/** Short English/enum notes that can appear in usage items. */
const NOTE_LABEL: Record<string, string> = {
  active: '订阅有效',
  trialing: '试用中',
  past_due: '已逾期',
  canceled: '已取消',
  cancelled: '已取消',
  unpaid: '未付款',
  failed: '刷新失败',
  timeout: '刷新超时',
  auth: '认证失败',
  network: '网络错误',
  rate_limited: '请求太频繁',
}
export function noteLabel(note: string | null | undefined): string {
  if (!note) return ''
  return note
    .split(/(；|;)/)
    .map((part) => NOTE_LABEL[part.trim()] || CLAUDE_REASON[part.trim()] || part)
    .join('')
}

/** "已用 $3.20 / $20" · "剩余 120 积分" · "剩 2" style value text for non-percent items. */
/** "1,058 / 3,500 积分": what is left of a metered allowance, shown next to its remaining %. */
export function remainingAmount(it: UsageItem): string {
  const limit = num(it.limit)
  const used = num(it.used)
  const remaining = num(it.remaining) ?? (limit !== null && used !== null ? Math.max(0, limit - used) : null)
  if (remaining === null || limit === null) return ''
  const fmt = (v: number) => {
    const text = v.toLocaleString('en-US', { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 })
    return it.unit === 'usd' ? `$${text}` : text
  }
  const suffix = it.unit === 'credits' ? ' 积分' : it.unit === 'count' ? ' 次' : ''
  return `${fmt(remaining)} / ${fmt(limit)}${suffix}`
}

export function usageValue(it: UsageItem): string {
  const fmt = (v: number) => {
    if (it.unit === 'usd') return `$${v.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
    return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  }
  const suffix = it.unit === 'credits' ? ' 积分' : ''
  const used = num(it.used)
  const limit = num(it.limit)
  const remaining = num(it.remaining)
  if (used !== null && limit !== null) return `已用 ${fmt(used)} / ${fmt(limit)}${suffix}`
  if (remaining !== null && limit !== null) return `剩余 ${fmt(remaining)} / ${fmt(limit)}${suffix}`
  if (remaining !== null) return `剩余 ${fmt(remaining)}${suffix}`
  if (used !== null) return `已用 ${fmt(used)}${suffix}`
  return ''
}

