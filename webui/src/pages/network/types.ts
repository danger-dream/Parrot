import type { Schemas } from '@/api/client'

export type NetworkSettings = Schemas['NetworkSettingsData']

/** One-time tested plan returned inside a test Operation's result. */
export interface TestPlan {
  id: string
  kind: 'dns' | 'socks5'
  passed: boolean
  tested: { servers?: string[]; configured?: boolean; maskedUrl?: string }
  result: {
    ok: boolean
    servers?: string[]
    results?: {
      label?: string
      url?: string
      host?: string
      resolve?: boolean
      http?: boolean
      ok?: boolean
      error?: string
      ips?: string[]
    }[]
  }
  createdAt: string
  expiresAt: string
  revision: string
}

export const CATEGORY_LABEL: Record<string, string> = {
  dns: 'DNS',
  socks5: 'SOCKS5',
  channel: '渠道',
  core: '核心上游',
}

/** Mask the password (and user) part of a proxy URL for display. */
export function maskUrl(url: string): string {
  const s = url.trim()
  if (!s) return ''
  return s.replace(/^([a-z0-9+.-]+:\/\/)?([^@/]+)@/i, (_m, scheme: string | undefined) => `${scheme || ''}***:***@`)
}
