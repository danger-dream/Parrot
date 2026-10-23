export const PROVIDERS = [
  { value: 'claude', label: 'Claude' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'cloudflare', label: 'Cloudflare' },
] as const

export type Provider = (typeof PROVIDERS)[number]['value']

export function providerLabel(p: string): string {
  return PROVIDERS.find((x) => x.value === p)?.label || p
}

export const IMPACT: Record<string, { label: string; tone: 'neutral' | 'blue' | 'orange' | 'red' | 'purple' }> = {
  none: { label: '无影响', tone: 'neutral' },
  maintenance: { label: '计划维护', tone: 'blue' },
  minor: { label: '轻微', tone: 'orange' },
  major: { label: '严重', tone: 'red' },
  critical: { label: '重大故障', tone: 'purple' },
}

export const MIN_IMPACT_OPTIONS = [
  { value: 'none', label: '全部（含无影响）' },
  { value: 'minor', label: '轻微及以上（默认）' },
  { value: 'major', label: '严重及以上' },
  { value: 'critical', label: '仅重大故障' },
] as const

export const STATUS: Record<string, string> = {
  investigating: '调查中',
  identified: '已定位原因',
  monitoring: '修复后观察中',
  resolved: '已解决',
  postmortem: '已复盘',
  scheduled: '已计划',
  in_progress: '维护中',
  verifying: '验证中',
  completed: '已完成',
}

export function statusLabel(s: string): string {
  return STATUS[s] || '状态未知'
}
