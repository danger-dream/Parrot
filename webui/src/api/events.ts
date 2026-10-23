import { reactive } from 'vue'
import { API_BASE } from './client'
import type { ServerEventMap, ServerEventType } from './types'

export type LinkState = 'idle' | 'connecting' | 'open' | 'retrying' | 'unauthorized' | 'closed'

type Handler<K extends ServerEventType> = (data: ServerEventMap[K], ts: string) => void

const handlers = new Map<ServerEventType, Set<Handler<ServerEventType>>>()

export const eventLink = reactive({
  state: 'idle' as LinkState,
  lastMessageAt: 0,
  connectedAt: 0,
  retries: 0,
})

let socket: WebSocket | null = null
let retryTimer: number | undefined
let watchdog: number | undefined
let wanted = false

function url(): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${location.host}${API_BASE}/events`
}

function dispatch(type: ServerEventType, data: unknown, ts: string): void {
  const set = handlers.get(type)
  if (!set) return
  for (const fn of set) {
    try {
      fn(data as never, ts)
    } catch (err) {
      console.error('[events] handler failed', type, err)
    }
  }
}

function scheduleRetry(): void {
  if (!wanted) return
  eventLink.state = 'retrying'
  const delay = Math.min(30_000, 1000 * 2 ** Math.min(eventLink.retries, 5)) + Math.random() * 500
  eventLink.retries += 1
  retryTimer = window.setTimeout(open, delay)
}

function armWatchdog(): void {
  window.clearTimeout(watchdog)
  watchdog = window.setTimeout(() => socket?.close(4000, 'stale'), 65_000)
}

function open(): void {
  window.clearTimeout(retryTimer)
  if (!wanted) return
  eventLink.state = 'connecting'
  const ws = new WebSocket(url())
  socket = ws
  ws.onopen = () => {
    eventLink.state = 'open'
    eventLink.connectedAt = Date.now()
    eventLink.retries = 0
    armWatchdog()
  }
  ws.onmessage = (msg) => {
    eventLink.lastMessageAt = Date.now()
    armWatchdog()
    let parsed: { type?: ServerEventType; ts?: string; data?: unknown }
    try {
      parsed = JSON.parse(String(msg.data))
    } catch {
      return
    }
    if (parsed.type) dispatch(parsed.type, parsed.data ?? {}, parsed.ts || new Date().toISOString())
  }
  ws.onclose = (ev) => {
    if (socket !== ws) return
    socket = null
    window.clearTimeout(watchdog)
    if (ev.code === 4401) {
      eventLink.state = 'unauthorized'
      return
    }
    if (!wanted) {
      eventLink.state = 'closed'
      return
    }
    scheduleRetry()
  }
}

export function connectEvents(): void {
  wanted = true
  if (socket || eventLink.state === 'connecting') return
  open()
}

export function disconnectEvents(): void {
  wanted = false
  window.clearTimeout(retryTimer)
  window.clearTimeout(watchdog)
  socket?.close(1000, 'bye')
  socket = null
  eventLink.state = 'closed'
}

export function onEvent<K extends ServerEventType>(type: K, fn: Handler<K>): () => void {
  let set = handlers.get(type)
  if (!set) {
    set = new Set()
    handlers.set(type, set)
  }
  set.add(fn as Handler<ServerEventType>)
  return () => set!.delete(fn as Handler<ServerEventType>)
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && wanted && !socket) {
    eventLink.retries = 0
    open()
  }
})
