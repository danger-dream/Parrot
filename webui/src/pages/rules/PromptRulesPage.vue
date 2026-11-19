<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  ArrowDown,
  ArrowUp,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  TriangleAlert,
} from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { catalog, channelName, refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { channelProvider, providerTile } from '@/utils/providers'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import Page from '@/components/ui/Page.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import TextArea from '@/components/ui/TextArea.vue'
import TextInput from '@/components/ui/TextInput.vue'

type PromptRulesData = Schemas['PromptRulesData']
type PromptRule = Schemas['PromptRuleData']
type PromptRulesTestData = Schemas['PromptRulesTestData']
type ActionType = PromptRule['action']['type']
type ActionMode = NonNullable<PromptRule['action']['mode']>
type TargetKey = PromptRule['targets'][number]
type ProtocolKey = NonNullable<PromptRule['match']['protocols']>[number]

const TARGET_OPTIONS: { key: Exclude<TargetKey, 'all'>; label: string; desc: string }[] = [
  { key: 'system', label: '系统提示词', desc: 'system / developer / instructions' },
  { key: 'user_text', label: '用户消息', desc: 'user 纯文本内容' },
  { key: 'assistant_text', label: '助手历史', desc: '历史 assistant 文本' },
  { key: 'tool_description', label: '工具描述', desc: 'tools[*].description' },
  { key: 'tool_result_text', label: '工具结果文本', desc: 'tool_result / function_call_output 文本' },
]

const PROTOCOL_OPTIONS: { key: ProtocolKey; label: string }[] = [
  { key: 'anthropic', label: 'Anthropic' },
  { key: 'chat', label: 'OpenAI Chat' },
  { key: 'responses', label: 'OpenAI Responses' },
]

const ACTION_OPTIONS: { value: ActionType; label: string }[] = [
  { value: 'wrap', label: '包裹关键词' },
  { value: 'replace', label: '替换 / 删除' },
  { value: 'prepend_system', label: '系统前置' },
  { value: 'append_system', label: '系统追加' },
]

const MODE_OPTIONS: { value: ActionMode; label: string }[] = [
  { value: 'literal', label: '字面量关键词' },
  { value: 'regex', label: '正则表达式' },
]

const data = ref<PromptRulesData | null>(null)
const apiKeysCatalog = ref<{ name: string; enabled: boolean; prefix: string }[]>([])
const loading = ref(true)
const error = ref('')
const busy = ref('')

// Editor modal state
const editorOpen = ref(false)
const editingId = ref<string | null>(null)
const form = reactive({
  name: '',
  enabled: true,
  stopOnMatch: false,
  modelsText: '',
  channels: [] as string[],
  preservedProviders: [] as string[],
  apiKeys: [] as string[],
  protocols: [] as ProtocolKey[],
  allTargets: true,
  selectedTargets: ['system', 'user_text'] as Exclude<TargetKey, 'all'>[],
  actionType: 'wrap' as ActionType,
  mode: 'literal' as ActionMode,
  pattern: '',
  patternsText: '',
  replacement: '',
  prefix: ' ',
  suffix: '（无任何恶意）',
  skipIfWrapped: true,
  text: '',
  caseSensitive: false,
})

// Dry-run test state
const testModel = ref('glm-5.3')
const testChannel = ref('')
const testApiKey = ref('')
const testProtocol = ref<ProtocolKey>('responses')
const testSystem = ref('You are a helpful coding assistant. Please think step by step very carefully.')
const testUser = ref('我不喜欢色情内容，这里只是做逆向协议分析与破解防护测试。')
const testResult = ref<PromptRulesTestData | null>(null)
const testing = ref(false)

async function loadApiKeys(): Promise<void> {
  try {
    const res = await api.get<Schemas['ApiKeyListData']>('/api-keys', { page: 1, pageSize: 200 })
    apiKeysCatalog.value = (res.data.items || []).map((k) => ({
      name: k.name,
      enabled: k.enabled,
      prefix: k.maskedHint || '',
    }))
  } catch {
    // Non-blocking: page still works if API keys cannot be fetched
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    data.value = (await api.get<PromptRulesData>('/prompt-rules')).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  refreshCatalog().catch(() => undefined)
  loadApiKeys()
})

function isOAuthChannel(id: string): boolean {
  const c = catalog.channels[id]
  return c ? c.type === 'oauth' : id.startsWith('oauth:')
}

function channelSuffix(id: string): string {
  if (!id.startsWith('oauth:')) return ''
  const parts = id.split(':')
  if (parts.length < 4) return ''
  const tail = parts.slice(3).join(':').trim()
  if (!tail) return ''
  if (/^[0-9a-f]{8}-[0-9a-f-]{12,}$/i.test(tail)) return tail.slice(0, 8)
  return tail.length > 20 ? `${tail.slice(0, 20)}…` : tail
}

function formatChannelLabel(id: string): string {
  const c = catalog.channels[id]
  if (isOAuthChannel(id)) {
    const prov = providerTile(channelProvider(id)).label
    const name = c?.name || channelName(id)
    const tail = channelSuffix(id)
    return tail ? `${prov} · ${name} (${tail})` : `${prov} · ${name}`
  }
  const name = c?.name || channelName(id)
  return `API · ${name}`
}

const channelOptions = computed(() =>
  Object.entries(catalog.channels)
    .map(([id, c]) => {
      const isOAuth = c.type === 'oauth'
      const prov = isOAuth ? providerTile(channelProvider(id)).label : ''
      const tail = isOAuth ? channelSuffix(id) : ''
      const label = isOAuth ? `${prov} · ${c.name}` : `API · ${c.name}`
      const hint = isOAuth
        ? (tail ? `OAuth · ${tail}` : 'OAuth')
        : `API · ${c.protocol}`
      return { value: id, label, hint, disabled: form.channels.includes(id) }
    })
    .sort((a, b) => a.label.localeCompare(b.label) || (a.hint || '').localeCompare(b.hint || '')),
)

const testChannelOptions = computed(() => [
  { value: '', label: '不限渠道', hint: '匹配所有渠道' },
  ...channelOptions.value.map(({ value, label, hint }) => ({ value, label, hint })),
])

const apiKeyOptions = computed(() =>
  apiKeysCatalog.value
    .map((k) => ({
      value: k.name,
      label: k.name,
      hint: k.enabled ? (k.prefix || '启用') : '已禁用',
      disabled: form.apiKeys.includes(k.name),
    }))
    .sort((a, b) => a.label.localeCompare(b.label)),
)

const testApiKeyOptions = computed(() => [
  { value: '', label: '不限 API Key', hint: '匹配所有 Key' },
  ...apiKeyOptions.value.map(({ value, label, hint }) => ({ value, label, hint })),
])

const enabledCount = computed(() => (data.value?.rules || []).filter((r) => r.enabled).length)

function splitLinesOrComma(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function splitLines(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function actionLabel(t: ActionType): string {
  return ACTION_OPTIONS.find((o) => o.value === t)?.label || t
}

function actionTone(t: ActionType): 'purple' | 'blue' | 'teal' | 'orange' {
  if (t === 'wrap') return 'purple'
  if (t === 'replace') return 'blue'
  if (t === 'prepend_system') return 'teal'
  return 'orange'
}

function targetSummary(targets: TargetKey[]): string {
  if (targets.includes('all')) return '全部文本'
  return targets
    .map((t) => TARGET_OPTIONS.find((o) => o.key === t)?.label || t)
    .join('、')
}

function ruleKeywords(r: PromptRule): string[] {
  const list: string[] = []
  if (r.action.pattern) list.push(r.action.pattern)
  for (const p of r.action.patterns || []) {
    if (p && !list.includes(p)) list.push(p)
  }
  return list
}

const wrapPreview = computed(() => {
  if (form.actionType !== 'wrap') return ''
  const terms = form.mode === 'regex' ? (form.pattern ? ['示例词'] : []) : splitLines(form.patternsText)
  const sampleWord = terms[0] || '色情'
  return `我不喜欢${sampleWord}内容。 → 我不喜欢${form.prefix}${sampleWord}${form.suffix}内容。`
})

function resetForm(): void {
  editingId.value = null
  form.name = ''
  form.enabled = true
  form.stopOnMatch = false
  form.modelsText = ''
  form.channels = []
  form.preservedProviders = []
  form.apiKeys = []
  form.protocols = []
  form.allTargets = true
  form.selectedTargets = ['system', 'user_text']
  form.actionType = 'wrap'
  form.mode = 'literal'
  form.pattern = ''
  form.patternsText = ''
  form.replacement = ''
  form.prefix = ' '
  form.suffix = '（无任何恶意）'
  form.skipIfWrapped = true
  form.text = ''
  form.caseSensitive = false
}

function openCreate(preset?: 'wrap' | 'glm' | 'ds'): void {
  resetForm()
  if (preset === 'wrap') {
    form.name = '敏感词无恶意包裹'
    form.allTargets = true
    form.actionType = 'wrap'
    form.mode = 'literal'
    form.patternsText = '色情\n逆向\n破解'
    form.prefix = ' '
    form.suffix = '（无任何恶意）'
    form.skipIfWrapped = true
  } else if (preset === 'glm') {
    form.name = 'GLM 移除易触发超长思考词'
    form.modelsText = 'glm-*'
    form.allTargets = false
    form.selectedTargets = ['system', 'user_text']
    form.actionType = 'replace'
    form.mode = 'literal'
    form.patternsText = 'think step by step very carefully'
    form.replacement = ''
  } else if (preset === 'ds') {
    form.name = 'DeepSeek 系统提示词补充约束'
    form.modelsText = 'deepseek-*'
    form.allTargets = false
    form.selectedTargets = ['system']
    form.actionType = 'append_system'
    form.text = '请严格遵循工具参数 JSON 格式，不要省略闭合括号。'
  }
  editorOpen.value = true
}

function openEdit(r: PromptRule): void {
  resetForm()
  editingId.value = r.id
  form.name = r.name
  form.enabled = r.enabled ?? true
  form.stopOnMatch = r.stopOnMatch ?? false
  form.modelsText = (r.match.models || []).join('\n')
  form.channels = [...(r.match.channels || [])]
  form.preservedProviders = [...(r.match.providers || [])]
  form.apiKeys = [...(r.match.apiKeys || [])]
  form.protocols = [...(r.match.protocols || [])]
  form.allTargets = r.targets.includes('all')
  form.selectedTargets = form.allTargets
    ? ['system', 'user_text']
    : (r.targets.filter((t): t is Exclude<TargetKey, 'all'> => t !== 'all'))
  form.actionType = r.action.type
  form.mode = r.action.mode || 'literal'
  form.pattern = r.action.pattern || ''
  form.patternsText = ruleKeywords(r).join('\n')
  form.replacement = r.action.replacement || ''
  form.prefix = r.action.prefix || ''
  form.suffix = r.action.suffix || ''
  form.skipIfWrapped = r.action.skipIfWrapped ?? true
  form.text = r.action.text || ''
  form.caseSensitive = r.action.caseSensitive ?? false
  editorOpen.value = true
}

function toggleTarget(key: Exclude<TargetKey, 'all'>): void {
  const i = form.selectedTargets.indexOf(key)
  if (i >= 0) form.selectedTargets.splice(i, 1)
  else form.selectedTargets.push(key)
}

function toggleProtocol(key: ProtocolKey): void {
  const i = form.protocols.indexOf(key)
  if (i >= 0) form.protocols.splice(i, 1)
  else form.protocols.push(key)
}

function addChannelToRule(channelId: string): void {
  if (!channelId || form.channels.includes(channelId)) return
  form.channels.push(channelId)
}

function removeChannelFromRule(channelId: string): void {
  form.channels = form.channels.filter((c) => c !== channelId)
}

function addApiKeyToRule(name: string): void {
  if (!name || form.apiKeys.includes(name)) return
  form.apiKeys.push(name)
}

function removeApiKeyFromRule(name: string): void {
  form.apiKeys = form.apiKeys.filter((k) => k !== name)
}

function buildRulePayload(): Record<string, unknown> | null {
  const name = form.name.trim()
  if (!name) {
    toast('请输入规则名称', 'warning')
    return null
  }
  const isSystemInsert = form.actionType === 'prepend_system' || form.actionType === 'append_system'
  const targets: TargetKey[] = isSystemInsert
    ? ['system']
    : form.allTargets
      ? ['all']
      : [...form.selectedTargets]
  if (!targets.length) {
    toast('请至少选择一个作用位置', 'warning')
    return null
  }

  let pattern = ''
  let patterns: string[] = []
  if (form.actionType === 'replace' || form.actionType === 'wrap') {
    if (form.mode === 'regex') {
      pattern = form.pattern
      if (!pattern) {
        toast('请输入正则表达式', 'warning')
        return null
      }
    } else {
      patterns = splitLines(form.patternsText)
      if (!patterns.length) {
        toast('请至少输入一个关键词（每行一个）', 'warning')
        return null
      }
    }
    if (form.actionType === 'wrap' && !form.prefix && !form.suffix) {
      toast('包裹动作至少需要填写前缀或后缀', 'warning')
      return null
    }
  } else if (!form.text) {
    toast('请输入要插入的系统提示词内容', 'warning')
    return null
  }

  return {
    name,
    enabled: form.enabled,
    stopOnMatch: form.stopOnMatch,
    match: {
      models: splitLinesOrComma(form.modelsText),
      channels: [...form.channels],
      providers: [...form.preservedProviders],
      apiKeys: [...form.apiKeys],
      protocols: [...form.protocols],
    },
    targets,
    action: {
      type: form.actionType,
      mode: form.mode,
      pattern,
      patterns,
      replacement: form.replacement,
      prefix: form.prefix,
      suffix: form.suffix,
      skipIfWrapped: form.skipIfWrapped,
      text: form.text,
      caseSensitive: form.caseSensitive,
    },
  }
}

async function mutate(key: string, fn: () => Promise<PromptRulesData>, okMsg: string): Promise<boolean> {
  busy.value = key
  try {
    data.value = await fn()
    toast(okMsg, 'success')
    return true
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') await load()
    return false
  } finally {
    busy.value = ''
  }
}

async function toggleGlobal(next: boolean): Promise<void> {
  await mutate(
    'global',
    async () => (await api.patch<PromptRulesData>('/prompt-rules/settings', { enabled: next }, { ifMatch: data.value?.revision })).data,
    next ? '已开启请求改写规则引擎' : '已关闭请求改写规则引擎',
  )
}

async function saveRule(): Promise<void> {
  const payload = buildRulePayload()
  if (!payload) return
  const ok = editingId.value
    ? await mutate(
        `save:${editingId.value}`,
        async () => (await api.patch<PromptRulesData>(`/prompt-rules/rules/${enc(editingId.value!)}`, payload, { ifMatch: data.value?.revision })).data,
        '规则已更新',
      )
    : await mutate(
        'create',
        async () => (await api.post<PromptRulesData>('/prompt-rules/rules', payload, { ifMatch: data.value?.revision })).data,
        '规则已创建',
      )
  if (ok) editorOpen.value = false
}

async function toggleRuleEnabled(r: PromptRule, next: boolean): Promise<void> {
  const payload = {
    name: r.name,
    enabled: next,
    match: r.match,
    targets: r.targets,
    action: r.action,
    stopOnMatch: r.stopOnMatch,
  }
  await mutate(
    `toggle:${r.id}`,
    async () => (await api.patch<PromptRulesData>(`/prompt-rules/rules/${enc(r.id)}`, payload, { ifMatch: data.value?.revision })).data,
    next ? `已启用「${r.name}」` : `已停用「${r.name}」`,
  )
}

async function deleteRule(r: PromptRule): Promise<void> {
  const ok = await confirmAction({
    title: `删除规则「${r.name}」？`,
    detail: `规则 ID：${r.id}。删除后立即停止生效。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  await mutate(
    `del:${r.id}`,
    async () => (await api.del<PromptRulesData>(`/prompt-rules/rules/${enc(r.id)}`, { ifMatch: data.value?.revision })).data,
    '规则已删除',
  )
}

async function moveRule(index: number, delta: -1 | 1): Promise<void> {
  const rules = data.value?.rules || []
  const target = index + delta
  if (target < 0 || target >= rules.length) return
  const ids = rules.map((r) => r.id)
  const [moved] = ids.splice(index, 1)
  ids.splice(target, 0, moved)
  await mutate(
    `move:${moved}`,
    async () => (await api.put<PromptRulesData>('/prompt-rules/order', { ruleIds: ids }, { ifMatch: data.value?.revision })).data,
    '执行顺序已更新',
  )
}

async function runTest(): Promise<void> {
  testing.value = true
  try {
    const res = await api.post<PromptRulesTestData>('/prompt-rules/test', {
      model: testModel.value.trim(),
      channelKey: testChannel.value,
      apiKeyName: testApiKey.value,
      protocol: testProtocol.value,
      samples: {
        system: testSystem.value,
        user_text: testUser.value,
        assistant_text: '',
        tool_description: '',
        tool_result_text: '',
      },
    })
    testResult.value = res.data
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <Page
    title="请求改写规则"
    subtitle="在发往上游渠道前按模型、渠道或协议自动替换、包裹关键词，或在系统提示词首尾补充约束。仅改写文本字段，不影响工具 JSON 参数、媒体或加密签名。"
    width="wide"
  >
    <template #actions>
      <Btn icon title="刷新" :loading="loading && !!data" @click="load"><RefreshCw /></Btn>
      <Btn variant="primary" @click="openCreate()"><Plus /> 新建规则</Btn>
    </template>

    <p v-if="error" class="err"><TriangleAlert /> {{ error }} <Btn size="sm" @click="load">重试</Btn></p>
    <div v-else-if="!data" class="sk"><Skeleton h="90px" r="14px" /><Skeleton h="240px" r="14px" /></div>

    <template v-else>
      <Card title="规则引擎总开关" subtitle="按列表顺序依次执行；修改后热生效，无需重启">
        <template #icon><SlidersHorizontal /></template>
        <template #actions>
          <span class="meta-stat">{{ enabledCount }} / {{ data.rules.length }} 条启用</span>
          <Switch :model-value="data.enabled" :loading="busy === 'global'" label="启用请求改写规则引擎" @update:model-value="toggleGlobal" />
        </template>
        <div class="presets">
          <span class="faint presets-label"><Sparkles class="ico" /> 快捷模板：</span>
          <Btn size="sm" @click="openCreate('wrap')">敏感词包裹（无任何恶意）</Btn>
          <Btn size="sm" @click="openCreate('glm')">GLM 防超长思考删词</Btn>
          <Btn size="sm" @click="openCreate('ds')">DeepSeek 系统提示词追加</Btn>
        </div>
      </Card>

      <Card title="规则列表" subtitle="每条规则可作用于所有模型或指定模型/渠道，并可自由选择全部文本或指定区域">
        <Empty
          v-if="!data.rules.length"
          title="还没有配置任何改写规则"
          detail="点击右上角「新建规则」或上方快捷模板开始配置。"
        />
        <div v-else class="rule-list">
          <section v-for="(r, idx) in data.rules" :key="r.id" class="rule-row" :class="{ disabled: !r.enabled }">
            <div class="rule-head">
              <div class="rule-title">
                <span class="ord">#{{ idx + 1 }}</span>
                <Switch :model-value="r.enabled" :loading="busy === `toggle:${r.id}`" :label="`启用 ${r.name}`" @update:model-value="(v) => toggleRuleEnabled(r, v)" />
                <strong>{{ r.name }}</strong>
                <span class="mono faint rid">{{ r.id }}</span>
                <Badge :tone="actionTone(r.action.type)">{{ actionLabel(r.action.type) }}</Badge>
                <Badge v-if="r.action.mode === 'regex' && (r.action.type === 'replace' || r.action.type === 'wrap')" tone="neutral">正则</Badge>
                <Badge v-if="r.stopOnMatch" tone="yellow">命中即停</Badge>
              </div>
              <div class="rule-ops">
                <Btn icon size="sm" title="上移" :disabled="idx === 0 || !!busy" @click="moveRule(idx, -1)"><ArrowUp /></Btn>
                <Btn icon size="sm" title="下移" :disabled="idx === data.rules.length - 1 || !!busy" @click="moveRule(idx, 1)"><ArrowDown /></Btn>
                <Btn icon size="sm" title="编辑" :disabled="!!busy" @click="openEdit(r)"><Pencil /></Btn>
                <Btn icon size="sm" title="删除" :disabled="!!busy" @click="deleteRule(r)"><Trash2 /></Btn>
              </div>
            </div>

            <div class="rule-meta">
              <span><b>模型：</b>{{ r.match.models?.length ? r.match.models.join(', ') : '所有模型' }}</span>
              <span><b>位置：</b>{{ targetSummary(r.targets) }}</span>
              <span v-if="r.match.channels?.length"><b>渠道：</b>{{ r.match.channels.map(formatChannelLabel).join(', ') }}</span>
              <span v-if="r.match.providers?.length"><b>Provider：</b>{{ r.match.providers.join(', ') }}</span>
              <span v-if="r.match.protocols?.length"><b>协议：</b>{{ r.match.protocols.join(', ') }}</span>
              <span v-if="r.match.apiKeys?.length"><b>API Key：</b>{{ r.match.apiKeys.join(', ') }}</span>
            </div>

            <div class="rule-preview mono">
              <template v-if="r.action.type === 'wrap'">
                关键词：[{{ ruleKeywords(r).join(' / ') }}] →
                <span class="hl">{{ r.action.prefix }}&lt;原词&gt;{{ r.action.suffix }}</span>
                <span v-if="r.action.skipIfWrapped" class="faint">（防重复包裹）</span>
              </template>
              <template v-else-if="r.action.type === 'replace'">
                匹配：[{{ ruleKeywords(r).join(' / ') }}] →
                <span class="hl">{{ r.action.replacement ? r.action.replacement : '（删除）' }}</span>
              </template>
              <template v-else>
                {{ r.action.type === 'prepend_system' ? '前置：' : '追加：' }}
                <span class="hl">{{ r.action.text }}</span>
              </template>
            </div>
          </section>
        </div>
      </Card>

      <Card title="试运行测试器（Dry-run）" subtitle="使用当前已保存规则在本地预览改写效果，不会发送上游请求">
        <template #icon><Play /></template>
        <div class="test-grid">
          <Field label="测试模型">
            <TextInput v-model="testModel" mono placeholder="如 glm-5.3 / deepseek-v4.1-flash / gemini-4-neon" />
          </Field>
          <Field label="测试渠道">
            <Select v-model="testChannel" :options="testChannelOptions" width="100%" />
          </Field>
          <Field label="测试 API Key">
            <Select v-model="testApiKey" :options="testApiKeyOptions" width="100%" />
          </Field>
          <Field label="入口协议">
            <Select
              v-model="testProtocol"
              :options="PROTOCOL_OPTIONS.map((p) => ({ value: p.key, label: p.label }))"
              width="100%"
            />
          </Field>
        </div>
        <div class="test-inputs">
          <Field label="系统提示词样例">
            <TextArea v-model="testSystem" :rows="3" placeholder="输入测试用的系统提示词" />
          </Field>
          <Field label="用户消息样例">
            <TextArea v-model="testUser" :rows="3" placeholder="输入测试用的用户消息" />
          </Field>
        </div>
        <div class="test-actions">
          <Btn variant="primary" :loading="testing" @click="runTest"><Play /> 运行试测</Btn>
        </div>

        <div v-if="testResult" class="test-result">
          <div class="res-bar">
            <Badge v-if="!testResult.enabled" tone="orange">规则引擎总开关已关闭，当前不会执行改写</Badge>
            <Badge v-else :tone="testResult.changed ? 'green' : 'neutral'">
              {{ testResult.changed ? `已改写 ${testResult.totalReplacements} 处` : '未触发任何改写' }}
            </Badge>
            <span v-for="h in testResult.hits" :key="h.id" class="hit-chip mono">
              {{ h.name }} ×{{ h.replacements }} ({{ h.targets.join(',') }})
            </span>
          </div>
          <div v-if="testResult.after.system !== testResult.before.system" class="diff-box">
            <div class="diff-label">系统提示词改写结果</div>
            <pre class="mono">{{ testResult.after.system }}</pre>
          </div>
          <div v-if="testResult.after.user_text !== testResult.before.user_text" class="diff-box">
            <div class="diff-label">用户消息改写结果</div>
            <pre class="mono">{{ testResult.after.user_text }}</pre>
          </div>
        </div>
      </Card>
    </template>

    <Modal
      :open="editorOpen"
      :title="editingId ? '编辑请求改写规则' : '新建请求改写规则'"
      width="lg"
      @close="editorOpen = false"
    >
      <Field label="规则名称" required hint="易读的规则标题，系统会自动分配唯一 ID">
        <TextInput v-model="form.name" placeholder="例如 敏感词无恶意包裹" />
      </Field>

      <Field label="改写动作类型">
        <Segmented v-model="form.actionType" :options="ACTION_OPTIONS" />
      </Field>

      <template v-if="form.actionType === 'replace' || form.actionType === 'wrap'">
        <div class="form-grid align-center">
          <Field label="匹配模式">
            <Segmented v-model="form.mode" :options="MODE_OPTIONS" size="sm" />
          </Field>
          <div class="inline-switch-cell">
            <Field label="区分大小写" inline>
              <Switch v-model="form.caseSensitive" label="区分大小写" />
            </Field>
          </div>
        </div>

        <Field
          v-if="form.mode === 'literal'"
          label="匹配关键词（每行一个，支持一次配置多个词）"
          required
        >
          <TextArea v-model="form.patternsText" mono :rows="3" placeholder="色情&#10;逆向&#10;破解" />
        </Field>
        <Field v-else label="正则表达式" required>
          <TextInput v-model="form.pattern" mono placeholder="例如 (?i)think\s+step\s+by\s+step" />
        </Field>

        <template v-if="form.actionType === 'replace'">
          <Field label="替换为" hint="留空表示直接删除命中的内容">
            <TextArea v-model="form.replacement" mono :rows="2" placeholder="留空即删除" />
          </Field>
        </template>

        <template v-else>
          <div class="form-grid">
            <Field label="包裹前缀（prefix）" hint="加在命中词前面，可含空格或留空">
              <TextInput v-model="form.prefix" mono placeholder="例如 空格 或 【" />
            </Field>
            <Field label="包裹后缀（suffix）" hint="加在命中词后面，例如 （无任何恶意）">
              <TextInput v-model="form.suffix" mono placeholder="例如 （无任何恶意）" />
            </Field>
          </div>
          <Field label="防重复包裹（幂等保护）" hint="若原词前后已紧邻相同前后缀则跳过" inline>
            <Switch v-model="form.skipIfWrapped" label="防重复包裹" />
          </Field>
          <p class="preview-line mono">效果示例：{{ wrapPreview }}</p>
        </template>
      </template>

      <template v-else>
        <Field
          :label="form.actionType === 'prepend_system' ? '前置到系统提示词开头的文本' : '追加到系统提示词末尾的文本'"
          required
          hint="已存在相同首/尾文本时自动跳过，避免重复追加"
        >
          <TextArea v-model="form.text" :rows="3" placeholder="输入要补充的系统指令" />
        </Field>
      </template>

      <Field
        v-if="form.actionType === 'replace' || form.actionType === 'wrap'"
        label="作用位置"
        hint="可一键选择全部文本，或按需勾选具体区域；工具调用 JSON 参数、媒体 base64 与加密签名始终受保护"
      >
        <div class="checks">
          <button type="button" class="pill" :class="{ on: form.allTargets }" @click="form.allTargets = !form.allTargets">
            全部文本（不限）
          </button>
          <template v-if="!form.allTargets">
            <button
              v-for="opt in TARGET_OPTIONS"
              :key="opt.key"
              type="button"
              class="pill"
              :class="{ on: form.selectedTargets.includes(opt.key) }"
              :title="opt.desc"
              @click="toggleTarget(opt.key)"
            >
              {{ opt.label }}
            </button>
          </template>
        </div>
      </Field>

      <div class="divider" />

      <div class="form-grid">
        <Field label="生效模型" hint="留空或填 * 表示所有模型；支持通配符如 glm-*、deepseek-*（逗号或换行分隔）">
          <TextArea v-model="form.modelsText" mono :rows="2" placeholder="留空 = 所有模型" />
        </Field>
        <Field label="限定 API Key（留空=所有 Key）">
          <Select
            model-value=""
            :options="apiKeyOptions"
            placeholder="添加限定 API Key..."
            width="100%"
            @update:model-value="(v) => addApiKeyToRule(String(v))"
          />
          <div v-if="form.apiKeys.length" class="chips">
            <span v-for="k in form.apiKeys" :key="k" class="chip">
              {{ k }}
              <button type="button" @click="removeApiKeyFromRule(k)">×</button>
            </span>
          </div>
        </Field>
      </div>

      <div class="form-grid">
        <Field label="限定渠道（留空=所有渠道，含 API 与 OAuth）">
          <Select
            model-value=""
            :options="channelOptions"
            placeholder="添加限定渠道..."
            width="100%"
            @update:model-value="(v) => addChannelToRule(String(v))"
          />
          <div v-if="form.channels.length" class="chips">
            <span v-for="ch in form.channels" :key="ch" class="chip">
              <ProviderIcon v-if="isOAuthChannel(ch)" :provider="channelProvider(ch)" :size="14" />
              <span v-else class="chip-tag">API</span>
              <span>{{ isOAuthChannel(ch) ? formatChannelLabel(ch) : (catalog.channels[ch]?.name || channelName(ch)) }}</span>
              <button type="button" @click="removeChannelFromRule(ch)">×</button>
            </span>
          </div>
        </Field>
        <Field label="限定入口协议（留空=全部协议）">
          <div class="checks">
            <button
              v-for="p in PROTOCOL_OPTIONS"
              :key="p.key"
              type="button"
              class="pill"
              :class="{ on: form.protocols.includes(p.key) }"
              @click="toggleProtocol(p.key)"
            >
              {{ p.label }}
            </button>
          </div>
        </Field>
      </div>

      <div class="switch-grid">
        <Field label="启用本规则" inline>
          <Switch v-model="form.enabled" label="启用本规则" />
        </Field>
        <Field label="命中后停止后续规则" inline>
          <Switch v-model="form.stopOnMatch" label="命中后停止后续规则" />
        </Field>
      </div>

      <template #footer>
        <Btn @click="editorOpen = false">取消</Btn>
        <Btn variant="primary" :loading="busy === 'create' || busy.startsWith('save:')" @click="saveRule">保存规则</Btn>
      </template>
    </Modal>
  </Page>
</template>

<style scoped>
.meta-stat {
  font-size: 12px;
  color: var(--text-3);
  margin-right: 8px;
}
.presets {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.presets-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}
.ico {
  width: 14px;
  height: 14px;
}
.rule-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.rule-row {
  padding: 12px 14px;
  border-radius: 10px;
  border: 0.5px solid var(--hairline);
  background: var(--fill-3, transparent);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rule-row.disabled {
  opacity: 0.58;
}
.rule-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.rule-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ord {
  font-size: 11.5px;
  color: var(--text-3);
  min-width: 24px;
}
.rid {
  font-size: 11.5px;
}
.rule-ops {
  display: flex;
  align-items: center;
  gap: 4px;
}
.rule-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  font-size: 12px;
  color: var(--text-2);
}
.rule-meta b {
  color: var(--text-3);
  font-weight: 500;
}
.rule-preview {
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 7px;
  background: var(--fill-2);
  color: var(--text-2);
  word-break: break-all;
}
.hl {
  color: var(--accent, var(--blue));
  font-weight: 500;
}
.test-grid,
.test-inputs,
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.test-inputs {
  margin-top: 10px;
}
.test-actions {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
.test-result {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 0.5px solid var(--hairline);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.res-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.hit-chip {
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--fill-2);
}
.diff-box {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--fill-2);
}
.diff-label {
  font-size: 11.5px;
  color: var(--text-3);
  margin-bottom: 4px;
}
.diff-box pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
}
.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.pill {
  padding: 5px 10px;
  border-radius: 7px;
  font-size: 12px;
  border: 0.5px solid var(--border);
  background: transparent;
  color: var(--text-2);
  cursor: pointer;
}
.pill.on {
  border-color: var(--blue);
  background: color-mix(in srgb, var(--blue) 14%, transparent);
  color: var(--text);
  font-weight: 500;
}
.preview-line {
  font-size: 12px;
  padding: 8px 10px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--purple) 10%, transparent);
  color: var(--text);
}
.divider {
  height: 0.5px;
  background: var(--hairline);
  margin: 12px 0;
}
.align-center {
  align-items: end;
}
.inline-switch-cell :deep(.field.inline) {
  border-bottom: 0;
  padding: 6px 0;
}
.switch-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 6px;
  padding-top: 8px;
  border-top: 0.5px solid var(--hairline);
}
.switch-grid :deep(.field.inline) {
  border-bottom: 0;
  padding: 6px 0;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  padding: 3px 8px;
  border-radius: 6px;
  background: var(--fill-2);
}
.chip-tag {
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  padding: 2px 4px;
  border-radius: 4px;
  background: var(--fill-3, rgba(127, 127, 127, 0.16));
  color: var(--text-3);
}
.chip button {
  color: var(--text-3);
  cursor: pointer;
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--red);
  font-size: 13px;
}
@media (max-width: 720px) {
  .test-grid,
  .test-inputs,
  .form-grid,
  .switch-grid {
    grid-template-columns: 1fr;
  }
}
</style>
