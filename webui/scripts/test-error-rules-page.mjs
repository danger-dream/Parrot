import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import * as Vue from 'vue'
import { parse, compileScript } from 'vue/compiler-sfc'
import ts from 'typescript'
import { ApiError } from '../src/api/client.ts'

function transpile(code) {
  return ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
}

const pageUrl = new URL('../src/pages/errorRules/ErrorRulesPage.vue', import.meta.url)
const { descriptor } = parse(readFileSync(pageUrl, 'utf8'), { filename: pageUrl.pathname })
const pageJs = transpile(compileScript(descriptor, { id: 'test-error-rules-page' }).content)
const modelJs = transpile(readFileSync(new URL('../src/pages/errorRules/model.ts', import.meta.url), 'utf8'))

const icons = new Proxy({}, { get: () => ({}) })

function load(code, require) {
  const mod = { exports: {} }
  new Function('require', 'exports', 'module', code)(require, mod.exports, mod)
  return mod.exports
}

const rule = (id, name, extra = {}) => ({
  id, name, enabled: true, note: '', category: 'rate_limit', final: 'next', thresholds: [],
  when: { kind: 'group', mode: 'all', items: [{ kind: 'cond', field: 'status', op: 'in', path: '', values: ['429'] }] },
  steps: [{ type: 'pause', scope: 'model', mode: 'fixed', seconds: 60, pattern: '', timezone: '+08:00', boundaryHours: 0, fallbackSeconds: 60 }],
  response: { status: 0, message: '', retryAfter: false },
  ...extra,
})

const baseData = (extra = {}) => ({
  enabled: true, rules: [], invalidRules: [], presets: [], builtins: [], categories: [], providers: [],
  channelProviders: {}, stats: {}, statsWindowDays: 7, revision: 'r1', ...extra,
})

function mountPage(remote, handlers = {}) {
  const calls = { get: 0, post: [], del: [] }
  const toasts = []
  const api = {
    async get(path) {
      assert.equal(path, '/error-rules')
      calls.get += 1
      return { data: structuredClone(remote.data) }
    },
    async post(path, payload, opts) {
      calls.post.push([path, payload, opts])
      return { data: await handlers.post(payload, opts) }
    },
    async del(path, opts) {
      calls.del.push([path, opts])
      return { data: await handlers.del(path, opts) }
    },
  }
  const model = load(modelJs, (id) => {
    if (id === '@lucide/vue') return icons
    if (id === '@/utils/providers') return { providerTile: () => ({ label: '' }) }
    return {}
  })
  const component = load(pageJs, (id) => {
    if (id === 'vue') return Vue
    if (id === 'vue-router') return { useRoute: () => ({ query: {} }), useRouter: () => ({ replace() {} }), RouterLink: {} }
    if (id === '@lucide/vue') return icons
    if (id === '@/api/client') return { api, ApiError, enc: encodeURIComponent }
    if (id === '@/stores/catalog') return { catalog: { channels: {} }, channelName: (k) => k, refreshCatalog: async () => {} }
    if (id === '@/stores/dialogs') return { confirmAction: async () => true }
    if (id === '@/stores/ui') return { toast: (...args) => toasts.push(args) }
    if (id === '@/utils/errors') return { errorTitle: (err) => err.message, errorDetail: () => '' }
    if (id === '@/utils/providers') return { channelProvider: () => '', providerTile: () => ({ label: '' }) }
    if (id === './ruleNames') return { storeRuleNames() {}, takeHandOff: () => null }
    if (id === './model') return model
    return {}
  }).default
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
  return { state: () => instance.setupState, calls, toasts, unmount: () => app.unmount() }
}

const settled = () => new Promise((resolve) => setImmediate(resolve))
const fileEvent = (rules) => ({ target: { value: 'x', files: [{ text: async () => JSON.stringify({ rules }) }] } })
const conflict = () => new ApiError(409, { code: 'REVISION_CONFLICT', message: 'REVISION_CONFLICT', retryable: false, fields: [] })

test('a skipped rule is deleted by its key against the shown revision', async () => {
  const key = 'a1b2c3d4e5f60718'
  const remote = { data: baseData({ invalidRules: [{ key, index: 1, id: 'broken', name: '坏规则', error: '请至少设置一个条件' }] }) }
  const page = mountPage(remote, { del: async () => baseData({ revision: 'r2' }) })
  try {
    await settled()
    await page.state().removeInvalid(page.state().data.invalidRules[0])
    assert.deepEqual(page.calls.del, [[`/error-rules/invalid-rules/${key}`, { ifMatch: 'r1' }]])
    assert.deepEqual(page.state().data.invalidRules, [])
    assert.equal(page.toasts.at(-1)[1], 'success')
  } finally { page.unmount() }
})

test('import skips rules already present or repeated in the file', async () => {
  const existing = rule('rule-1', '已有规则')
  const remote = { data: baseData({ rules: [existing] }) }
  let rev = 1
  const page = mountPage(remote, {
    post: async (payload) => {
      remote.data = baseData({ rules: [...remote.data.rules, rule(`rule-${++rev}`, payload.name, payload)], revision: `r${rev}` })
      return structuredClone(remote.data)
    },
  })
  try {
    await settled()
    const { id: _id, ...sameContent } = existing
    const fresh = rule('x', '新规则')
    delete fresh.id
    await page.state().importRules(fileEvent([{ ...sameContent, enabled: false }, fresh, { ...fresh }]))
    assert.deepEqual(page.calls.post.map(([, payload]) => payload.name), ['新规则'])
    const [title, tone, detail] = page.toasts.at(-1)
    assert.equal(tone, 'success')
    assert.match(title, /导入了 1 条/)
    assert.match(detail, /2 条和已有规则相同/)
  } finally { page.unmount() }
})

test('import reloads after a revision conflict and retries that rule once', async () => {
  const remote = { data: baseData() }
  let first = true
  const page = mountPage(remote, {
    post: async (payload, opts) => {
      if (first) {
        first = false
        remote.data = baseData({ revision: 'r9' })
        throw conflict()
      }
      assert.deepEqual(opts, { ifMatch: 'r9' })
      return baseData({ rules: [rule('rule-new', payload.name, payload)], revision: 'r10' })
    },
  })
  try {
    await settled()
    const fresh = rule('x', '新规则')
    delete fresh.id
    await page.state().importRules(fileEvent([fresh]))
    assert.equal(page.calls.post.length, 2)
    assert.equal(page.calls.get, 2)
    assert.deepEqual(page.state().data.rules.map((r) => r.name), ['新规则'])
    assert.equal(page.toasts.at(-1)[1], 'success')
  } finally { page.unmount() }
})
