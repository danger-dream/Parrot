/**
 * One wording for reasoning-effort values on every page: OpenAI-style levels, plus GLM's
 * thinking switch, which the metadata stores as the pair "disabled" / "enabled".
 */
export const EFFORT_LABEL: Record<string, string> = {
  none: '关闭',
  disabled: '关闭',
  enabled: '开启',
  minimal: '最低',
  low: '低',
  medium: '中',
  high: '高',
  xhigh: '超高',
  max: '最高',
  ultra: '极限',
}

export function effortLabel(v: string | null | undefined): string {
  if (!v) return '—'
  return EFFORT_LABEL[v] || v
}

export function effortList(list: readonly string[] | null | undefined): string {
  return (list || []).map(effortLabel).join(' / ')
}
