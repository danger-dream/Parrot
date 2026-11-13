import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import * as Vue from 'vue'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import { ApiError } from '../src/api/client.ts'

const filename = new URL('../src/pages/models/RegistryPanel.vue', import.meta.url)
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename: filename.pathname })
const compiled = compileScript(descriptor, { id: 'test-registry-toggle' })
const js = ts.transpileModule(compiled.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText

function mountPanel({ error = null, committed = false, refreshError = false, modelRefresh = async () => {} } = {}) {
  let remote = { enabled: false, revision: 'r1', rules: [], dismissed: [], pending: [], suggestions: [] }
  const puts = [], toasts = [], refreshes = []
  let reads = 0
  const mod = { exports: {} }
  const api = {
    async get(path) {
      assert.equal(path, '/models/registry')
      reads++
      if (reads > 1 && refreshError) throw new Error('refresh failed')
      return { data: structuredClone(remote) }
    },
    async put(...args) {
      puts.push(args)
      if (!error || committed) remote = { ...remote, enabled: args[1].enabled, revision: 'r2' }
      if (error) throw error
      return { data: { revision: remote.revision } }
    },
  }
  function require(id) {
    if (id === 'vue') return Vue
    if (id === '@/api/client') return { api, ApiError }
    if (id === '@/stores/ui') return { toast: (...args) => toasts.push(args) }
    if (id === '@/stores/catalog') return { refreshCatalog() {} }
    if (id === '@/utils/errors') return { errorTitle: (err) => err.message, errorDetail: () => '' }
    if (id === './shared') return {
      loadModels(force) { refreshes.push(force); return modelRefresh() },
      channelLabel: (id) => id,
    }
    return {}
  }
  new Function('require', 'exports', 'module', js)(require, mod.exports, mod)
  const component = mod.exports.default
  component.render = () => null
  const renderer = Vue.createRenderer({
    createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
    parentNode: () => null, nextSibling: () => null,
  })
  let instance
  const app = renderer.createApp({
    setup: () => () => Vue.h(component, { ref: (value) => { if (value) instance = value.$ } }),
  })
  app.mount({})
  return { state: () => instance.setupState, puts, toasts, refreshes, reads: () => reads, unmount: () => app.unmount() }
}

const settled = () => new Promise((resolve) => setImmediate(resolve))
const savedError = () => new ApiError(503, {
  code: 'DEPENDENCY_UNAVAILABLE', message: 'saved, reload unconfirmed', retryable: false,
  fields: [{ path: 'runtime', code: 'SAVED_RELOAD_UNCONFIRMED', message: 'refresh state' }],
})

test('successful toggle refreshes registry and model list with the displayed revision', async () => {
  const panel = mountPanel()
  try {
    await settled()
    await panel.state().toggleEnabled(true)
    assert.deepEqual(panel.puts, [['/models/registry/enabled', { enabled: true }, { ifMatch: 'r1' }]])
    assert.equal(panel.state().data.enabled, true)
    assert.equal(panel.state().data.revision, 'r2')
    assert.equal(panel.reads(), 2)
    assert.deepEqual(panel.refreshes, [true])
    assert.equal(panel.state().busy, '')
    assert.equal(panel.toasts[0][1], 'success')
  } finally { panel.unmount() }
})

test('saved reload error refreshes actual state, waits for models and never replays the write', async () => {
  let finish
  const panel = mountPanel({ error: savedError(), committed: true, modelRefresh: () => new Promise((resolve) => { finish = resolve }) })
  try {
    await settled()
    const pending = panel.state().toggleEnabled(true)
    await settled()
    assert.equal(panel.state().data.enabled, true)
    assert.equal(panel.state().data.revision, 'r2')
    assert.equal(panel.state().busy, 'enabled')
    await panel.state().toggleEnabled(false)
    assert.equal(panel.puts.length, 1)
    finish()
    await pending
    assert.equal(panel.state().busy, '')
    assert.deepEqual(panel.refreshes, [true])
    assert.equal(panel.toasts[0][1], 'error')
    assert.equal(panel.reads(), 2)
  } finally { panel.unmount() }
})

test('revision conflict refreshes both registry and model list without automatic retry', async () => {
  const panel = mountPanel({ error: new ApiError(409, { code: 'REVISION_CONFLICT' }), committed: true })
  try {
    await settled()
    await panel.state().toggleEnabled(true)
    assert.equal(panel.state().data.revision, 'r2')
    assert.equal(panel.puts.length, 1)
    assert.deepEqual(panel.refreshes, [true])
    assert.equal(panel.toasts[0][1], 'warning')
  } finally { panel.unmount() }
})

test('rejected write does not claim a state change or reload models', async () => {
  const panel = mountPanel({ error: new ApiError(403, { code: 'CAPABILITY_DENIED' }) })
  try {
    await settled()
    await panel.state().toggleEnabled(true)
    assert.equal(panel.state().data.enabled, false)
    assert.equal(panel.state().busy, '')
    assert.equal(panel.reads(), 1)
    assert.deepEqual(panel.refreshes, [])
    assert.equal(panel.toasts[0][1], 'error')
  } finally { panel.unmount() }
})

test('failed state refresh after a saved error is shown instead of a false success', async () => {
  const panel = mountPanel({ error: savedError(), committed: true, refreshError: true })
  try {
    await settled()
    await panel.state().toggleEnabled(true)
    assert.equal(panel.state().error, 'refresh failed')
    assert.equal(panel.state().busy, '')
    assert.equal(panel.puts.length, 1)
    assert.deepEqual(panel.refreshes, [true])
    assert.equal(panel.toasts.some((toast) => toast[1] === 'success'), false)
  } finally { panel.unmount() }
})
