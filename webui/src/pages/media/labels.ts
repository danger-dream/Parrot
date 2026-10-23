export type MediaStatus = 'running' | 'pending' | 'success' | 'failed' | 'expired' | 'cancelled'
export type MediaAction = 'generate' | 'edit' | 'extend'
export type Tone = 'green' | 'red' | 'orange' | 'blue' | 'neutral'

export const STATUS_LABEL: Record<string, string> = {
  running: '生成中',
  pending: '排队中',
  success: '成功',
  failed: '失败',
  expired: '已过期',
  cancelled: '已取消',
}

export const STATUS_TONE: Record<string, Tone> = {
  running: 'blue',
  pending: 'blue',
  success: 'green',
  failed: 'red',
  expired: 'neutral',
  cancelled: 'orange',
}

export const ACTION_LABEL: Record<string, string> = { generate: '生成', edit: '编辑', extend: '延长' }

export const PROVIDER_LABEL: Record<string, string> = {
  openai: 'GPT',
  xai: 'Grok',
  antigravity: 'Gemini',
  google: 'Gemini',
}

export const CACHE_LABEL: Record<string, string> = { cached: '已缓存', disabled: '未开启缓存', failed: '缓存失败' }

export function statusLabel(s: string | null | undefined): string {
  return STATUS_LABEL[s || ''] || s || '未知'
}
export function statusTone(s: string | null | undefined): Tone {
  return STATUS_TONE[s || ''] || 'neutral'
}
export function actionLabel(a: string | null | undefined): string {
  return ACTION_LABEL[a || ''] || a || '—'
}
export function providerLabel(p: string | null | undefined): string {
  return PROVIDER_LABEL[p || ''] || p || '—'
}
export function mediaLabel(t: string | null | undefined): string {
  return t === 'video' ? '视频' : '图片'
}

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`
}

export function seconds(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return Number.isInteger(v) ? `${v} 秒` : `${v.toFixed(1)} 秒`
}
