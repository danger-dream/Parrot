import { reactive, shallowReactive } from 'vue'
import { api } from '@/api/client'
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
const RESYNC_PAGE_SIZE = 200
const IDLE_RECONCILE_MS = 10_000

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

let syncSeq = 0
let syncing = false
let syncQueued = false
let syncTotal: number | null = null
let idlePendingSince = 0
let lastIdleReconcileAt = 0

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

function fromLog(log: RequestLogData, prev?: LiveRequest, receivedAt = Date.now()): LiveRequest {
  return {
    id: log.id,
    createdAt: log.createdAt || prev?.createdAt || new Date().toISOString(),
    apiKeyName: log.apiKeyName ?? prev?.apiKeyName ?? null,
    requestedModel: log.requestedModel ?? prev?.requestedModel ?? null,
    finalModel: log.finalModel ?? null,
    channelId: log.channelId ?? prev?.channelId ?? null,
    protocol: log.protocol ?? prev?.protocol ?? null,
    isStream: log.isStream ?? prev?.isStream ?? false,
    retryCount: log.retryCount,
    status: log.status,
    durationMilliseconds: log.durationMilliseconds ?? null,
    inputTokens: log.inputTokens,
    outputTokens: log.outputTokens,
    costTicks: log.costTicks,
    error: log.error ?? null,
    log,
    receivedAt,
  }
}

function pushFeed(item: LiveRequest): void {
  const index = live.feed.findIndex((r) => r.id === item.id)
  if (index >= 0) {
    if (item.status === 'pending' && live.feed[index].status !== 'pending') return
    live.feed.splice(index, 1, item)
  } else {
    live.feed.unshift(item)
    if (live.feed.length > MAX_FEED) live.feed.length = MAX_FEED
  }
}

function hasPendingFeedItems(): boolean {
  return live.inFlight.size > 0 || live.feed.some((r) => r.status === 'pending')
}

function snapshotInFlightCount(data: ConcurrencyData): number {
  return Math.max(data.channelTotals?.inFlight ?? 0, data.apiKeyTotals?.inFlight ?? 0)
}

onEvent('request.started', (e: RequestStartedEvent) => {
  live.started += 1
  const existing = live.feed.find((r) => r.id === e.id)
  if (existing && existing.status !== 'pending') return
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
  if (snapshotInFlightCount(data) > 0 || !hasPendingFeedItems()) {
    idlePendingSince = 0
    return
  }
  const now = Date.now()
  if (!idlePendingSince) {
    idlePendingSince = now
    return
  }
  if (!live.paused && now - idlePendingSince >= IDLE_RECONCILE_MS && now - lastIdleReconcileAt >= IDLE_RECONCILE_MS) {
    lastIdleReconcileAt = now
    void syncFeed({ reconcilePending: true })
  }
})

onEvent('resync', () => {
  live.resyncs += 1
  live.inFlight.clear()
  idlePendingSince = 0
  if (!live.paused) void syncFeed({ reconcilePending: true })
})

export function seedFeed(logs: RequestLogData[], opts?: { reconcilePending?: boolean; snapshotAt?: number }): void {
  const reconcilePending = Boolean(opts?.reconcilePending)
  const snapshotAt = opts?.snapshotAt ?? 0
  const byId = new Map<string, number>()
  for (let i = 0; i < live.feed.length; i++) byId.set(live.feed[i].id, i)

  const activePendingIds = new Set<string>()

  for (const log of logs) {
    const existingIndex = byId.get(log.id)
    const existing = existingIndex !== undefined ? live.feed[existingIndex] : undefined
    const prev = live.inFlight.get(log.id) ?? existing

    if (log.status === 'pending') {
      if (existing && existing.status !== 'pending') continue
      activePendingIds.add(log.id)
      const item = fromLog(log, prev, existing?.receivedAt ?? 0)
      live.inFlight.set(log.id, item)
      if (existingIndex !== undefined) {
        live.feed.splice(existingIndex, 1, item)
      } else {
        byId.set(log.id, live.feed.length)
        live.feed.push(item)
      }
      continue
    }

    live.inFlight.delete(log.id)
    const item = fromLog(log, prev, 0)
    if (existingIndex !== undefined) {
      // Replace stale pending rows with the authoritative terminal record from /logs.
      if (existing!.status === 'pending') live.feed.splice(existingIndex, 1, item)
    } else {
      byId.set(log.id, live.feed.length)
      live.feed.push(item)
    }
  }

  if (reconcilePending) {
    for (let i = live.feed.length - 1; i >= 0; i--) {
      const row = live.feed[i]
      if (row.status !== 'pending' || activePendingIds.has(row.id)) continue
      // Keep requests that arrived while the /logs fetch was in flight.
      if (snapshotAt > 0 && row.receivedAt >= snapshotAt) continue
      live.inFlight.delete(row.id)
      live.feed.splice(i, 1)
    }
    for (const [id, row] of Array.from(live.inFlight.entries())) {
      if (activePendingIds.has(id)) continue
      if (snapshotAt > 0 && row.receivedAt >= snapshotAt) continue
      live.inFlight.delete(id)
    }
  }

  live.feed.sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0))
  if (live.feed.length > MAX_FEED) live.feed.length = MAX_FEED
}

export async function syncFeed(opts?: { reconcilePending?: boolean }): Promise<number | null> {
  if (syncing) {
    syncQueued = true
    return syncTotal
  }
  syncing = true
  const reconcilePending = opts?.reconcilePending ?? true
  try {
    do {
      syncQueued = false
      const seq = ++syncSeq
      const snapshotAt = Date.now()
      const res = await api.get<RequestLogData[]>('/logs', {
        pageSize: RESYNC_PAGE_SIZE,
        sort: 'createdAt',
        descending: true,
      })
      if (seq !== syncSeq) continue
      let items = res.data
      if (reconcilePending && res.data.length >= RESYNC_PAGE_SIZE) {
        const returnedIds = new Set(res.data.map((r) => r.id))
        const hasOlderPending =
          live.feed.some((r) => r.status === 'pending' && !returnedIds.has(r.id) && r.receivedAt < snapshotAt) ||
          Array.from(live.inFlight.values()).some((r) => !returnedIds.has(r.id) && r.receivedAt < snapshotAt)
        if (hasOlderPending) {
          const pendingRes = await api.get<RequestLogData[]>('/logs', {
            status: 'pending',
            pageSize: RESYNC_PAGE_SIZE,
            sort: 'createdAt',
            descending: true,
          })
          if (seq !== syncSeq) continue
          items = [...res.data, ...pendingRes.data.filter((r) => !returnedIds.has(r.id))]
        }
      }
      syncTotal = res.meta.total ?? res.data.length
      seedFeed(items, { reconcilePending, snapshotAt })
      if (!hasPendingFeedItems()) idlePendingSince = 0
    } while (syncQueued && !live.paused)
    return syncTotal
  } catch {
    return null
  } finally {
    syncing = false
  }
}
