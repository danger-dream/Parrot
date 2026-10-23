import { reactive } from 'vue'
import type { Schemas } from '@/api/client'
export { bytes } from '@/utils/format'

export type Proxy = Schemas['ProxyData']
export type ProxyGroup = Schemas['ProxyGroupData']
export type Routing = Schemas['ProxyRoutingData']
export type ProxyStats = Schemas['ProxyRuntimeStats']

/** Bumped after any proxy / group / routing mutation so every view reloads. */
export const proxyBus = reactive({ version: 0 })
export function notifyProxiesChanged(): void {
  proxyBus.version++
}

export const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,30}$/

export function nameError(name: string, taken: string[] = []): string {
  const n = name.trim()
  if (!n) return '请填写名称'
  if (!NAME_RE.test(n)) return '只能用小写字母、数字、- 和 _，以字母或数字开头，最长 31 个字符'
  if (n === 'direct') return 'direct 是保留名（表示直连）'
  if (taken.includes(n)) return '名称已被占用'
  return ''
}

export function typeLabel(t: string): string {
  return t === 'socks5' ? 'SOCKS5' : t === 'ss2022' ? 'Shadowsocks' : '未知类型'
}


export interface ProbeItem {
  ok: boolean
  ip: string
  latencyMilliseconds: number | null
  traffic: { bytesUp: number; bytesDown: number; totalBytes: number }
  error: { code: string; message: string } | null
  name?: string
}
export interface ProbeOutcome {
  target: string
  results: ProbeItem | ProbeItem[]
}

/** Split a pasted proxy line into URL (without #fragment) and a suggested name. */
export function parseProxyLine(line: string, taken: string[]): { url: string; name: string; error: string } {
  const raw = line.trim()
  const hash = raw.indexOf('#')
  const url = hash >= 0 ? raw.slice(0, hash) : raw
  let frag = ''
  if (hash >= 0) {
    try {
      frag = decodeURIComponent(raw.slice(hash + 1)).trim()
    } catch {
      frag = raw.slice(hash + 1).trim()
    }
  }
  let error = ''
  const scheme = url.split('://')[0]?.toLowerCase()
  if (!url.includes('://')) error = '不是有效的代理地址'
  else if (!['ss', 'socks5', 'socks5h', 'socks'].includes(scheme)) error = '只支持 ss:// 和 socks5:// 地址'
  let name = frag.toLowerCase()
  if (!NAME_RE.test(name) || name === 'direct') {
    const at = url.lastIndexOf('@')
    const hostPort = (at >= 0 ? url.slice(at + 1) : url.split('://')[1] || '').split(/[/?]/)[0]
    name = (frag || hostPort)
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, '-')
      .replace(/^[-_]+|-+$/g, '')
      .slice(0, 31)
  }
  if (!name || !NAME_RE.test(name)) name = 'proxy'
  let candidate = name
  for (let i = 2; taken.includes(candidate) || candidate === 'direct'; i++) candidate = `${name.slice(0, 28)}-${i}`
  return { url, name: candidate, error }
}

export const PROVIDER_ROUTES: { key: string; label: string }[] = [
  { key: 'openai', label: 'OpenAI' },
  { key: 'xai', label: 'Grok' },
  { key: 'cursor', label: 'Cursor' },
  { key: 'antigravity', label: 'Antigravity' },
  { key: 'workbuddy', label: 'WorkBuddy' },
  { key: 'qoder', label: 'Qoder' },
  { key: 'zhipu', label: 'z.ai / 智谱' },
  { key: 'claude', label: 'Anthropic' },
]
export const LEGACY_FUNCTIONS: { key: string; label: string }[] = [
  { key: 'oauth_anthropic', label: '旧兼容 · Anthropic 家族' },
  { key: 'oauth_openai', label: '旧兼容 · OpenAI 家族' },
]

export function targetLabel(t: string | null | undefined): string {
  if (!t) return '继承'
  return t === 'direct' ? '直连' : t
}
