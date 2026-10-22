import { reactive, shallowReactive } from 'vue'
import { onEvent } from '@/api/events'
import type { ConcurrencyData, RequestLogData, RequestStartedEvent } from '@/api/types'

export interface LiveRequest {
  id: string
  createdAt: string
  apiKeyName: string | null
  requestedModel: string | null
  finalModel: string | null
  channelId: string | null
  protocol: string | null
  isStream: boolean
  retryCount: number
  status: string
  durationMilliseconds: number | null
  inputTokens: number
  outputTokens: number
  costTicks: number
  error: string | null
  log: RequestLogData | null
  receivedAt: number
}

const WINDOW_SECONDS = 15 * 60
const MAX_FEED = 400

export const live = reactive({
  feed: [] as LiveRequest[],
  inFlight: shallowReactive(new Map<string, LiveRequest>()),
  snapshot: null as ConcurrencyData | null,
  snapshotAt: 0,
  started: 0,
  finished: 0,
  failed: 0,
  resyncs: 0,
  paused: false,
})

// Per-second buckets of finished requests for the last 15 minutes.
const bucketStart = Math.floor(Date.now() / 1000) - WINDOW_SECONDS + 1
export const qps = reactive({
  origin: bucketStart,
  ok: new Array<number>(WINDOW_SECONDS).fill(0),
  err: new Array<number>(WINDOW_SECONDS).fill(0),
  version: 0,
})

function advance(nowSec: number): void {
  const last = qps.origin + WINDOW_SECONDS - 1
  if (nowSec <= last) return
  const shift = Math.min(WINDOW_SECONDS, nowSec - last)
  qps.ok.splice(0, shift)
  qps.err.splice(0, shift)
  for (let i = 0; i < shift; i++) {
    qps.ok.push(0)
    qps.err.push(0)
  }
  qps.origin += shift
}

function count(ok: boolean, atMs: number): void {
  const sec = Math.floor(atMs / 1000)
  advance(Math.floor(Date.now() / 1000))
  const idx = sec - qps.origin
  if (idx < 0 || idx >= WINDOW_SECONDS) return
  if (ok) qps.ok[idx] += 1
  else qps.err[idx] += 1
  qps.version += 1
}

window.setInterval(() => {
  advance(Math.floor(Date.now() / 1000))
  qps.version += 1
}, 1000)

export function seedQps(points: { t: string; total: number; errorCount: number }[], bucketSeconds: number): void {
  // Spread minute buckets from the time-series endpoint evenly across their seconds.
  for (const p of points) {
    const start = Math.floor(new Date(p.t).getTime() / 1000)
    for (let s = 0; s < bucketSeconds; s++) {
      const idx = start + s - qps.origin
      if (idx < 0 || idx >= WINDOW_SECONDS) continue
      qps.ok[idx] = Math.max(qps.ok[idx], (p.total - p.errorCount) / bucketSeconds)
      qps.err[idx] = Math.max(qps.err[idx], p.errorCount / bucketSeconds)
    }
  }
  qps.version += 1
}

function fromLog(log: RequestLogData, prev?: LiveRequest): LiveRequest {
  return {
    id: log.id,
    createdAt: log.createdAt || prev?.createdAt || new Date().toISOString(),
    apiKeyName: log.apiKeyName ?? prev?.apiKeyName ?? null,
    requestedModel: log.requestedModel ?? prev?.requestedModel ?? null,
    finalModel: log.finalModel ?? null,
    channelId: log.channelId ?? prev?.channelId ?? null,
    protocol: log.protocol ?? prev?.protocol ?? null,
    isStream: prev?.isStream ?? false,
    retryCount: log.retryCount,
    status: log.status,
    durationMilliseconds: log.durationMilliseconds ?? null,
    inputTokens: log.inputTokens,
    outputTokens: log.outputTokens,
    costTicks: log.costTicks,
    error: log.error ?? null,
    log,
    receivedAt: Date.now(),
  }
}

function pushFeed(item: LiveRequest): void {
  const index = live.feed.findIndex((r) => r.id === item.id)
  if (index >= 0) live.feed.splice(index, 1, item)
  else {
    live.feed.unshift(item)
    if (live.feed.length > MAX_FEED) live.feed.length = MAX_FEED
  }
}

onEvent('request.started', (e: RequestStartedEvent) => {
  live.started += 1
  const item: LiveRequest = {
    id: e.id,
    createdAt: e.createdAt,
    apiKeyName: e.apiKeyName,
    requestedModel: e.requestedModel,
    finalModel: null,
    channelId: null,
    protocol: e.protocol,
    isStream: e.isStream,
    retryCount: 0,
    status: 'pending',
    durationMilliseconds: null,
    inputTokens: 0,
    outputTokens: 0,
    costTicks: 0,
    error: null,
    log: null,
    receivedAt: Date.now(),
  }
  live.inFlight.set(e.id, item)
  if (!live.paused) pushFeed(item)
})

onEvent('request.updated', (e) => {
  const cur = live.inFlight.get(e.id)
  if (!cur) return
  const next = {
    ...cur,
    channelId: e.channelId ?? cur.channelId,
    finalModel: e.model ?? cur.finalModel,
    retryCount: e.retryCount ?? cur.retryCount,
  }
  live.inFlight.set(e.id, next)
  if (!live.paused && live.feed.some((r) => r.id === e.id)) pushFeed(next)
})

onEvent('request.finished', ({ log }) => {
  const prev = live.inFlight.get(log.id)
  live.inFlight.delete(log.id)
  const item = fromLog(log, prev)
  live.finished += 1
  const ok = log.status === 'success'
  if (!ok) live.failed += 1
  count(ok, Date.now())
  if (!live.paused) pushFeed(item)
})

onEvent('runtime.snapshot', (data) => {
  live.snapshot = data
  live.snapshotAt = Date.now()
})

onEvent('resync', () => {
  live.resyncs += 1
  live.inFlight.clear()
})

export function seedFeed(logs: RequestLogData[]): void {
  const known = new Set(live.feed.map((r) => r.id))
  for (const log of logs) {
    if (known.has(log.id)) continue
    live.feed.push(fromLog(log))
  }
  live.feed.sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
  if (live.feed.length > MAX_FEED) live.feed.length = MAX_FEED
}
