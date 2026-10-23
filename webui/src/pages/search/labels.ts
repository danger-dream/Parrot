import type { Schemas } from '@/api/client'

export type Settings = Schemas['SearchSettingsData']
export type Backend = Schemas['SearchBackendData']
export type BackendType = Backend['type']
export type Mode = Settings['functionMode']

export const TYPE_NAME: Record<string, string> = {
  anysearch: 'AnySearch',
  tavily: 'Tavily',
  exa: 'Exa',
  brave: 'Brave',
  openai: 'OpenAI OAuth',
  xai: 'xAI OAuth',
  anthropic: 'Anthropic OAuth',
  zhipu: '智谱 MCP',
}
export const BACKEND_TYPES: BackendType[] = ['anysearch', 'tavily', 'exa', 'brave', 'openai', 'xai', 'anthropic', 'zhipu']
export const KEY_TYPES = new Set<string>(['anysearch', 'tavily', 'exa', 'brave', 'zhipu'])
export const ACCOUNT_TYPES = new Set<string>(['openai', 'xai', 'anthropic', 'zhipu'])
export const EXTRACT_TYPES = new Set<string>(['anysearch', 'tavily', 'exa', 'openai', 'zhipu'])
/** OAuth sources may pin a model; key sources and Zhipu MCP have none. */
export const MODEL_TYPES = new Set<string>(['openai', 'xai', 'anthropic'])
export const ZHIPU_SITES = [
  { value: '', label: '中国站（默认）' },
  { value: 'https://open.bigmodel.cn', label: '中国站 open.bigmodel.cn' },
  { value: 'https://api.z.ai', label: '国际站 api.z.ai' },
]

export const MODES: { value: Mode; label: string; hint: string }[] = [
  { value: 'managed', label: 'Parrot 接管', hint: '由 Parrot 按下方顺序调用搜索来源' },
  { value: 'passthrough', label: '透传', hint: '原样交给上游模型自己处理' },
  { value: 'disabled', label: '禁用', hint: '直接返回错误，不会静默成功' },
]

export function kindLabel(type: string): string {
  if (type === 'zhipu') return 'Coding Plan MCP（Key / 已有账号）'
  return KEY_TYPES.has(type) ? 'API Key 来源' : 'OAuth 来源'
}

export function stateOf(b: Backend): { label: string; tone: 'green' | 'orange' | 'neutral' } {
  if (!b.enabled) return { label: '已停用', tone: 'neutral' }
  if (b.available) return { label: '可用', tone: 'green' }
  return { label: b.reason === 'no_eligible_accounts' ? '无可用账户' : '缺少凭据', tone: 'orange' }
}

export function credentialText(b: Backend): string {
  if (b.type === 'zhipu') return `Key ${b.keyCount} 个 · 账户 ${b.accountCount} 个`
  if (KEY_TYPES.has(b.type)) return `Key ${b.keyCount} 个`
  return b.accountIds.length ? `指定账户 ${b.accountIds.length} 个` : `符合条件账户 ${b.accountCount} 个`
}

export const OPERATION_LABEL: Record<string, string> = { search: '网页搜索', x_search: 'X 搜索', extract: '网页提取' }
export const ORIGIN_LABEL: Record<string, string> = { mcp: 'MCP 工具', managed_round: '对话内搜索', management_test: '手动测试' }
export const STATUS_LABEL: Record<string, string> = { running: '进行中', success: '成功', error: '失败' }
export const COST_SOURCE: Record<string, string> = { actual: '实际结算', estimated: '估算', unpriced: '未计价' }
export const CREDENTIAL_KIND: Record<string, string> = { api_key: 'API Key', oauth: 'OAuth 账户' }

const ERROR_LABEL: Record<string, string> = {
  empty_extract_response: '提取结果为空',
  invalid_search_response: '上游返回格式错误',
  search_backend_error: '来源配置错误',
  search_network_error: '网络错误',
  search_timeout: '超时',
  search_upstream_error: '上游返回错误',
  cancelled: '已取消',
}
export function errorLabel(code: string | null | undefined): string {
  if (!code) return ''
  return ERROR_LABEL[code] || code
}

export { dollars as usdFloat } from '@/utils/format'

export function tsDate(sec: number | null | undefined): Date | null {
  return sec ? new Date(sec * 1000) : null
}
