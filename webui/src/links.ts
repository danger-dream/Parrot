import { defineAsyncComponent } from 'vue'
import { router } from '@/router'
import { openInspector } from '@/stores/ui'
import { channelName } from '@/stores/catalog'

// Cross-page navigation. Every place that shows an account, channel, API key, model or request
// should make it clickable through these helpers so the UI stays linked end to end.

const AccountDetail = defineAsyncComponent(() => import('@/pages/oauth/AccountDetail.vue'))
const ChannelDetail = defineAsyncComponent(() => import('@/pages/channels/ChannelDetail.vue'))
const ApiKeyDetail = defineAsyncComponent(() => import('@/pages/keys/ApiKeyDetail.vue'))
const ModelDetail = defineAsyncComponent(() => import('@/pages/models/ModelDetail.vue'))
const RequestDetail = defineAsyncComponent(() => import('@/pages/live/RequestDetail.vue'))
const ProxyDetail = defineAsyncComponent(() => import('@/pages/proxies/ProxyDetail.vue'))

/** OAuth account id, e.g. "claude:someone@example.com". */
export function openAccount(accountId: string, title?: string): void {
  openInspector(`account:${accountId}`, title || '账号详情', AccountDetail, { id: accountId }, accountId)
}

/** Routing channel id: "api:Name" opens the API channel, "oauth:<accountId>" opens the account. */
export function openChannel(channelId: string | null | undefined): void {
  if (!channelId) return
  if (channelId.startsWith('oauth:')) {
    openAccount(channelId.slice('oauth:'.length), channelName(channelId))
    return
  }
  openInspector(`channel:${channelId}`, channelName(channelId), ChannelDetail, { id: channelId }, channelId)
}

/** API key id (the key's name). */
export function openApiKey(keyId: string | null | undefined): void {
  if (!keyId) return
  openInspector(`apikey:${keyId}`, keyId, ApiKeyDetail, { id: keyId }, '下游 API Key')
}

/** Client-visible model id, e.g. "claude-opus-5-5". */
export function openModel(modelId: string | null | undefined): void {
  if (!modelId) return
  openInspector(`model:${modelId}`, modelId, ModelDetail, { id: modelId }, '模型')
}

/** Proxy or proxy group by name; "direct" (直连) is not an object and is ignored. */
export function openProxy(kind: 'proxy' | 'group', id: string | null | undefined): void {
  if (!id || id === 'direct') return
  openInspector(`${kind === 'group' ? 'pgroup' : 'proxy'}:${id}`, id, ProxyDetail, { id, kind }, kind === 'group' ? '代理组' : '代理')
}

export function openRequest(id: string): void {
  openInspector(`req:${id}`, '请求详情', RequestDetail, { id }, id)
}

export interface LogFilter {
  status?: 'success' | 'error' | 'pending' | 'cancelled'
  model?: string
  channel?: string
  apiKey?: string
  query?: string
  /** RFC3339 or epoch ms */
  from?: string | number
  to?: string | number
}

/** Open the request history with filters applied. */
export function goLogs(filter: LogFilter = {}): void {
  const query: Record<string, string> = { mode: 'history' }
  for (const [k, v] of Object.entries(filter)) {
    if (v === undefined || v === null || v === '') continue
    query[k] = typeof v === 'number' ? new Date(v).toISOString() : String(v)
  }
  router.push({ path: '/live', query })
}

export function goPage(path: string, query?: Record<string, string>): void {
  router.push({ path, query })
}
