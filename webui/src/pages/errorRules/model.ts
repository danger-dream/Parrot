import type { Component } from 'vue'
import {
  BatteryLow,
  Bell,
  Eye,
  FileWarning,
  Gauge,
  PackageX,
  PauseCircle,
  RotateCw,
  ServerCrash,
  ShieldX,
  TrendingDown,
  UserX,
} from '@lucide/vue'
import type { Schemas } from '@/api/client'
import { providerTile } from '@/utils/providers'

export type ErrorRulesData = Schemas['ErrorRulesData']
export type ErrorRule = Schemas['ErrorRuleData']
export type Cond = Schemas['ErrorRuleCondData']
export type Group = Schemas['ErrorRuleGroupData']
export type Node = Cond | Group
export type Step = Schemas['ErrorRuleStepData']
export type Threshold = Schemas['ErrorRuleThresholdData']
export type RuleResponse = Schemas['ErrorRuleResponseData']
export type Final = ErrorRule['final']
export type Category = ErrorRule['category']
export type CategoryInfo = Schemas['ErrorRuleCategoryData']
export type TestResult = Schemas['ErrorRulesTestData']
export type RecentData = Schemas['ErrorRulesRecentData']
export type RecentGroup = Schemas['ErrorRuleRecentGroupData']
export type Field = Cond['field']
export type Op = Cond['op']
export type StepType = Step['type']
export type Phase = 'http' | 'stream' | 'transport'
export type PauseMode = NonNullable<Step['mode']>
export type Tone = 'neutral' | 'blue' | 'green' | 'orange' | 'red' | 'purple' | 'teal' | 'yellow' | 'pink'

export interface EditorSample {
  provider: string
  channelKey: string
  model: string
  status: number | null
  phase: Phase
  body: string
}

export interface EditorSeed {
  rule?: Partial<ErrorRule>
  editingId?: string | null
  origin?: string
  guessed?: boolean
  sample?: EditorSample | null
}

export const CATEGORY_LOOK: Record<Category, { tone: Tone; color: string; icon: Component }> = {
  rate_limit: { tone: 'yellow', color: 'var(--yellow)', icon: Gauge },
  quota_exhausted: { tone: 'orange', color: 'var(--orange)', icon: BatteryLow },
  plan_excluded: { tone: 'purple', color: 'var(--purple)', icon: PackageX },
  account_blocked: { tone: 'red', color: 'var(--red)', icon: UserX },
  request_invalid: { tone: 'blue', color: 'var(--blue)', icon: FileWarning },
  content_blocked: { tone: 'pink', color: 'var(--pink)', icon: ShieldX },
  upstream_busy: { tone: 'teal', color: 'var(--teal)', icon: ServerCrash },
  observe: { tone: 'neutral', color: 'var(--text-3)', icon: Eye },
}

// ─── conditions ──────────────────────────────────────────────────────

export type ValueKind = 'numbers' | 'codes' | 'text' | 'provider' | 'channel' | 'channelType' | 'model' | 'phase' | 'any'

export const FIELD_META: Record<Field, { label: string; ops: Op[]; kind: ValueKind; hint: string; placeholder?: string }> = {
  status: { label: '状态码', ops: ['in', 'not_in', 'eq', 'ne', 'gte', 'lte', 'gt', 'lt'], kind: 'numbers', hint: '上游返回的 HTTP 状态码。可写 429、500-599、5xx', placeholder: '如 429' },
  code: { label: '错误码', ops: ['in', 'not_in', 'eq', 'ne', 'exists', 'not_exists'], kind: 'codes', hint: '自动从 code、error.code、error.type、base_resp.status_code 等位置和消息里的「(2067)」取出；数字码可写区间 1316-1321', placeholder: '如 1308' },
  message: { label: '错误消息', ops: ['contains', 'not_contains', 'eq', 'ne', 'starts_with', 'regex'], kind: 'text', hint: '从 message、msg、error.message 等位置取出的那句话', placeholder: '如 额度已用完' },
  body: { label: '响应全文', ops: ['contains', 'not_contains', 'regex'], kind: 'text', hint: '上游返回的完整内容（前 4 KB）', placeholder: '如 quota' },
  path: { label: 'JSON 路径', ops: ['eq', 'ne', 'in', 'not_in', 'contains', 'not_contains', 'starts_with', 'regex', 'exists', 'not_exists', 'gt', 'gte', 'lt', 'lte'], kind: 'any', hint: '取上游返回 JSON 里任意字段：$.error.code、$.base_resp.status_code、$.errors[0].message；$..message 表示任意层级的 message', placeholder: '值' },
  provider: { label: '提供商', ops: ['in', 'not_in'], kind: 'provider', hint: '' },
  channel: { label: '渠道 / 账号', ops: ['in', 'not_in'], kind: 'channel', hint: '具体的 API 渠道或 OAuth 账号' },
  channelType: { label: '账号类型', ops: ['eq', 'ne'], kind: 'channelType', hint: '' },
  model: { label: '模型', ops: ['in', 'not_in', 'eq', 'ne', 'contains', 'not_contains'], kind: 'model', hint: '支持通配符，如 glm-5*', placeholder: '如 glm-5*' },
  phase: { label: '发生阶段', ops: ['in', 'not_in'], kind: 'phase', hint: '' },
}

export const FIELD_ORDER: Field[] = ['status', 'code', 'message', 'path', 'provider', 'channel', 'channelType', 'model', 'phase', 'body']

export const OP_LABELS: Record<Op, string> = {
  eq: '等于', ne: '不等于', in: '是其中之一', not_in: '不是其中任何一个', contains: '包含', not_contains: '不包含',
  starts_with: '开头是', regex: '匹配正则', exists: '存在', not_exists: '不存在', gt: '大于', gte: '大于等于', lt: '小于', lte: '小于等于',
}

export const LIST_OPS: Op[] = ['in', 'not_in', 'contains', 'not_contains']
export const NO_VALUE_OPS: Op[] = ['exists', 'not_exists']

export const PHASE_OPTIONS: { value: Phase; label: string; hint: string }[] = [
  { value: 'http', label: '上游返回错误', hint: '上游直接回了 4xx / 5xx' },
  { value: 'stream', label: '输出中报错', hint: '返回 200 后，在开始输出前报错' },
  { value: 'transport', label: '连接或超时', hint: '没拿到上游的回复：连不上、超时' },
]
export const CHANNEL_TYPE_LABELS: Record<string, string> = { oauth: 'OAuth 账号', api: 'API 渠道' }

export function newCond(field: Field = 'status'): Cond {
  return { kind: 'cond', field, op: FIELD_META[field].ops[0], path: field === 'path' ? '$.' : '', values: [] }
}

export function newGroup(mode: 'all' | 'any' = 'any', items: Node[] = [newCond()]): Group {
  return { kind: 'group', mode, items }
}

export function countConds(g: Group): number {
  return g.items.reduce((n, i) => n + (i.kind === 'group' ? countConds(i) : 1), 0)
}

export function filledConds(g: Group): number {
  return g.items.reduce((n, i) => n + (i.kind === 'group' ? filledConds(i) : condFilled(i) ? 1 : 0), 0)
}

/** `$.a`, `$..a`, `$[0]`, `$['a']` — the starts the backend accepts (`$.` alone is not a path yet). */
export function validPath(path: string | null | undefined): boolean {
  return /^\$(\.\.?[^.\[\s]|\[)/.test((path || '').trim())
}

export function condFilled(c: Cond): boolean {
  if (c.field === 'path' && !validPath(c.path)) return false
  return NO_VALUE_OPS.includes(c.op) || (c.values?.length ?? 0) > 0
}

/** Drops conditions without a value and groups left empty, so a half-filled row never blocks saving. */
export function pruneTree(g: Group): Group {
  const items: Node[] = []
  for (const i of g.items) {
    if (i.kind === 'group') {
      const sub = pruneTree(i)
      if (sub.items.length) items.push(sub)
    } else if (condFilled(i)) {
      items.push({ ...i, values: NO_VALUE_OPS.includes(i.op) ? [] : i.values })
    }
  }
  return { kind: 'group', mode: g.mode, items }
}

export interface Segment {
  text: string
  strong?: boolean
  muted?: boolean
}

export interface Labels {
  provider: (id: string) => string
  channel: (id: string) => string
}

const plainNumber = /^(-?\d+(\.\d+)?|\d+-\d+|[1-5]xx)$/i

function valueText(c: Cond, v: string, labels: Labels): string {
  if (c.field === 'provider') return labels.provider(v)
  if (c.field === 'channel') return labels.channel(v)
  if (c.field === 'channelType') return CHANNEL_TYPE_LABELS[v] || v
  if (c.field === 'phase') return PHASE_OPTIONS.find((p) => p.value === v)?.label || v
  if (['message', 'body', 'path', 'model', 'code'].includes(c.field) && !plainNumber.test(v)) return `“${v}”`
  return v.replace(/^(\d+)-(\d+)$/, '$1–$2')
}

export function condSegments(c: Cond, labels: Labels): Segment[] {
  const name = c.field === 'path' ? c.path || '$' : FIELD_META[c.field].label
  const out: Segment[] = [{ text: name, strong: c.field === 'path' }, { text: ` ${OP_LABELS[c.op]}` }]
  if (!NO_VALUE_OPS.includes(c.op)) {
    const values = (c.values || []).slice(0, 4).map((v) => valueText(c, v, labels))
    const more = (c.values || []).length > 4 ? ` 等 ${(c.values || []).length} 项` : ''
    const joiner = c.op === 'in' || c.op === 'not_in' ? '、' : ' 或 '
    out.push({ text: ' ' }, { text: values.length ? values.join(joiner) + more : '（未填）', strong: values.length > 0, muted: !values.length })
  }
  return out
}

/** "状态码 是其中之一 429 且 ( 错误消息 包含 “x” 或 $.code 等于 1 )" as segments. */
export function treeSegments(g: Group, labels: Labels, top = true): Segment[] {
  const out: Segment[] = []
  const items = g.items.filter((i) => (i.kind === 'group' ? countConds(i) > 0 : true))
  items.forEach((item, idx) => {
    if (idx) out.push({ text: g.mode === 'all' ? ' 且 ' : ' 或 ', muted: true })
    if (item.kind === 'group') {
      const inner = treeSegments(item, labels, false)
      const wrap = item.items.length > 1
      if (wrap) out.push({ text: '（', muted: true })
      out.push(...inner)
      if (wrap) out.push({ text: '）', muted: true })
    } else {
      out.push(...condSegments(item, labels))
    }
  })
  if (top && !out.length) out.push({ text: '还没有设置条件', muted: true })
  return out
}

/** Every leaf of a JSON value as `$.a.b[0]` → value, for picking a field from a sample. */
export function flattenJson(data: unknown, limit = 80): { path: string; value: string }[] {
  const out: { path: string; value: string }[] = []
  const walk = (node: unknown, path: string, depth: number): void => {
    if (out.length >= limit || depth > 6) return
    if (Array.isArray(node)) {
      node.slice(0, 5).forEach((v, i) => walk(v, `${path}[${i}]`, depth + 1))
    } else if (node && typeof node === 'object') {
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
        const seg = /^[A-Za-z0-9_\-$]+$/.test(k) ? `.${k}` : `['${k.replace(/'/g, '')}']`
        walk(v, path + seg, depth + 1)
      }
    } else if (node !== null && node !== undefined) {
      out.push({ path, value: String(node) })
    }
  }
  walk(data, '$', 0)
  return out
}

export function parseJsonBody(body: string): unknown {
  const start = body.indexOf('{')
  if (start < 0) return null
  try {
    return JSON.parse(body.slice(start))
  } catch {
    return null
  }
}

// ─── steps ───────────────────────────────────────────────────────────

export const STEP_META: Record<StepType, { label: string; icon: Component; hint: string; tone: string }> = {
  retry: { label: '等待后重试', icon: RotateCw, hint: '在同一个渠道重发。重试成功就到此结束；重试后还是这个错误，才继续执行下一步', tone: 'var(--blue)' },
  pause: { label: '暂停', icon: PauseCircle, hint: '暂停期间调度会跳过它，到时间自动恢复', tone: 'var(--orange)' },
  deprioritize: { label: '降低调度优先级', icon: TrendingDown, hint: '智能调度按各渠道最近的成功率和速度排序。记一次失败，这个渠道会暂时排得靠后、少被选中，之后成功了会慢慢回升', tone: 'var(--teal)' },
  disable: { label: '停用账号', icon: UserX, hint: 'OAuth 账号会被停用，处理好后在「OAuth 账户」里手动启用；API 渠道会停用整个渠道', tone: 'var(--red)' },
  notify: { label: '发通知', icon: Bell, hint: '通过通知中心和 Telegram 提醒', tone: 'var(--purple)' },
}

export const MODE_OPTIONS: { value: PauseMode; label: string; hint: string }[] = [
  { value: 'fixed', label: '固定时长', hint: '每次都暂停同样长的时间' },
  { value: 'message_time', label: '到错误里写的恢复时间', hint: '自动识别「将在 2026-11-20 19:52:32 重置」「resets in 3 hours」这类写法' },
  { value: 'retry_after', label: '按上游的 Retry-After', hint: '读取响应头 Retry-After，没有时再从错误文本里找' },
  { value: 'usage_window', label: '到额度窗口结束', hint: '读取这个渠道的额度查询结果（如 MiniMax Token Plan）' },
  { value: 'boundary', label: '到下一个周期边界', hint: '每 N 小时一个周期，北京时间 0 点起算' },
  { value: 'ladder', label: '按冷却阶梯', hint: '和默认策略一样：连续出错时按「错误冷却」阶梯逐级延长' },
]

export const FINAL_OPTIONS: { value: Final; label: string; hint: string }[] = [
  { value: 'next', label: '换下一个渠道', hint: '继续用下一个可用渠道完成这次请求' },
  { value: 'respond', label: '直接返回给客户端', hint: '不再尝试其他渠道。适合请求本身有问题、换哪个渠道都会失败的情况' },
  { value: 'pass', label: '交给后面的规则', hint: '这条规则只做上面的步骤（比如标记、通知），怎么处理仍由后面的规则和默认策略决定' },
]

export function newStep(type: StepType): Step {
  switch (type) {
    case 'retry':
      return { type, delaySeconds: 2, times: 1 }
    case 'pause':
      return { type, scope: 'model', mode: 'fixed', seconds: 900, pattern: '', timezone: '+08:00', boundaryHours: 0, fallbackSeconds: 900 }
    case 'disable':
      return { type, reason: '' }
    case 'notify':
      return { type, repeatHours: 6 }
    default:
      return { type }
  }
}

export function humanDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0 秒'
  const units: [number, string][] = [[86400, '天'], [3600, '小时'], [60, '分钟'], [1, '秒']]
  for (const [size, name] of units) {
    if (seconds >= size && seconds % size === 0) return `${seconds / size} ${name}`
  }
  if (seconds >= 3600) return `${Math.round(seconds / 360) / 10} 小时`
  return `${Math.round(seconds / 60)} 分钟`
}

export function describeWait(s: Step): string {
  const fallback = s.fallbackSeconds ? `，读不到时 ${humanDuration(s.fallbackSeconds)}` : ''
  switch (s.mode) {
    case 'fixed':
      return humanDuration(s.seconds || 0)
    case 'message_time':
      return `到错误里写的恢复时间${fallback}`
    case 'retry_after':
      return `按 Retry-After${s.fallbackSeconds ? `，没有时 ${humanDuration(s.fallbackSeconds)}` : ''}`
    case 'usage_window':
      return `到额度窗口结束${s.boundaryHours ? `，读不到时到下一个 ${s.boundaryHours} 小时边界` : fallback}`
    case 'boundary':
      return `到下一个 ${s.boundaryHours} 小时周期边界`
    case 'ladder':
      return '按冷却阶梯逐级延长'
  }
  return ''
}

export function describeStep(s: Step): string {
  switch (s.type) {
    case 'retry':
      return s.delaySeconds ? `等 ${s.delaySeconds} 秒后在同一渠道重试${(s.times || 1) > 1 ? ` ${s.times} 次` : ''}` : `立刻在同一渠道重试${(s.times || 1) > 1 ? ` ${s.times} 次` : ''}`
    case 'pause':
      return `${s.scope === 'account' ? '暂停整个账号' : '暂停这个模型'}（${describeWait(s)}）`
    case 'deprioritize':
      return '降低这个渠道的调度优先级'
    case 'disable':
      return '停用账号'
    case 'notify':
      return '发通知'
  }
  return ''
}

export const THRESHOLD_SCOPE_OPTIONS = [
  { value: 'model' as const, label: '同一渠道的同一模型' },
  { value: 'account' as const, label: '同一渠道 / 账号' },
  { value: 'rule' as const, label: '所有渠道合计' },
]

export function describeThreshold(t: Threshold): string {
  const scope = THRESHOLD_SCOPE_OPTIONS.find((o) => o.value === t.scope)?.label || ''
  const when = t.mode === 'consecutive' ? `连续 ${t.count} 次` : `${t.windowMinutes} 分钟内 ${t.count} 次`
  const what = t.steps.map(describeStep).join('、') || '（还没选动作）'
  return `${scope}${when} → ${what}`
}

export function newThreshold(): Threshold {
  return { count: 3, mode: 'consecutive', windowMinutes: 10, scope: 'model', steps: [{ ...newStep('pause'), seconds: 1800 }, newStep('notify')] }
}

export function finalLabel(f: Final): string {
  return FINAL_OPTIONS.find((o) => o.value === f)?.label || f
}

// ─── reply ───────────────────────────────────────────────────────────

export const RESPONSE_STATUS_OPTIONS: { value: number; label: string; hint: string }[] = [
  { value: 0, label: '保持默认', hint: '和没有这条规则时一样（直接返回时原样转发上游的错误）' },
  { value: 429, label: '429 请求过多', hint: '客户端会稍后重试，适合限流和额度用尽' },
  { value: 503, label: '503 服务不可用', hint: '临时不可用' },
  { value: 402, label: '402 需要付费', hint: '余额或额度不足' },
  { value: 403, label: '403 禁止访问', hint: '没有权限，客户端通常不会重试' },
  { value: 400, label: '400 请求有误', hint: '请求本身的问题，客户端不会重试' },
  { value: 404, label: '404 不存在', hint: '模型不存在或不可用' },
  { value: 500, label: '500 服务器错误', hint: '' },
  { value: 502, label: '502 网关错误', hint: '' },
  { value: 504, label: '504 网关超时', hint: '' },
]

export const TEMPLATE_VARS: { key: string; label: string; sample: string }[] = [
  { key: 'model', label: '模型', sample: 'glm-5.3' },
  { key: 'reset_at', label: '恢复时间', sample: '2026-11-20 23:55:22 (UTC+8)' },
  { key: 'reset_in', label: '还要多久', sample: '2 h 14 min' },
  { key: 'provider', label: '提供商', sample: 'zhipu' },
  { key: 'upstream_message', label: '上游原文', sample: '已达到 5 小时的使用上限' },
  { key: 'code', label: '错误码', sample: '1308' },
]

export const DEFAULT_MESSAGES: Partial<Record<Category, string>> = {
  quota_exhausted: 'Upstream quota for {model} is exhausted. Expected to recover at {reset_at} (in {reset_in}).',
  rate_limit: 'Upstream rate limit for {model}. Retry in {reset_in}.',
  plan_excluded: 'The upstream plan does not include {model}.',
  account_blocked: 'The upstream account for {model} is unavailable: {upstream_message}',
}

export function renderTemplate(template: string, category: Category, data: unknown = null): string {
  const text = template || DEFAULT_MESSAGES[category] || '{upstream_message}'
  return text.replace(/\{(\$[^{}]*|[a-z_]+)\}/g, (all, key: string) => {
    if (key.startsWith('$')) {
      const hit = data ? flattenJson(data, 400).find((f) => f.path === key) : undefined
      return hit ? hit.value : `‹${key}›`
    }
    return TEMPLATE_VARS.find((v) => v.key === key)?.sample ?? all
  })
}

// ─── misc ────────────────────────────────────────────────────────────

const OUTCOME_LABELS: Record<string, string> = {
  connection_timeout: '连接超时',
  connect_timeout: '连接超时',
  http_connect_timeout: '连接超时',
  upload_timeout: '上传超时',
  first_byte_timeout: '首字超时',
  idle_timeout: '输出中断',
  total_timeout: '总时长超时',
  connect_error: '连接失败',
  transport_error: '传输错误',
  connection_lifecycle: '连接中断',
  stream_upstream_error: '输出中报错',
  upstream_error_json: '返回错误内容',
  http_auth_error: '认证失败',
  request_invalid: '请求被拒',
}

export function outcomeLabel(outcome: string): string {
  return OUTCOME_LABELS[outcome] || outcome
}

export function statusLabel(status: number | null | undefined, outcome = ''): string {
  return status ? `HTTP ${status}` : outcomeLabel(outcome) || '无状态码'
}

export function providerLabel(id: string): string {
  return providerTile(id).label
}

export function categoryLabel(categories: CategoryInfo[], id: Category): string {
  return categories.find((c) => c.id === id)?.label || id
}

/** "约 2 小时 14 分后" for a moment in the future (epoch ms). */
export function untilRelative(ms: number, now = Date.now()): string {
  const s = Math.max(0, Math.round((ms - now) / 1000))
  if (s < 60) return `${s} 秒后`
  const m = Math.round(s / 60)
  if (m < 60) return `约 ${m} 分钟后`
  const h = Math.floor(m / 60)
  if (h < 48) return m % 60 ? `约 ${h} 小时 ${m % 60} 分后` : `约 ${h} 小时后`
  return `约 ${Math.round(h / 24)} 天后`
}

export function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

export function rulePayload(r: ErrorRule): Record<string, unknown> {
  return {
    name: r.name, enabled: r.enabled, note: r.note, when: r.when, category: r.category,
    steps: r.steps, final: r.final, thresholds: r.thresholds, response: r.response,
  }
}

const GENERIC_CODES = new Set([
  'error', 'api_error', 'server_error', 'rate_limit_error', 'invalid_request_error', 'service_unavailable_error',
  'authentication_error', 'permission_error', 'not_found_error', 'overloaded_error', 'upstream_error',
  'internal_error', 'internal_server_error', 'bad_request', 'invalid_request', 'unknown_error', 'failed',
])

/** The code worth matching on: a business number first, then a specific identifier. */
export function pickCode(codes: string[]): string {
  return codes.find((c) => /^\d{4,6}$/.test(c))
    || codes.find((c) => !GENERIC_CODES.has(c) && !/^\d{1,3}$/.test(c))
    || ''
}

export function guessCategory(status: number | null, message: string, phase: Phase): Category {
  const text = message.toLowerCase()
  if (/套餐.*(不|未).*(含|包含|开放|支持)|not (yet )?include|does not include|not available on your plan/.test(text)) return 'plan_excluded'
  if (/欠费|封禁|已停用|suspend|banned|deactivated|disabled|expired|到期|失效|unauthorized|invalid api key/.test(text) || status === 401 || status === 403) return 'account_blocked'
  if (/额度|配额|上限|用量|余额|积分|quota|credit|balance|usage limit|exhaust|insufficient/.test(text)) return 'quota_exhausted'
  if (/safety|审核|敏感|违规|content.?filter|moderation|policy/.test(text)) return 'content_blocked'
  if (status === 429 || /rate.?limit|频率|too many requests/.test(text)) return 'rate_limit'
  if (status && [400, 404, 413, 422].includes(status)) return 'request_invalid'
  if (phase === 'transport' || (status && status >= 500)) return 'upstream_busy'
  return 'observe'
}

function clip(text: string, max: number): string {
  if (text.length <= max) return text
  const cut = text.slice(0, max)
  const space = cut.lastIndexOf(' ')
  return space > max / 2 ? cut.slice(0, space) : cut
}

/** A new rule pre-filled from one recorded error. */
export function seedFromError(sample: EditorSample, codes: string[], message: string, origin: string): EditorSeed {
  const code = pickCode(codes)
  const items: Node[] = []
  if (sample.provider) items.push({ kind: 'cond', field: 'provider', op: 'in', path: '', values: [sample.provider] })
  if (sample.phase === 'transport') items.push({ kind: 'cond', field: 'phase', op: 'in', path: '', values: ['transport'] })
  if (sample.status) items.push({ kind: 'cond', field: 'status', op: 'in', path: '', values: [String(sample.status)] })
  if (code) {
    items.push({ kind: 'cond', field: 'code', op: 'in', path: '', values: [code] })
  } else {
    const keyword = clip(message.split(/[。!！?？;；\n]|\.\s/)[0].replace(/^\[[^\]]*\]/, '').trim(), 60)
    if (keyword) items.push({ kind: 'cond', field: 'message', op: 'contains', path: '', values: [keyword] })
  }
  return {
    origin,
    guessed: true,
    sample,
    rule: { category: guessCategory(sample.status, message, sample.phase), when: { kind: 'group', mode: 'all', items } },
  }
}

/** Like :func:`seedFromError`, reading the codes and message out of a raw error body. */
export function seedFromBody(sample: EditorSample, origin: string): EditorSeed {
  const codes: string[] = []
  for (const part of sample.body.match(/"(?:code|type|status_code)"\s*:\s*"?[\w.-]+"?/g) || []) {
    const v = part.replace(/^[^:]*:\s*"?/, '').replace(/"$/, '').toLowerCase()
    if (v && !codes.includes(v)) codes.push(v)
  }
  for (const n of sample.body.match(/[([（【]\s*\d{3,6}\s*[)\]）】]/g) || []) {
    const v = n.replace(/\D/g, '')
    if (!codes.includes(v)) codes.push(v)
  }
  const message = (sample.body.match(/"(?:message|msg|status_msg)"\s*:\s*"((?:[^"\\]|\\.)*)"/) || [])[1] || sample.body.slice(0, 200)
  return seedFromError(sample, codes, message, origin)
}
