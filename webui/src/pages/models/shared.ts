import { EFFORT_LABEL } from '@/utils/effort'
import { reactive } from 'vue'
import { api, type Schemas } from '@/api/client'
import { catalog, channelName } from '@/stores/catalog'
import { channelProvider } from '@/utils/providers'
import { capacity as tokens, pctValue, price } from '@/utils/format'
export { tokens, price }

// Shared state and Chinese labels for the model center and routing pages.

export type ModelData = Schemas['ModelData']
export type ModelSource = Schemas['ModelSourceData']
export type ModelMetadata = Schemas['ModelMetadataData']
export type MetadataValues = Schemas['MetadataValues']
export type ModelKind = 'chat' | 'image' | 'video'
export type ModelUsage = Schemas['ModelUsageData']
export type CompressionModel = Schemas['CompressionModelData']
export type SourceAccount = Schemas['OAuthSourceAccountData']
export type UsageSection = Schemas['OAuthUsageSectionData']
export type UsageItem = Schemas['OAuthUsageItemData']

interface Paged<T> {
  data: T[]
  meta: { hasNext?: boolean; revision?: string; total?: number }
}

/** Fetch every page of a paged list endpoint (pageSize 200). */
export async function fetchAll<T>(path: string, query: Record<string, string | number | undefined> = {}): Promise<{ items: T[]; revision: string }> {
  const items: T[] = []
  let revision = ''
  for (let page = 1; page < 100; page++) {
    const res = (await api.get<T[]>(path, { ...query, page, pageSize: 200 })) as unknown as Paged<T>
    items.push(...res.data)
    revision = res.meta.revision || revision
    if (!res.meta.hasNext) break
  }
  return { items, revision }
}

// ---- model list cache ----

export const modelStore = reactive({
  models: [] as ModelData[],
  revision: '',
  metadata: {} as Record<string, ModelMetadata>,
  /** Account/channel-scoped metadata records per model (values that differ from the common record). */
  sourceMetadata: {} as Record<string, ModelMetadata[]>,
  metadataRevision: '',
  /** Current compression model (getCompressionModel). */
  compression: null as CompressionModel | null,
  loading: false,
  loaded: false,
  error: null as unknown,
  loadedAt: 0,
})

let inflight: Promise<void> | null = null

export function loadModels(force = false): Promise<void> {
  if (!force && modelStore.loaded && Date.now() - modelStore.loadedAt < 15_000) return Promise.resolve()
  if (inflight) return inflight
  modelStore.loading = true
  inflight = (async () => {
    try {
      const [models, meta, comp] = await Promise.all([
        fetchAll<ModelData>('/models'),
        fetchAll<ModelMetadata>('/model-metadata').catch(() => ({ items: [] as ModelMetadata[], revision: '' })),
        api.get<CompressionModel>('/compression-model').then((r) => r.data).catch(() => null),
      ])
      modelStore.compression = comp
      modelStore.models = models.items
      modelStore.revision = models.revision
      const map: Record<string, ModelMetadata> = {}
      // Model-common records only (scope=global, no scopeId); account/channel records carry their own scope.
      const scoped: Record<string, ModelMetadata[]> = {}
      for (const m of meta.items) {
        if (m.scope === 'global' && !m.scopeId) map[m.modelId] = m
        else (scoped[m.modelId] ||= []).push(m)
      }
      modelStore.metadata = map
      modelStore.sourceMetadata = scoped
      modelStore.metadataRevision = meta.revision
      modelStore.loaded = true
      modelStore.loadedAt = Date.now()
      modelStore.error = null
    } catch (err) {
      modelStore.error = err
    } finally {
      modelStore.loading = false
      inflight = null
    }
  })()
  return inflight
}

export function setCompressionLocal(modelId: string | null, revision?: string): void {
  modelStore.compression = { modelId, revision: revision || modelStore.compression?.revision || '' }
}

/** Find a model by its client-visible id (or one of its aliases). */
export function findModel(id: string): ModelData | undefined {
  return modelStore.models.find((m) => m.modelId === id) || modelStore.models.find((m) => (m.aliases || []).includes(id))
}

// ---- labels ----

export const KIND_LABEL: Record<string, string> = { chat: '对话', image: '图片', video: '视频' }

export const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'] as const
export { EFFORT_LABEL, effortLabel } from '@/utils/effort'

export const TIER_LABEL: Record<string, string> = { default: '标准', fast: '快速', flex: '弹性', priority: '优先', scale: '规模', auto: '自动' }

export const MODALITY_LABEL: Record<string, string> = { text: '文本', image: '图片', pdf: 'PDF', audio: '音频', video: '视频', file: '文件' }

export const VALUE_SOURCE_LABEL: Record<string, string> = {
  'catalog-snapshot': '公共目录',
  catalog: '公共目录',
  'account-native': '账户原生',
  'native-hard': '服务方硬限制',
  derived: '推算',
  'common-override': '手工设置（通用）',
  'source-override': '手工设置（本来源）',
  'metadata-override': '手工设置',
}
export function valueSourceLabel(v: string | null | undefined): string {
  if (!v) return '—'
  return VALUE_SOURCE_LABEL[v] || '其他来源'
}

export const AUTHORITY_LABEL: Record<string, string> = {
  'models.dev': 'models.dev 公共目录',
  'account-upstream': '账户上游资料',
  none: '未匹配',
}
export const META_SOURCE_LABEL: Record<string, string> = {
  auto: '自动匹配',
  manual: '手动绑定',
  'management-api': '手动绑定',
  override: '仅手工设置',
  none: '未匹配',
  'cursor.AvailableModels': 'Cursor 原生资料',
}
/** Catalog target shown to users; synthetic "unbound/…" keys mean no catalog entry. */
export function matchTarget(t: string | null | undefined): string | null {
  if (!t || t.startsWith('unbound/')) return null
  return t
}
export function isManualSource(s: string | null | undefined): boolean {
  return s === 'manual' || s === 'management-api'
}
export function metaSourceLabel(m: Pick<ModelMetadata, 'source' | 'authority'> | null | undefined): string {
  if (!m) return '—'
  return META_SOURCE_LABEL[m.source] || AUTHORITY_LABEL[m.authority] || '其他'
}

export const PROVIDER_LABEL: Record<string, string> = {
  claude: 'Claude',
  openai: 'OpenAI',
  xai: 'xAI',
  cursor: 'Cursor',
  antigravity: 'Antigravity',
  workbuddy: 'WorkBuddy',
  qoder: 'Qoder',
  zhipu: '智谱',
  deepseek: 'DeepSeek',
  kimi: 'Kimi',
  minimax: 'MiniMax',
  anthropic: 'Anthropic',
  google: 'Google',
}
export function providerLabel(p: string | null | undefined): string {
  if (!p) return '—'
  return PROVIDER_LABEL[p] || p
}

/** Channel id ("oauth:<accountId>" / "api:Name") for a model source. */
export function sourceChannelId(s: Pick<ModelSource, 'type' | 'id'>): string {
  return s.type === 'oauth' ? `oauth:${s.id}` : s.id
}

/** Provider key for ProviderIcon. */
export function sourceProvider(s: Pick<ModelSource, 'type' | 'id' | 'provider'>): string {
  return channelProvider(sourceChannelId(s), s.provider)
}

/** TG 💳 rate lines from `creditRates` (listModels / getModel), merged per provider. */
export function rateSummary(rates: Record<string, string[]> | null | undefined): string[] {
  const out: string[] = []
  const wb = rates?.workbuddy || []
  if (wb.length) out.push('WorkBuddy 倍率 ' + wb.map((v) => (Number(v) === 0 ? '×0（免费）' : `×${+Number(v).toFixed(4)}`)).join(' / '))
  const qd = rates?.qoder || []
  if (qd.length) out.push('Qoder 积分倍率 ' + qd.join(' / '))
  return out
}

/** Rate of one source, read from its catalog record (same values TG formats). */
export function sourceRate(s: ModelSource): string {
  const m = s.effectiveMetadata || {}
  if (s.provider === 'workbuddy') {
    const n = Number(String(m.workbuddyCreditMultiplier ?? '').replace(/[x×\s]|credits/gi, ''))
    if (m.workbuddyCreditMultiplier == null || !Number.isFinite(n)) return 'WorkBuddy 倍率未知'
    return n === 0 ? 'WorkBuddy 倍率 ×0（免费）' : `WorkBuddy 倍率 ×${n}`
  }
  if (s.provider === 'qoder') {
    const rate = m.qoderRate
    if (m.qoderFree !== true && (typeof rate !== 'number' || rate < 0)) return ''
    let text = m.qoderFree === true ? 'Qoder 积分 免费' : `Qoder 积分倍率 ×${rate}`
    const was = m.qoderRateWas
    if (typeof was === 'number' && was > (typeof rate === 'number' ? rate : 0)) text += `（原 ×${was}）`
    return text
  }
  return ''
}

/** One routing source (account / channel) of a model; several upstream ids fold into one. */
export interface SourceGroup {
  key: string
  type: ModelSource['type']
  id: string
  provider: string
  channelId: string
  items: ModelSource[]
  live: boolean
  sourceEnabled: boolean
  containerEnabled: boolean
  enabledOverride: boolean
  rank: number | null
}

/**
 * Group sources by account/channel. getModel already orders them as TG does (scheduled
 * sources by schedule order, then the rest) and gives `scheduleRank` in every mode.
 */
export function groupSources(model: ModelData): SourceGroup[] {
  const map = new Map<string, SourceGroup & { order: number }>()
  ;(model.sources || []).forEach((s, index) => {
    const key = `${s.type}|${s.id}`
    let g = map.get(key)
    if (!g) {
      g = { key, type: s.type, id: s.id, provider: s.provider, channelId: sourceChannelId(s), items: [], live: false, sourceEnabled: false, containerEnabled: s.containerEnabled, enabledOverride: false, rank: null, order: s.displayOrder ?? index }
      map.set(key, g)
    }
    g.items.push(s)
    g.live ||= s.effectiveRoutable
    g.sourceEnabled ||= s.sourceEnabled
    g.enabledOverride ||= s.enabledOverride
    g.containerEnabled &&= s.containerEnabled
    if (s.scheduleRank != null) g.rank = g.rank == null ? s.scheduleRank : Math.min(g.rank, s.scheduleRank)
    g.order = Math.min(g.order, s.displayOrder ?? index)
  })
  return [...map.values()].sort((a, b) => Number(b.live) - Number(a.live) || a.order - b.order)
}

export function sourceName(s: ModelSource): string {
  const ch = sourceChannelId(s)
  const known = catalog.channels[ch]?.name
  if (s.type === 'api') return known || s.label.replace(/^api:/, '')
  const ident = known || s.id.split(':').slice(1).join(':') || s.label
  return `${providerLabel(s.provider)} · ${ident}`
}

const NOT_SCHEDULED: Record<string, string> = {
  accountRestricted: '账户受限，全部模型暂停',
  sourceDisabled: '本来源已停用这个模型',
  globalDisabled: '模型已全局停用',
  unavailable: '当前不可用',
}

/** Why a source is not scheduled (getModel `notScheduledReason`, TG wording). */
export function unavailableReason(model: ModelData, s: ModelSource): string {
  const code = s.notScheduledReason
  if (code === 'containerDisabled' || (!code && !s.containerEnabled)) return s.type === 'oauth' ? '账户已停用' : '渠道已停用'
  if (code && NOT_SCHEDULED[code]) {
    if (code === 'unavailable' && s.unavailableReason && /[\u4e00-\u9fff]/.test(s.unavailableReason)) return s.unavailableReason
    return NOT_SCHEDULED[code]
  }
  if (!s.sourceEnabled) return NOT_SCHEDULED.sourceDisabled
  if (model.globalEnabled === false && !s.enabledOverride) return NOT_SCHEDULED.globalDisabled
  return NOT_SCHEDULED.unavailable
}

export type Tone = 'neutral' | 'blue' | 'green' | 'orange' | 'red' | 'purple' | 'teal'

const STATE: Record<string, { label: string; tone: Tone }> = {
  sourceOverride: { label: '来源例外启用', tone: 'orange' },
  globalDisabled: { label: '全局停用', tone: 'neutral' },
  accountDisabled: { label: '账户停用', tone: 'red' },
  channelDisabled: { label: '渠道停用', tone: 'red' },
  sourceContainerDisabled: { label: '来源停用', tone: 'red' },
  sourceDisabled: { label: '来源停用', tone: 'neutral' },
  noSources: { label: '无来源', tone: 'neutral' },
  unavailable: { label: '不可用', tone: 'red' },
  configured: { label: '已配置', tone: 'neutral' },
}

/** One concise list state (TG `_list_model_state`, served as `stateCode`). */
export function modelStatus(m: ModelData): { label: string; tone: Tone } {
  if (m.stateCode === null) return { label: '可用', tone: 'green' }
  if (m.stateCode && STATE[m.stateCode]) return STATE[m.stateCode]
  // Older payloads without stateCode.
  const live = (m.sources || []).some((s) => s.effectiveRoutable)
  if (m.identity.type === 'video') return STATE.configured
  if (m.globalEnabled === false) return live ? STATE.sourceOverride : STATE.globalDisabled
  if (!(m.sources || []).length) return STATE.noSources
  return live ? { label: '可用', tone: 'green' } : STATE.unavailable
}

/** The state a list row shows: the model's own, or with a source filter that source's. */
export function displayStatus(m: ModelData, source?: ModelSource): { label: string; tone: Tone } {
  if (source) {
    if (source.effectiveRoutable) return { label: '本来源可用', tone: 'green' }
    if (!source.sourceEnabled) return { label: '本来源停用', tone: 'neutral' }
    return { label: '本来源不可用', tone: 'orange' }
  }
  return modelStatus(m)
}

const TONE_RANK: Record<string, number> = { green: 0, orange: 2, red: 3, neutral: 4 }
/** List order (and filter order): usable, configured, usable through an exception, broken, switched off. */
export function statusRank(m: ModelData, source?: ModelSource): number {
  if (!source && (m.stateCode === 'configured' || (m.stateCode === undefined && m.identity.type === 'video'))) return 1
  return TONE_RANK[displayStatus(m, source).tone] ?? 4
}

// ---- channel status (from runtime status) ----

const DISABLED_REASON: Record<string, string> = {
  auth_error: '认证失败',
  quota: '额度用尽',
  quota_exhausted: '额度用尽',
  user: '已停用',
}
const HEALTH_LABEL: Record<string, string> = {
  healthy: '正常',
  unknown: '暂无流量',
  degraded: '成功率下降',
  cooldown: '冷却中',
  quotaCooldown: '额度冷却',
  unhealthy: '成功率低',
  permanentCooldown: '永久冷却',
  disabled: '已停用',
}

export function channelState(id: string): { label: string; tone: Tone; exists: boolean; title: string } {
  const ch = catalog.status?.channels.find((c) => c.id === id)
  if (!ch) return { label: catalog.status ? '已不存在' : '—', tone: catalog.status ? 'red' : 'neutral', exists: !catalog.status, title: '' }
  if (!ch.enabled) return { label: DISABLED_REASON[ch.disabledReason || ''] || '已停用', tone: ch.disabledReason === 'user' || !ch.disabledReason ? 'neutral' : 'red', exists: true, title: '' }
  const tone: Tone =
    ch.health === 'healthy' ? 'green' : ch.health === 'unknown' ? 'neutral' : ch.health === 'unhealthy' || ch.health === 'permanentCooldown' ? 'red' : 'orange'
  // Say which models: the channel health is the lowest per-model recent success rate.
  const title = [
    ...(ch.cooldowns || []).map((c) => `${c.model} ${c.state === 'permanent' ? '永久冷却' : c.quota ? '额度冷却' : '冷却中'}`),
    ...(ch.weakModels || []).map((w) => `${w.model} 最近 ${w.recentRequests} 次成功 ${pctValue(w.successRate)}`),
  ].join('\n')
  return { label: HEALTH_LABEL[ch.health] || '未知', tone, exists: true, title }
}

export function channelTypeLabel(id: string): string {
  if (id.startsWith('oauth:')) return providerLabel(id.slice(6).split(':')[0])
  if (id.startsWith('api:')) return 'API'
  return '其他'
}

export { channelName }

/** Channel display name with its provider, e.g. "WorkBuddy · 小枕头". */
export function channelLabel(id: string | null | undefined): string {
  if (!id) return '—'
  const name = channelName(id)
  return id.startsWith('oauth:') ? `${channelTypeLabel(id)} · ${name}` : name
}

// ---- metadata formatting ----



export function priceSummary(v: MetadataValues | null | undefined): string {
  const c = v?.cost
  if (!c || (c.input == null && c.output == null)) return ''
  if (c.input === 0 && c.output === 0) return '免费'
  return `${price(c.input)} / ${price(c.output)}`
}

export function capabilityTags(v: MetadataValues | null | undefined): { key: string; label: string }[] {
  if (!v) return []
  const out: { key: string; label: string }[] = []
  if (v.vision) out.push({ key: 'vision', label: '识图' })
  if (v.toolCall) out.push({ key: 'tool', label: '工具' })
  if (v.reasoningEfforts?.length) out.push({ key: 'reason', label: '思考' })
  if (v.structuredOutput) out.push({ key: 'json', label: '结构化' })
  return out
}

/** Labels of the overridable metadata fields, in the TG order. */
export interface MetaField {
  key: string
  label: string
  group: 'capacity' | 'capability' | 'price'
  kind: 'tokens' | 'bool' | 'list' | 'date' | 'price'
  hint?: string
}
export const META_FIELDS: MetaField[] = [
  { key: 'contextWindow', label: '上下文', group: 'capacity', kind: 'tokens', hint: '一次请求能容纳的 Token 上限' },
  { key: 'maxInputTokens', label: '最大输入', group: 'capacity', kind: 'tokens' },
  { key: 'maxOutputTokens', label: '最大输出', group: 'capacity', kind: 'tokens' },
  { key: 'compactTriggerTokens', label: '压缩阈值', group: 'capacity', kind: 'tokens', hint: '超过后触发上下文压缩，不等于最终容量' },
  { key: 'vision', label: '图片输入', group: 'capability', kind: 'bool' },
  { key: 'toolCall', label: '工具调用', group: 'capability', kind: 'bool' },
  { key: 'structuredOutput', label: '结构化输出', group: 'capability', kind: 'bool' },
  { key: 'reasoningEfforts', label: '思考档位', group: 'capability', kind: 'list', hint: '如 low, medium, high' },
  { key: 'serviceTiers', label: '服务档位', group: 'capability', kind: 'list', hint: '如 default, fast' },
  { key: 'knowledgeCutoff', label: '知识截止', group: 'capability', kind: 'date', hint: 'YYYY-MM 或 YYYY-MM-DD' },
  { key: 'cost.input', label: '输入价格', group: 'price', kind: 'price' },
  { key: 'cost.output', label: '输出价格', group: 'price', kind: 'price' },
  { key: 'cost.cacheRead', label: '缓存读取价格', group: 'price', kind: 'price' },
  { key: 'cost.cacheWrite', label: '缓存写入价格', group: 'price', kind: 'price' },
  { key: 'cost.longContextInput', label: '长上下文输入价格', group: 'price', kind: 'price' },
  { key: 'cost.longContextOutput', label: '长上下文输出价格', group: 'price', kind: 'price' },
]
export const META_GROUP_LABEL: Record<string, string> = { capacity: '容量', capability: '能力', price: '价格（美元 / 百万 Token）' }

export function metaValue(v: MetadataValues | Record<string, unknown> | null | undefined, key: string): unknown {
  if (!v) return undefined
  if (key in v) return (v as Record<string, unknown>)[key]
  if (key.startsWith('cost.')) {
    const c = (v as Record<string, unknown>).cost as Record<string, unknown> | null | undefined
    return c ? c[key.slice(5)] : undefined
  }
  return (v as Record<string, unknown>)[key]
}

export function formatMeta(field: MetaField, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  switch (field.kind) {
    case 'tokens':
      return typeof value === 'number' ? `${tokens(value)}（${value.toLocaleString('en-US')}）` : String(value)
    case 'bool':
      return value ? '支持' : '不支持'
    case 'list': {
      const arr = Array.isArray(value) ? (value as string[]) : []
      if (!arr.length) return '无'
      const labels = field.key === 'reasoningEfforts' ? EFFORT_LABEL : field.key === 'serviceTiers' ? TIER_LABEL : {}
      return arr.map((x) => labels[x] || x).join('、')
    }
    case 'price':
      return typeof value === 'number' ? price(value) : String(value)
    default:
      return String(value)
  }
}

/** Override values stored in commonOverride / sourceOverride (nested cost or flat "cost.x"). */
export function overrideKeys(o: Record<string, unknown> | null | undefined): string[] {
  if (!o) return []
  const out: string[] = []
  for (const [k, v] of Object.entries(o)) {
    if (k === 'cost' && v && typeof v === 'object') for (const ck of Object.keys(v as object)) out.push(`cost.${ck}`)
    else out.push(k)
  }
  return out
}

// ---- operation results ----

export const SYNC_RESULT_LABEL: Record<string, string> = {
  created: '新增',
  updated: '已更新',
  unchanged: '未变化',
  protected: '受保护（保留人工设置）',
  unmatched: '未匹配到目录',
  missing: '模型已不存在',
  failed: '失败',
  scanned: '扫描',
}

export function syncResultLines(result: unknown): string[] {
  if (!result || typeof result !== 'object') return []
  const r = result as Record<string, unknown>
  const lines: string[] = []
  for (const [k, label] of Object.entries(SYNC_RESULT_LABEL)) {
    const v = r[k]
    const n = Array.isArray(v) ? v.length : typeof v === 'number' ? v : null
    if (n !== null && (n > 0 || k === 'scanned')) lines.push(`${label} ${n} 个`)
  }
  if (r.catalog === 'updated') lines.push('公共目录已更新')
  else if (r.catalog === 'local') lines.push('使用本地目录匹配')
  return lines
}

// ---- source details ----

/** Read a field from a source's raw effectiveMetadata (catalog cost keys use snake_case). */
export function rawMetaValue(raw: Record<string, unknown> | null | undefined, key: string): unknown {
  if (!raw) return undefined
  if (!key.startsWith('cost.')) return raw[key]
  const cost = raw.cost as Record<string, unknown> | undefined
  if (!cost) return undefined
  const k = key.slice(5)
  const snake: Record<string, string[]> = {
    cacheRead: ['cache_read', 'cacheRead'],
    cacheWrite: ['cache_write', 'cacheWrite'],
    longContextInput: ['longContextInput'],
    longContextOutput: ['longContextOutput'],
  }
  if (k === 'longContextInput' || k === 'longContextOutput') {
    const tier = cost.context_over_200k as Record<string, unknown> | undefined
    if (tier && tier[k === 'longContextInput' ? 'input' : 'output'] !== undefined) return tier[k === 'longContextInput' ? 'input' : 'output']
  }
  for (const name of snake[k] || [k]) if (cost[name] !== undefined) return cost[name]
  return undefined
}

export const WINDOW_LABEL: Record<string, string> = { fiveHour: '5 小时', sevenDay: '7 天', thirtyDay: '30 天', daily: '每日', weekly: '7 天', monthly: '每月' }

export const STATUS_TAG: Record<string, { label: string; tone: Tone }> = {
  userDisabled: { label: '已停用', tone: 'neutral' },
  quotaDisabled: { label: '额度用尽', tone: 'orange' },
  authFailed: { label: '认证失败', tone: 'red' },
  providerRestricted: { label: '供应商限制', tone: 'red' },
}

/** Shared (scheduled-sources-common) value from `sharedMetadata`, TG detail head block. */
export function sharedValue(m: ModelData, key: string): unknown {
  return metaValue(m.sharedMetadata as Record<string, unknown> | undefined, key)
}
