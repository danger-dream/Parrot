import { reactive } from 'vue'
import { api, ApiError, onUnauthorized } from '@/api/client'
import { connectEvents, disconnectEvents } from '@/api/events'
import type { SessionSummary } from '@/api/types'

export type SessionStatus = 'unknown' | 'authenticated' | 'anonymous'

export const session = reactive({
  status: 'unknown' as SessionStatus,
  summary: null as SessionSummary | null,
})

export async function loadSession(): Promise<boolean> {
  try {
    const res = await api.get<SessionSummary>('/auth/session')
    session.summary = res.data
    session.status = 'authenticated'
    connectEvents()
    return true
  } catch (err) {
    session.summary = null
    session.status = 'anonymous'
    if (!(err instanceof ApiError) || !err.isAuth) throw err
    return false
  }
}

export async function logout(): Promise<void> {
  try {
    await api.del('/auth/session')
  } catch {
    /* already gone */
  }
  markSignedOut()
}

export function markSignedOut(): void {
  disconnectEvents()
  session.summary = null
  session.status = 'anonymous'
}

export function deviceSummary(): string {
  const ua = navigator.userAgent
  const os = /iPhone|iPad/.test(ua)
    ? 'iOS'
    : /Android/.test(ua)
      ? 'Android'
      : /Mac OS X/.test(ua)
        ? 'macOS'
        : /Windows/.test(ua)
          ? 'Windows'
          : /Linux/.test(ua)
            ? 'Linux'
            : '未知系统'
  const browser = /Edg\//.test(ua)
    ? 'Edge'
    : /Firefox\//.test(ua)
      ? 'Firefox'
      : /Chrome\//.test(ua)
        ? 'Chrome'
        : /Safari\//.test(ua)
          ? 'Safari'
          : '浏览器'
  const kind = /Mobi|iPhone|Android/.test(ua) ? '手机' : '电脑'
  return `${browser} · ${os} · ${kind}`
}

let onSignedOut: (() => void) | null = null

export function setSignedOutHandler(fn: () => void): void {
  onSignedOut = fn
}

onUnauthorized(() => {
  if (session.status !== 'authenticated') return
  markSignedOut()
  onSignedOut?.()
})
