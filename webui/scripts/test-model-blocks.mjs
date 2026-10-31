// Dependency-free focused UI checks: Node test runner + the project's Vue/TS compiler.
// No browser/network, config, real key, or running service is accessed.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import * as Vue from 'vue'
import { parse, compileScript, compileTemplate } from 'vue/compiler-sfc'
import ts from 'typescript'
import * as groups from '../src/pages/keys/modelBlockGroups.ts'

const { groupModelBlocks, flattenModelBlocks, modelBlockOptions, modelBlockValidation, updateModelSelection } = groups
const rule = (model, settings = {}) => ({ model, enabled: true, preset: 'quota_exhausted', message: null, ...settings })

test('multi-select flattens to the existing single-model backend contract', () => {
  const grouped = [{ id: 4, models: ['glm-5.3', 'other'], enabled: false, preset: 'service_busy', message: 'Unavailable.' }]
  assert.deepEqual(flattenModelBlocks(grouped), ['glm-5.3', 'other'].map((model) => rule(model, { enabled: false, preset: 'service_busy', message: 'Unavailable.' })))
})
test('only adjacent equal settings merge; unchanged roundtrip preserves priority, enabled and exact message', () => {
  const rules = [rule('z'), rule('a'), rule('alias', { preset: 'service_busy' }), rule('target'),
    rule('off', { enabled: false }), rule('custom', { message: '  Keep exact text.  ' }), rule('empty', { message: '' }),
    { model: 'legacy-defaults' }]
  const before = structuredClone(rules)
  const grouped = groupModelBlocks(rules)
  assert.deepEqual(grouped.map((group) => group.models), [['z', 'a'], ['alias'], ['target'], ['off'], ['custom'], ['empty'], ['legacy-defaults']])
  assert.deepEqual(flattenModelBlocks(grouped), before)
  assert.deepEqual(rules, before)
})
test('selection keeps existing order, appends new catalog choices, refuses arbitrary input', () => {
  const options = ['a', 'b', 'z'].map((value) => ({ value }))
  assert.deepEqual(updateModelSelection(['z', 'a'], ['a', 'b', 'z', 'typed-new-model'], options), ['z', 'a', 'b'])
  assert.deepEqual(updateModelSelection(['z', 'a'], ['a'], options), ['a'])
})
test('missing saved entries remain selectable, including a failed/empty catalog', () => {
  const options = modelBlockOptions([{ value: 'a' }], ['gone', 'a'])
  assert.equal(options[0].value, 'gone')
  assert.match(options[0].hint, /当前不可用/)
  assert.deepEqual(updateModelSelection(['gone'], ['gone'], options), ['gone'])
  assert.deepEqual(modelBlockOptions([], ['gone']).map((option) => option.value), ['gone'])
})
test('requires a selection per group, rejects cross-group duplicates and >100 expanded rules', () => {
  assert.equal(modelBlockValidation([]), '') // Removing all rules is intentional.
  assert.match(modelBlockValidation([{ id: 0, models: [] }]), /至少选择/)
  assert.match(modelBlockValidation([{ id: 0, models: ['a'] }, { id: 1, models: ['a'] }]), /重复/)
  const hundred = [{ id: 0, models: Array.from({ length: 100 }, (_, i) => `model-${i}`) }]
  assert.equal(modelBlockValidation(hundred), '')
  hundred[0].models.push('overflow')
  assert.match(modelBlockValidation(hundred), /100/)
  assert.match(modelBlockValidation([{ id: 0, models: ['a'], message: 'x'.repeat(1001) }]), /高级选项/)
})

const filename = new URL('../src/pages/keys/KeyModelBlocksModal.vue', import.meta.url)
const source = readFileSync(filename, 'utf8')
const { descriptor } = parse(source, { filename: filename.pathname })

test('actual template puts custom message and technical preview inside native, default-closed details', () => {
  const result = compileTemplate({ source: descriptor.template.content, filename: filename.pathname, id: 'test-model-blocks' })
  assert.deepEqual(result.errors, [])
  const nodes = []
  function visit(node, ancestors = []) {
    if (node.type === 1) nodes.push({ node, ancestors })
    for (const child of node.children || []) visit(child, node.type === 1 ? [...ancestors, node] : ancestors)
    for (const branch of node.branches || []) visit(branch, ancestors)
  }
  visit(result.ast)
  const details = nodes.filter(({ node }) => node.tag === 'details')
  assert.equal(details.length, 1)
  assert.ok(!details[0].node.props.some((prop) => prop.name === 'open' || prop.arg?.content === 'open'))
  assert.equal(nodes.filter(({ node }) => node.tag === 'TextInput').length, 0)
  const custom = nodes.find(({ node }) => node.tag === 'TextArea')
  const preview = nodes.find(({ node }) => node.props.some((prop) => prop.name === 'class' && prop.value?.content === 'preview'))
  assert.ok(custom.ancestors.includes(details[0].node))
  assert.ok(preview.ancestors.includes(details[0].node))
  const list = nodes.find(({ node }) => node.tag === 'CheckList')
  assert.ok(list.ancestors.some((node) => node.tag === 'fieldset' && node.props.some((prop) => prop.arg?.content === 'disabled')))
})

// Execute the real setup script with a minimal Vue renderer. Only external IO
// and child presentation are stubbed; watches, computed guards and save are real.
function mountDialog(renderTree = false) {
  const requests = [], patches = []
  const compiled = compileScript(descriptor, { id: 'test-model-blocks' })
  const js = ts.transpileModule(compiled.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const mod = { exports: {} }
  function require(id) {
    if (id === 'vue') return Vue
    if (id === './modelBlockGroups') return groups
    if (id === './meta') return { loadModelOptions: () => new Promise((resolve, reject) => requests.push({ resolve, reject })) }
    if (id === '@/api/client') return { enc: encodeURIComponent, api: { patch: async (...args) => { patches.push(args); return { data: props.k } } } }
    if (id === '@/stores/ui') return { toast() {} }
    if (id === '@/utils/errors') return { errorTitle: (error) => error.message, errorDetail: () => '' }
    if (renderTree) {
      if (id.endsWith('/CheckList.vue')) {
        // Real CheckList setup owns q; render a minimal input without browser DOM
        // directives so its local search state can be exercised in Node.
        const child = parse(readFileSync(new URL('../src/components/ui/CheckList.vue', import.meta.url), 'utf8')).descriptor
        const code = ts.transpileModule(compileScript(child, { id: 'test-checklist' }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
        const childModule = { exports: {} }
        new Function('require', 'exports', 'module', code)(require, childModule.exports, childModule)
        childModule.exports.default.render = (_ctx, _cache, _props, state) => {
          return Vue.h('input', { value: state.q, onInput: (value) => { state.q = value } })
        }
        return childModule.exports
      }
      const stub = { inheritAttrs: false, setup: (_, { slots }) => () => Vue.h('div', [...(slots.default?.() || []), ...(slots.footer?.() || [])]) }
      return { default: stub, Plus: stub, Trash2: stub }
    }
    return {} // Child components/icons are not rendered in these state tests.
  }
  new Function('require', 'exports', 'module', js)(require, mod.exports, mod)
  const component = mod.exports.default
  if (renderTree) {
    const template = compileTemplate({ source: descriptor.template.content, filename: filename.pathname, id: 'test-model-blocks', compilerOptions: { bindingMetadata: compiled.bindings } })
    assert.deepEqual(template.errors, [])
    const renderModule = { exports: {} }
    const code = ts.transpileModule(template.code, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
    new Function('require', 'exports', 'module', code)(require, renderModule.exports, renderModule)
    component.render = renderModule.exports.render
  } else component.render = () => null
  const node = (tag, text = '') => ({ tag, text, props: {}, children: [], parent: null })
  const detach = (child) => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = null
  }
  const renderer = Vue.createRenderer({
    createElement: (tag) => node(tag), createText: (text) => node('#text', text), createComment: (text) => node('#comment', text),
    insert(child, parent, anchor) {
      detach(child)
      const index = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
      child.parent = parent
    },
    remove: detach, setText: (node, text) => { node.text = text }, setElementText: (node, text) => { node.text = text; node.children = [] },
    patchProp: (node, key, _previous, value) => { node.props[key] = value },
    parentNode: (node) => node.parent, nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
  })
  const props = Vue.reactive({ open: true, k: { keyId: 'fixture-only', revision: 'r1', modelBlocks: [rule('gone'), rule('z')] } })
  let instance
  const app = renderer.createApp({ setup: () => () => Vue.h(component, { ...props, ref: (value) => { if (value) instance = value.$ } }) })
  const root = node('root')
  app.mount(root)
  function find(tag, from = root) {
    if (from.tag === tag) return from
    for (const child of from.children) { const found = find(tag, child); if (found) return found }
  }
  return { props, requests, patches, find, state: () => instance.setupState, unmount: () => app.unmount() }
}

test('dialog guards save while loading, preserves unavailable saved rules on failure, retries successfully', async () => {
  const dialog = mountDialog()
  try {
    assert.equal(dialog.state().cannotSave, true)
    await dialog.state().save()
    assert.equal(dialog.patches.length, 0)
    dialog.requests[0].reject(new Error('fixture load failed'))
    await Vue.nextTick()
    assert.match(dialog.state().loadError, /fixture load failed/)
    assert.deepEqual(dialog.state().options(dialog.state().groups[0]).map((option) => option.value), ['gone', 'z'])
    assert.equal(dialog.state().cannotSave, false)
    const retry = dialog.state().loadModels()
    assert.equal(dialog.state().cannotSave, true)
    dialog.requests[1].resolve([{ value: 'available' }])
    await retry
    assert.equal(dialog.state().loadError, '')
    assert.equal(dialog.state().cannotSave, false)
    assert.deepEqual(dialog.state().options(dialog.state().groups[0]).map((option) => option.value), ['gone', 'z', 'available'])
  } finally { dialog.unmount() }
})

test('closing/reopening resets groups and rejects stale catalog responses; save preserves revision and order', async () => {
  const dialog = mountDialog()
  try {
    const originalId = dialog.state().groups[0].id
    dialog.state().groups[0].message = 'discard on close'
    dialog.props.open = false
    await Vue.nextTick()
    dialog.props.open = true
    await Vue.nextTick()
    assert.notEqual(dialog.state().groups[0].id, originalId) // remounts native details and CheckList search
    assert.equal(dialog.state().groups[0].message, null)
    dialog.requests[0].resolve([{ value: 'stale' }])
    await Vue.nextTick()
    assert.deepEqual(dialog.state().models, [])
    assert.equal(dialog.state().loading, true)
    dialog.requests[1].resolve([{ value: 'z' }, { value: 'a' }])
    await Vue.nextTick()
    const group = dialog.state().groups[0]
    dialog.state().select(group, ['a', 'z', 'gone'])
    await dialog.state().save()
    assert.deepEqual(dialog.patches[0], ['/api-keys/fixture-only', { modelBlocks: [rule('gone'), rule('z'), rule('a')] }, { ifMatch: 'r1' }])
  } finally { dialog.unmount() }
})

test('same-key parent snapshot replacements do not reload or overwrite the edit session', async () => {
  const dialog = mountDialog()
  try {
    dialog.requests[0].resolve([{ value: 'z' }, { value: 'a' }])
    await Vue.nextTick()
    const state = dialog.state()
    const originalGroups = state.groups
    const group = state.groups[0]
    state.select(group, ['a', 'z'])
    group.enabled = false
    group.preset = 'service_busy'
    group.message = 'Unsaved custom message'
    const edited = structuredClone(Vue.toRaw(group))
    const catalog = state.models
    for (let tick = 0; tick < 3; tick++) {
      // Mirrors ApiKeyDetail's runtime.snapshot object replacement. Also cover
      // a refreshed detail response with new revision/rules for the same key.
      dialog.props.k = { ...dialog.props.k, limiter: { inFlight: tick }, revision: 'r2', modelBlocks: [rule('server-change')] }
      await Vue.nextTick()
      assert.equal(dialog.requests.length, 1)
      assert.equal(state.loading, false)
      assert.equal(state.cannotSave, false)
      assert.equal(state.groups, originalGroups)
      assert.equal(state.groups[0], group)
      assert.deepEqual(group, edited)
      assert.equal(state.models, catalog)
      assert.equal(state.revision, 'r1')
    }
    await state.save()
    assert.equal(dialog.patches[0][2].ifMatch, 'r1')
    assert.deepEqual(dialog.patches[0][1].modelBlocks, flattenModelBlocks([edited]))
  } finally { dialog.unmount() }
})

test('switching key while open initializes once and ignores the old pending catalog request', async () => {
  const dialog = mountDialog()
  try {
    const firstId = dialog.state().groups[0].id
    dialog.props.k = { keyId: 'different-key', revision: 'other-r1', modelBlocks: [rule('other')] }
    await Vue.nextTick()
    assert.equal(dialog.requests.length, 2)
    assert.notEqual(dialog.state().groups[0].id, firstId)
    assert.deepEqual(flattenModelBlocks(dialog.state().groups), [rule('other')])
    assert.equal(dialog.state().revision, 'other-r1')
    dialog.requests[0].resolve([{ value: 'stale' }])
    await Vue.nextTick()
    assert.deepEqual(dialog.state().models, [])
    assert.equal(dialog.state().loading, true)
    dialog.requests[1].resolve([{ value: 'other' }])
    await Vue.nextTick()
    assert.deepEqual(dialog.state().models, [{ value: 'other' }])
    assert.equal(dialog.state().loading, false)
  } finally { dialog.unmount() }
})

test('same-key refresh retains rendered search and expanded advanced node; reopen resets both', async () => {
  const dialog = mountDialog(true)
  try {
    dialog.requests[0].resolve([{ value: 'z' }])
    await Vue.nextTick()
    const input = dialog.find('input')
    const advanced = dialog.find('details')
    assert.ok(input && advanced)
    assert.equal(advanced.props.open, undefined)
    input.props.onInput('unsaved search')
    advanced.open = true // Native disclosure state is not bound/managed by Vue.
    await Vue.nextTick()
    assert.equal(input.props.value, 'unsaved search')
    dialog.props.k = { ...dialog.props.k, limiter: { inFlight: 3 } }
    await Vue.nextTick()
    assert.equal(dialog.requests.length, 1)
    assert.equal(dialog.find('input'), input)
    assert.equal(input.props.value, 'unsaved search')
    assert.equal(dialog.find('details'), advanced)
    assert.equal(advanced.open, true)
    dialog.props.open = false
    await Vue.nextTick()
    dialog.props.open = true
    await Vue.nextTick()
    assert.equal(dialog.requests.length, 2)
    assert.notEqual(dialog.find('input'), input)
    assert.equal(dialog.find('input').props.value, '')
    assert.notEqual(dialog.find('details'), advanced)
    assert.equal(dialog.find('details').open, undefined)
    assert.equal(dialog.find('details').props.open, undefined)
  } finally { dialog.unmount() }
})
