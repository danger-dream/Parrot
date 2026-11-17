import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import * as Vue from 'vue'
import { renderToString } from 'vue/server-renderer'
import { parse, compileScript, compileTemplate } from 'vue/compiler-sfc'
import ts from 'typescript'

const hint = 'API Key 导入无法查询重置卡，请使用 OAuth 登录。'

function evaluate(source, require) {
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const mod = { exports: {} }
  new Function('require', 'exports', 'module', js)(require, mod.exports, mod)
  return mod.exports
}

function component(path, require) {
  const filename = new URL(path, import.meta.url)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename: filename.pathname })
  const compiled = compileScript(descriptor, { id: 'test-zhipu-reset-hint' })
  const template = compileTemplate({
    source: descriptor.template.content, filename: filename.pathname, id: 'test-zhipu-reset-hint',
    compilerOptions: { bindingMetadata: compiled.bindings },
  })
  assert.deepEqual(template.errors, [])
  const result = evaluate(compiled.content, require).default
  result.render = evaluate(template.code, require).render
  return result
}

async function renderPanel(snapshot, error = null) {
  const calls = []
  const meta = evaluate(readFileSync(new URL('../src/pages/oauth/meta.ts', import.meta.url), 'utf8'), () => ({}))
  const empty = { render: () => null }
  function require(id) {
    // SSR prefetch runs the real read-only mounted load before the actual template.
    if (id === 'vue') return { ...Vue, onMounted: Vue.onServerPrefetch }
    if (id === '@/api/client') return { api: {
      async get(path) {
        calls.push(path)
        if (error) throw error
        return { data: { snapshot, actions: [], reset: null } }
      },
      post() { assert.fail('rendering must not claim or use a card') },
    } }
    if (id === './meta') return meta
    if (id === './oauthApi') return { accountPath: (id, path) => `/oauth/accounts/${id}${path}` }
    if (id === '@/utils/errors') return { errorTitle: (err) => err.message }
    if (id === '@/utils/format') return { dateTime: String, relative: String }
    if (id === '@/components/SubBlock.vue') return { default: component('../src/components/SubBlock.vue', require) }
    if (id === '@/components/ui/Btn.vue') return { default: { setup: (_, { slots }) => () => Vue.h('button', slots.default?.()) } }
    if (id === '@lucide/vue') return Object.fromEntries(['Building2', 'Gift', 'KeyRound', 'RefreshCw', 'RotateCcw'].map((name) => [name, empty]))
    return { default: empty }
  }
  const panel = component('../src/pages/oauth/ZhipuPanel.vue', require)
  const context = {}
  await renderToString(Vue.createSSRApp(panel, { accountId: 'fixture', revision: 'r1', scope: 'account' }), context)
  assert.deepEqual(calls, ['/oauth/accounts/fixture/zhipu'])
  return context.teleports['#account-quota']
}

for (const modeField of ['credentialMode', 'credential_mode']) {
  test(`API Key shows OAuth guidance without fake counts or card actions (${modeField})`, async () => {
    const html = await renderPanel({ [modeField]: 'api_key' })
    assert.match(html, /官方重置卡/)
    assert.ok(html.includes(hint))
    assert.doesNotMatch(html, /\d+ 张|没有可用的卡|<button|领取重置卡|用 5 小时卡|用 7 天卡/)
  })

  test(`OAuth still shows cached cards and existing query/use actions (${modeField})`, async () => {
    const html = await renderPanel({ [modeField]: 'oauth', reset: { data: {
      available_five_hour_resets: [{ expire_at: 2000000000000 }, { expire_at: 2000000000000 }, { expire_at: 2000000000000 }],
      available_week_resets: [{ expire_at: 2000000000000 }],
    } } })
    assert.ok(!html.includes(hint))
    for (const label of ['官方重置卡', '3 张', '1 张', '查询', '领取重置卡', '用 5 小时卡', '用 7 天卡']) {
      assert.ok(html.includes(label), label)
    }
  })
}

test('failed snapshot load remains an error rather than an API Key diagnosis', async () => {
  const html = await renderPanel({}, new Error('fixture load failure'))
  assert.match(html, /智谱状态加载失败：fixture load failure/)
  assert.ok(!html.includes(hint))
})

test('unknown credential mode is not described as API Key', async () => {
  const html = await renderPanel({})
  assert.ok(!html.includes(hint))
  assert.doesNotMatch(html, /\d+ 张|<button/)
})
