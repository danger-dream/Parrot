import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import * as Vue from 'vue'
import ts from 'typescript'

function transpile(path) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  return ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
}

const eventsJs = transpile('../src/api/events.ts')
const liveJs = transpile('../src/stores/live.ts')

function createHarness({ onGet } = {}) {
  let now = 1_700_000_000_000
  const OrigDate = Date
  class FakeDate extends OrigDate {
    constructor(...args) {
      if (args.length === 0) super(now)
      else super(...args)
    }
    static now() {
      return now
    }
  }

  const sockets = []
  const timers = new Map()
  let nextTimerId = 1
  const docListeners = new Map()

  class FakeWebSocket {
    constructor(url) {
      this.url = url
      this.closed = null
      sockets.push(this)
    }
    close(code, reason) {
      this.closed = { code, reason }
      this.onclose?.({ code, reason })
    }
    emitOpen() {
      this.onopen?.({})
    }
    emitMessage(payload) {
      this.onmessage?.({ data: JSON.stringify(payload) })
    }
    emitClose(code = 1006, reason = '') {
      this.onclose?.({ code, reason })
    }
  }

  const fakeWindow = {
    setTimeout(fn, delay) {
      const id = nextTimerId++
      timers.set(id, { fn, delay, kind: 'timeout' })
      return id
    },
    clearTimeout(id) {
      timers.delete(id)
    },
    setInterval(fn, delay) {
      const id = nextTimerId++
      timers.set(id, { fn, delay, kind: 'interval' })
      return id
    },
    clearInterval(id) {
      timers.delete(id)
    },
  }

  const fakeDocument = {
    visibilityState: 'visible',
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, new Set())
      docListeners.get(type).add(fn)
    },
  }

  const fakeLocation = { protocol: 'https:', host: 'parrot.example' }
  const getCalls = []
  const api = {
    async get(path, query) {
      getCalls.push({ path, query })
      if (!onGet) return { data: [], meta: { total: 0 } }
      return await onGet(path, query)
    },
  }

  function loadModule(code, req) {
    const mod = { exports: {} }
    new Function('require', 'exports', 'module', 'window', 'document', 'location', 'WebSocket', 'Date', code)(
      req,
      mod.exports,
      mod,
      fakeWindow,
      fakeDocument,
      fakeLocation,
      FakeWebSocket,
      FakeDate,
    )
    return mod.exports
  }

  const eventsMod = loadModule(eventsJs, (id) => {
    if (id === 'vue') return Vue
    if (id === './client') return { API_BASE: '/api/management/v1' }
    return {}
  })

  const liveMod = loadModule(liveJs, (id) => {
    if (id === 'vue') return Vue
    if (id === '@/api/client') return { api }
    if (id === '@/api/events') return eventsMod
    return {}
  })

  return {
    events: eventsMod,
    liveStore: liveMod,
    sockets,
    getCalls,
    advanceNow(ms) {
      now += ms
    },
    runPendingRetryTimers() {
      for (const [id, entry] of Array.from(timers.entries())) {
        if (entry.kind === 'timeout' && entry.delay < 60_000) {
          timers.delete(id)
          entry.fn()
        }
      }
    },
  }
}

function makeLog(overrides) {
  return {
    id: 'req-1',
    status: 'success',
    createdAt: '2026-11-21T04:08:21Z',
    apiKeyName: 'gemini',
    requestedModel: 'gemini-4-neon-xhigh',
    finalModel: 'gemini-4-neon-xhigh',
    channelId: 'oauth:antigravity:test',
    protocol: 'chat',
    isStream: true,
    retryCount: 0,
    durationMilliseconds: 20410,
    inputTokens: 2518,
    outputTokens: 1546,
    costTicks: 100,
    error: null,
    ...overrides,
  }
}

function makeSnapshot(channelInFlight, apiKeyInFlight = channelInFlight) {
  return {
    channelTotals: { inFlight: channelInFlight, waiting: 0, trackedChannels: 1 },
    channels: [],
    apiKeyTotals: { inFlight: apiKeyInFlight, waiting: 0, trackedKeys: 1 },
    apiKeys: [],
    revision: 'rev_1',
  }
}

const settled = () => new Promise((resolve) => setImmediate(resolve))

test('seedFeed replaces stale pending entries with authoritative terminal logs', () => {
  const h = createHarness()
  h.events.connectEvents()
  const ws = h.sockets[0]
  ws.emitOpen()
  ws.emitMessage({
    type: 'request.started',
    data: {
      id: 'ghost-1',
      createdAt: '2026-11-21T04:08:21Z',
      apiKeyName: 'gemini',
      requestedModel: 'gemini-4-neon-xhigh',
      protocol: 'chat',
      isStream: true,
      clientIp: null,
      reasoningEffort: null,
      fastMode: null,
    },
  })

  assert.equal(h.liveStore.live.feed.length, 1)
  assert.equal(h.liveStore.live.feed[0].status, 'pending')
  assert.equal(h.liveStore.live.inFlight.size, 1)

  h.liveStore.seedFeed([makeLog({ id: 'ghost-1', status: 'success', durationMilliseconds: 20410 })])

  assert.equal(h.liveStore.live.feed.length, 1)
  assert.equal(h.liveStore.live.feed[0].status, 'success')
  assert.equal(h.liveStore.live.feed[0].durationMilliseconds, 20410)
  assert.equal(h.liveStore.live.inFlight.size, 0)
})

test('websocket reconnect dispatches resync and reconciles missed request.finished events', async () => {
  const logs = [
    makeLog({ id: 'ghost-2', createdAt: '2026-11-21T04:08:22Z', status: 'error', error: 'timeout' }),
    makeLog({ id: 'ghost-1', createdAt: '2026-11-21T04:08:21Z', status: 'success' }),
  ]
  const h = createHarness({
    async onGet(path) {
      assert.equal(path, '/logs')
      return { data: logs, meta: { total: logs.length } }
    },
  })

  h.events.connectEvents()
  const ws1 = h.sockets[0]
  ws1.emitOpen()
  for (const [id, createdAt] of [['ghost-1', '2026-11-21T04:08:21Z'], ['ghost-2', '2026-11-21T04:08:22Z']]) {
    ws1.emitMessage({
      type: 'request.started',
      data: {
        id,
        createdAt,
        apiKeyName: 'gemini',
        requestedModel: 'gemini-4-neon-xhigh',
        protocol: 'chat',
        isStream: true,
        clientIp: null,
        reasoningEffort: null,
        fastMode: null,
      },
    })
  }
  assert.equal(h.liveStore.live.inFlight.size, 2)
  assert.equal(h.liveStore.live.resyncs, 0)

  // Drop the socket before request.finished arrives, then reconnect.
  ws1.emitClose(1006, 'network blip')
  h.runPendingRetryTimers()
  assert.equal(h.sockets.length, 2)
  h.sockets[1].emitOpen()
  await settled()

  assert.equal(h.liveStore.live.resyncs, 1)
  assert.equal(h.getCalls.length, 1)
  assert.equal(h.liveStore.live.inFlight.size, 0)
  assert.deepEqual(
    h.liveStore.live.feed.map((r) => [r.id, r.status]),
    [['ghost-2', 'error'], ['ghost-1', 'success']],
  )
})

test('reconcilePending preserves newly started requests arriving during syncFeed and drops vanished ghosts', async () => {
  let resolveFetch
  const h = createHarness({
    onGet() {
      return new Promise((resolve) => {
        resolveFetch = resolve
      })
    },
  })

  h.events.connectEvents()
  const ws = h.sockets[0]
  ws.emitOpen()
  ws.emitMessage({
    type: 'request.started',
    data: {
      id: 'old-ghost',
      createdAt: '2026-11-21T04:00:00Z',
      apiKeyName: 'gemini',
      requestedModel: 'gemini-4-neon-xhigh',
      protocol: 'chat',
      isStream: true,
      clientIp: null,
      reasoningEffort: null,
      fastMode: null,
    },
  })

  h.advanceNow(500)
  const syncPromise = h.liveStore.syncFeed({ reconcilePending: true })

  // A genuinely new request starts while GET /logs is still in flight.
  h.advanceNow(10)
  ws.emitMessage({
    type: 'request.started',
    data: {
      id: 'fresh-inflight',
      createdAt: '2026-11-21T04:09:00Z',
      apiKeyName: 'gemini',
      requestedModel: 'gemini-4-neon-xhigh',
      protocol: 'chat',
      isStream: true,
      clientIp: null,
      reasoningEffort: null,
      fastMode: null,
    },
  })

  resolveFetch({ data: [makeLog({ id: 'other-finished' })], meta: { total: 1 } })
  await syncPromise

  assert.deepEqual(
    h.liveStore.live.feed.map((r) => [r.id, r.status]),
    [['fresh-inflight', 'pending'], ['other-finished', 'success']],
  )
  assert.equal(h.liveStore.live.inFlight.has('fresh-inflight'), true)
  assert.equal(h.liveStore.live.inFlight.has('old-ghost'), false)
})

test('reconcilePending queries status=pending when 200 newer logs push an active long request off the first page', async () => {
  const recent200 = Array.from({ length: 200 }, (_, i) =>
    makeLog({ id: `recent-${i}`, createdAt: `2026-11-21T05:${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}Z` }),
  )
  const h = createHarness({
    async onGet(path, query) {
      assert.equal(path, '/logs')
      if (query?.status === 'pending') {
        return {
          data: [makeLog({ id: 'long-running', status: 'pending', createdAt: '2026-11-21T04:00:00Z', durationMilliseconds: null })],
          meta: { total: 1 },
        }
      }
      return { data: recent200, meta: { total: 500 } }
    },
  })

  h.events.connectEvents()
  const ws = h.sockets[0]
  ws.emitOpen()
  for (const id of ['long-running', 'vanished-ghost']) {
    ws.emitMessage({
      type: 'request.started',
      data: {
        id,
        createdAt: '2026-11-21T04:00:00Z',
        apiKeyName: 'gemini',
        requestedModel: 'gemini-4-neon-xhigh',
        protocol: 'chat',
        isStream: true,
        clientIp: null,
        reasoningEffort: null,
        fastMode: null,
      },
    })
  }

  h.advanceNow(100)
  await h.liveStore.syncFeed({ reconcilePending: true })

  assert.equal(h.getCalls.length, 2)
  assert.equal(h.getCalls[1].query.status, 'pending')
  assert.equal(h.liveStore.live.inFlight.has('long-running'), true)
  assert.equal(h.liveStore.live.inFlight.has('vanished-ghost'), false)
  assert.equal(h.liveStore.live.feed.some((r) => r.id === 'long-running' && r.status === 'pending'), true)
  assert.equal(h.liveStore.live.feed.some((r) => r.id === 'vanished-ghost'), false)
})

test('pending rows unconfirmed for a minute are reconciled even while the server stays busy', async () => {
  const h = createHarness({
    async onGet() {
      return { data: [makeLog({ id: 'orphan-1', status: 'success' })], meta: { total: 1 } }
    },
  })

  h.events.connectEvents()
  const ws = h.sockets[0]
  ws.emitOpen()
  ws.emitMessage({
    type: 'request.started',
    data: {
      id: 'orphan-1',
      createdAt: '2026-11-21T04:08:21Z',
      apiKeyName: 'gemini',
      requestedModel: 'gemini-4-neon-xhigh',
      protocol: 'chat',
      isStream: true,
      clientIp: null,
      reasoningEffort: null,
      fastMode: null,
    },
  })

  // Other requests keep the server busy the whole time.
  ws.emitMessage({ type: 'runtime.snapshot', data: makeSnapshot(3) })
  h.advanceNow(30_000)
  ws.emitMessage({ type: 'runtime.snapshot', data: makeSnapshot(3) })
  await settled()
  assert.equal(h.getCalls.length, 0)

  h.advanceNow(31_000)
  ws.emitMessage({ type: 'runtime.snapshot', data: makeSnapshot(3) })
  await settled()

  assert.equal(h.getCalls.length, 1)
  assert.equal(h.liveStore.live.inFlight.size, 0)
  assert.equal(h.liveStore.live.feed[0].status, 'success')
})

test('rows the server still reports as pending are re-checked at most once a minute', async () => {
  const h = createHarness({
    async onGet() {
      return {
        data: [makeLog({ id: 'queued-1', status: 'pending', durationMilliseconds: null })],
        meta: { total: 1 },
      }
    },
  })

  h.events.connectEvents()
  const ws = h.sockets[0]
  ws.emitOpen()
  ws.emitMessage({
    type: 'request.started',
    data: {
      id: 'queued-1',
      createdAt: '2026-11-21T04:08:21Z',
      apiKeyName: 'gemini',
      requestedModel: 'gemini-4-neon-xhigh',
      protocol: 'chat',
      isStream: true,
      clientIp: null,
      reasoningEffort: null,
      fastMode: null,
    },
  })

  h.advanceNow(61_000)
  ws.emitMessage({ type: 'runtime.snapshot', data: makeSnapshot(0) })
  await settled()
  assert.equal(h.getCalls.length, 1)
  assert.equal(h.liveStore.live.inFlight.has('queued-1'), true)
  assert.equal(h.liveStore.live.feed[0].status, 'pending')

  // Snapshots keep arriving every 2s; the confirmed row is not fetched again within the minute.
  for (let i = 0; i < 29; i++) {
    h.advanceNow(2_000)
    ws.emitMessage({ type: 'runtime.snapshot', data: makeSnapshot(0) })
    await settled()
  }
  assert.equal(h.getCalls.length, 1)

  h.advanceNow(2_000)
  ws.emitMessage({ type: 'runtime.snapshot', data: makeSnapshot(0) })
  await settled()
  assert.equal(h.getCalls.length, 2)
})

test('concurrent syncFeed calls share one run and resolve with the latest total', async () => {
  const fetches = []
  const h = createHarness({
    onGet() {
      return new Promise((resolve) => fetches.push(resolve))
    },
  })

  const first = h.liveStore.syncFeed({ reconcilePending: true })
  const second = h.liveStore.syncFeed({ reconcilePending: true })
  assert.equal(first, second)
  assert.equal(fetches.length, 1)

  // The second call may carry newer state, so the run fetches once more before settling.
  fetches[0]({ data: [], meta: { total: 5 } })
  await settled()
  assert.equal(fetches.length, 2)
  fetches[1]({ data: [makeLog({ id: 'late-1' })], meta: { total: 7 } })

  assert.equal(await first, 7)
  assert.equal(await second, 7)
  assert.equal(h.getCalls.length, 2)
  assert.equal(h.liveStore.live.feed[0].id, 'late-1')
})

test('paused live stream ignores resync fetches until resumed', async () => {
  const h = createHarness({
    async onGet() {
      return { data: [makeLog({ id: 'ghost-paused', status: 'success' })], meta: { total: 1 } }
    },
  })

  h.events.connectEvents()
  const ws = h.sockets[0]
  ws.emitOpen()
  ws.emitMessage({
    type: 'request.started',
    data: {
      id: 'ghost-paused',
      createdAt: '2026-11-21T04:08:21Z',
      apiKeyName: 'gemini',
      requestedModel: 'gemini-4-neon-xhigh',
      protocol: 'chat',
      isStream: true,
      clientIp: null,
      reasoningEffort: null,
      fastMode: null,
    },
  })

  h.liveStore.live.paused = true
  ws.emitMessage({ type: 'resync', data: { reason: 'overflow' } })
  await settled()
  assert.equal(h.getCalls.length, 0)
  assert.equal(h.liveStore.live.feed[0].status, 'pending')

  h.liveStore.live.paused = false
  await h.liveStore.syncFeed({ reconcilePending: true })
  assert.equal(h.getCalls.length, 1)
  assert.equal(h.liveStore.live.feed[0].status, 'success')
  assert.equal(h.liveStore.live.inFlight.size, 0)
})

test('disconnectEvents resets connectCount so next login does not emit spurious reconnect resync', async () => {
  const h = createHarness()
  h.events.connectEvents()
  h.sockets[0].emitOpen()
  assert.equal(h.liveStore.live.resyncs, 0)

  h.events.disconnectEvents()
  h.events.connectEvents()
  h.sockets[1].emitOpen()
  await settled()
  assert.equal(h.liveStore.live.resyncs, 0)
})
