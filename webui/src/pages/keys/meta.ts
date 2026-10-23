import { reactive } from 'vue'
import { api, type Schemas } from '@/api/client'

export type ApiKey = Schemas['ApiKeyData']
export type ApiKeyUsage = Schemas['ApiKeyUsageData']
export type Limiter = Schemas['ApiKeyLimiterData']
export type McpTool = ApiKey['mcpTools'][number]

/** Bumped by the detail panel / dialogs so the list page reloads. */
export const keyBus = reactive({ version: 0 })
export function notifyKeysChanged(): void {
  keyBus.version++
}

export const NAME_RE = /^[A-Za-z0-9_.-]{1,64}$/
export const SECRET_RE = /^[A-Za-z0-9\-_.~+/=]{8,256}$/

export function nameError(name: string, existing: string[] = []): string {
  const n = name.trim()
  if (!n) return '请填写名称'
  if (!NAME_RE.test(n)) return '只能使用字母、数字和 _ - .，最长 64 个字符'
  if (existing.includes(n)) return '已有同名 Key，请换一个'
  return ''
}
export function secretError(secret: string): string {
  if (!secret) return '请填写密钥'
  if (secret.length < 8) return '密钥太短，至少 8 个字符'
  if (secret.length > 256) return '密钥太长，最多 256 个字符'
  if (!SECRET_RE.test(secret)) return '只能使用字母、数字和 - _ . ~ + / =，不能有空格或换行'
  return ''
}

export const MCP_TOOLS: { value: McpTool; label: string }[] = [
  { value: 'web_search', label: '网络搜索' },
  { value: 'web_fetch', label: '网页抓取' },
  { value: 'image_generate', label: '图片生成' },
  { value: 'image_edit', label: '图片编辑' },
  { value: 'video_generate', label: '视频生成' },
  { value: 'video_status', label: '视频查询' },
]

export const SOURCE_LABEL: Record<string, string> = { generated: '自动生成', custom: '自定义', unknown: '来源未知' }

export function sourceLabel(src: string): string {
  return src === 'key' ? '单独设置' : '继承默认'
}

export function waitText(seconds: number): string {
  if (!seconds) return '不等待'
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`
  return `${seconds} 秒`
}

interface ModelRow {
  modelId: string
  aliases: string[]
  globalEnabled: boolean
  visible: boolean
  identity: { type: string }
  sources: { effectiveRoutable: boolean }[]
}

let modelCache: { at: number; list: { value: string; hint?: string }[] } | null = null

/** Client-visible models that are currently routable (text, image and video). */
export async function loadModelOptions(force = false): Promise<{ value: string; hint?: string }[]> {
  if (!force && modelCache && Date.now() - modelCache.at < 60_000) return modelCache.list
  const rows: ModelRow[] = []
  let page = 1
  for (;;) {
    const res = await api.get<ModelRow[]>('/models', { page, pageSize: 200 })
    rows.push(...res.data)
    if (!res.meta.hasNext) break
    page++
  }
  const kind: Record<string, string> = { image: '图片模型', video: '视频模型' }
  const seen = new Set<string>()
  const list: { value: string; hint?: string }[] = []
  for (const r of rows) {
    if (!r.globalEnabled || !r.visible || !r.sources.some((s) => s.effectiveRoutable)) continue
    for (const id of [r.modelId, ...r.aliases]) {
      if (!id || seen.has(id)) continue
      seen.add(id)
      list.push({ value: id, hint: id === r.modelId ? kind[r.identity.type] : `${r.modelId} 的别名` })
    }
  }
  list.sort((a, b) => a.value.localeCompare(b.value))
  modelCache = { at: Date.now(), list }
  return list
}

/** Full key value: listApiKeys / getApiKey return it to admin sessions, null otherwise. */
export function keySecret(k: ApiKey | null | undefined): string | null {
  return k?.secret || null
}

/** Limiter brief like the bot: "0/20 在途 · 0/50 排队 · 最长 30m", or "关闭". */
export function limitBrief(l: Limiter, live?: { inFlight: number; waiting: number } | null): string {
  if (!l.enabled) return '关闭'
  const max = l.unlimited ? '∞' : String(l.maxConcurrent)
  return `${live?.inFlight ?? l.inFlight}/${max} 在途 · ${live?.waiting ?? l.waiting}/${l.maxQueue} 排队 · 最长 ${waitText(l.queueWaitSeconds)}`
}
