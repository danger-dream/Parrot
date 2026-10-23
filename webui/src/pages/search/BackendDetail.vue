<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Check, ExternalLink, FlaskConical, KeyRound, ListFilter, Plus, Replace, Trash2, TriangleAlert, Users, X } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { waitOperation, operationErrorText, type Operation } from '@/api/operations'
import { confirmAction, promptText } from '@/stores/dialogs'
import { closeInspector, toast } from '@/stores/ui'
import { dateTime, integer, newIdempotencyKey } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import { openAccount, openModel } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import TextInput from '@/components/ui/TextInput.vue'
import DetailSection from '@/components/DetailSection.vue'
import {
  ACCOUNT_TYPES,
  errorLabel,
  EXTRACT_TYPES,
  KEY_TYPES,
  kindLabel,
  MODEL_TYPES,
  OPERATION_LABEL,
  stateOf,
  TYPE_NAME,
  ZHIPU_SITES,
  type Settings,
} from './labels'
import { mutateSearch, searchRes, searchUi } from './store'

type Account = Schemas['SearchAccountData']
type CallLog = Schemas['SearchCallLogData']
/** `GET /search/backends/{id}/models` (getSearchBackendModels). */
interface ModelChoices {
  backendId: string
  supported: boolean
  selected: string
  default: string
  models: string[]
}
interface TestHit {
  title?: string | null
  url?: string | null
}

const props = defineProps<{ id: string }>()

const b = computed(() => searchRes.value?.data.value?.backends.find((x) => x.id === props.id) || null)
const position = computed(() => (searchRes.value?.data.value?.backends.findIndex((x) => x.id === props.id) ?? -1) + 1)
const count = computed(() => searchRes.value?.data.value?.backends.length || 0)

const form = ref({ name: '', endpoint: '', model: '' })
function syncForm(): void {
  if (!b.value) return
  form.value = { name: b.value.name, endpoint: b.value.endpoint, model: b.value.model }
  if (choices.value) customModel.value = Boolean(form.value.model) && !choices.value.models.includes(form.value.model)
}
watch(b, (now, before) => {
  if (!before || !now) return syncForm()
  // Only overwrite fields the user has not been editing.
  if (form.value.name === before.name) form.value.name = now.name
  if (form.value.endpoint === before.endpoint) form.value.endpoint = now.endpoint
  if (form.value.model === before.model) form.value.model = now.model
})
onMounted(syncForm)

const busy = ref('')

// ---- model choices (OAuth sources only) ----
const CUSTOM = '__custom__'
const choices = ref<ModelChoices | null>(null)
const customModel = ref(false)
async function loadModels(): Promise<void> {
  if (!b.value || !MODEL_TYPES.has(b.value.type)) return
  try {
    choices.value = (await api.get<ModelChoices>(`/search/backends/${enc(props.id)}/models`)).data
  } catch {
    choices.value = null
  }
  customModel.value = Boolean(form.value.model) && !choices.value?.models.includes(form.value.model)
}
onMounted(loadModels)
const defaultModel = computed(() => choices.value?.default || '')
const modelOptions = computed(() => {
  const list = choices.value?.models || []
  return [
    { value: '', label: defaultModel.value ? `自动（默认 ${defaultModel.value}）` : '自动' },
    ...list.map((m) => ({ value: m, label: m })),
    { value: CUSTOM, label: '手动输入…' },
  ]
})
const modelChoice = computed<string>({
  get: () => (customModel.value ? CUSTOM : form.value.model),
  set: (v) => {
    if (v === CUSTOM) {
      customModel.value = true
      return
    }
    customModel.value = false
    form.value.model = v
  },
})

async function patch(body: Record<string, unknown>, ok: string, tag = 'patch'): Promise<boolean> {
  busy.value = tag
  try {
    return await mutateSearch(
      (rev) => api.patch<Settings>(`/search/backends/${enc(props.id)}`, body, { ifMatch: rev }),
      ok,
    )
  } finally {
    busy.value = ''
  }
}

const basicDirty = computed(() => {
  if (!b.value) return false
  return (
    form.value.name.trim() !== b.value.name ||
    (KEY_TYPES.has(b.value.type) && form.value.endpoint.trim() !== b.value.endpoint) ||
    (MODEL_TYPES.has(b.value.type) && form.value.model.trim() !== b.value.model)
  )
})

async function saveBasic(): Promise<void> {
  const cur = b.value
  if (!cur) return
  const body: Record<string, unknown> = {}
  if (form.value.name.trim() !== cur.name) {
    if (!form.value.name.trim()) return toast('名称不能为空', 'warning')
    body.name = form.value.name.trim()
  }
  if (KEY_TYPES.has(cur.type) && form.value.endpoint.trim() !== cur.endpoint) body.endpoint = form.value.endpoint.trim()
  if (MODEL_TYPES.has(cur.type) && form.value.model.trim() !== cur.model) body.model = form.value.model.trim()
  if (Object.keys(body).length) await patch(body, '已保存', 'basic')
}

async function toggleEnabled(v: boolean): Promise<void> {
  await patch({ enabled: v }, v ? '已启用该来源' : '已停用该来源', 'enabled')
}

// ---- keys (write-only) ----
function parseKeys(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
}
async function addKeys(): Promise<void> {
  const text = await promptText({
    title: '追加 API Key',
    detail: '每行一个；保存后不会再显示 Key 的内容。',
    multiline: true,
    mono: true,
    placeholder: 'tvly-…',
    confirmText: '追加',
  })
  if (!text) return
  const keys = parseKeys(text)
  if (keys.length) await patch({ addApiKeys: keys }, `已追加 ${keys.length} 个 Key`, 'keys')
}
async function replaceKeys(): Promise<void> {
  const text = await promptText({
    title: '替换全部 API Key',
    detail: `现有 ${b.value?.keyCount ?? 0} 个 Key 会被全部替换为下面填写的内容。每行一个。`,
    multiline: true,
    mono: true,
    confirmText: '替换',
  })
  if (!text) return
  const keys = parseKeys(text)
  if (!keys.length) return
  const ok = await confirmAction({
    title: '确认替换全部 Key？',
    detail: `原有的 ${b.value?.keyCount ?? 0} 个 Key 将被删除，替换为新的 ${keys.length} 个。此操作不可撤销。`,
    danger: true,
    confirmText: '替换',
  })
  if (ok) await patch({ apiKeys: keys }, '已替换全部 Key', 'keys')
}
async function removeKey(index: number): Promise<void> {
  const ok = await confirmAction({
    title: `移除第 ${index + 1} 个 Key？`,
    detail: 'Key 内容不可查看，移除后无法恢复，需要时请重新追加。',
    danger: true,
    confirmText: '移除',
  })
  if (ok) await patch({ removeKeyIndices: [index] }, `已移除第 ${index + 1} 个 Key`, 'keys')
}

// ---- accounts ----
const accounts = ref<Account[] | null>(null)
const accError = ref('')
const picked = ref<string[]>([])
async function loadAccounts(): Promise<void> {
  if (!b.value || !ACCOUNT_TYPES.has(b.value.type)) return
  try {
    accounts.value = (await api.get<Account[]>(`/search/backends/${enc(props.id)}/accounts`)).data
    accError.value = ''
  } catch (err) {
    accError.value = errorTitle(err)
  }
}
watch(
  () => b.value?.accountIds,
  (ids) => (picked.value = [...(ids || [])]),
  { immediate: true },
)
onMounted(loadAccounts)
const accDirty = computed(() => JSON.stringify([...picked.value].sort()) !== JSON.stringify([...(b.value?.accountIds || [])].sort()))
function togglePick(id: string): void {
  picked.value = picked.value.includes(id) ? picked.value.filter((x) => x !== id) : [...picked.value, id]
}
async function saveAccounts(): Promise<void> {
  await patch({ accountIds: picked.value }, picked.value.length ? `已指定 ${picked.value.length} 个账户` : '已改为使用全部符合条件账户', 'accounts')
}

// ---- test ----
const testOp = ref<'search' | 'extract'>('search')
const testInput = ref('')
const testing = ref(false)
const testResult = ref<{ ok: boolean; text: string; log?: CallLog | null; hits?: TestHit[] } | null>(null)

async function runTest(): Promise<void> {
  const value = testInput.value.trim()
  if (!value || !b.value) return
  testing.value = true
  testResult.value = null
  try {
    const body = { backendId: props.id, operation: testOp.value, ...(testOp.value === 'search' ? { query: value } : { url: value }) }
    const op = (await api.post<Operation>('/search/test', body, { headers: { 'Idempotency-Key': newIdempotencyKey() } })).data
    const done = await waitOperation(op)
    let log: CallLog | null = null
    try {
      const logs = (await api.get<CallLog[]>('/search/logs', { period: 'today', sourceId: props.id, limit: 5 })).data
      log = logs.find((l) => (testOp.value === 'search' ? l.query === value : l.url === value)) || null
    } catch {
      log = null
    }
    if (done.status === 'succeeded') {
      const r = (done.result || {}) as { resultCount?: number; contentChars?: number; attemptCount?: number; results?: TestHit[] }
      testResult.value = {
        ok: true,
        text: `测试成功 · 返回 ${integer(r.resultCount ?? 0)} 条结果 · 正文 ${integer(r.contentChars ?? 0)} 字符 · 尝试 ${integer(r.attemptCount ?? 0)} 次`,
        log,
        hits: Array.isArray(r.results) ? r.results : [],
      }
    } else {
      testResult.value = { ok: false, text: `测试失败：${errorLabel(log?.errorCode) || operationErrorText(done)}`, log }
    }
    searchUi.logsTick++
  } catch (err) {
    testResult.value = { ok: false, text: `测试失败：${errorTitle(err)}${errorDetail(err) ? `（${errorDetail(err)}）` : ''}` }
  } finally {
    testing.value = false
  }
}

// ---- delete ----
async function remove(): Promise<void> {
  const cur = b.value
  const res = searchRes.value
  if (!cur || !res) return
  const ok = await confirmAction({
    title: `删除搜索来源「${cur.name}」？`,
    detail:
      (KEY_TYPES.has(cur.type) ? `其中保存的 ${cur.keyCount} 个 Key 会一并删除。` : '') +
      '只移除这个来源的配置，不影响 OAuth 账户或其它来源。删除后不可恢复。',
    danger: true,
    confirmText: '删除',
  })
  if (!ok) return
  busy.value = 'delete'
  const done = await mutateSearch((rev) => api.del<Settings>(`/search/backends/${enc(props.id)}`, { ifMatch: rev }), '已删除搜索来源')
  busy.value = ''
  if (done) closeInspector()
}

function showLogs(): void {
  searchUi.logSource = props.id
  closeInspector()
  window.setTimeout(() => document.getElementById('search-logs')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60)
}
</script>

<template>
  <div class="bd">
    <div v-if="!searchRes?.data.value" class="skl"><Skeleton h="20px" w="40%" /><Skeleton h="120px" r="10px" /></div>
    <p v-else-if="!b" class="faint">这个搜索来源已不存在。</p>
    <template v-else>
      <div class="hero">
        <div class="t">
          <p class="name">{{ b.name }}</p>
          <p class="sub">{{ TYPE_NAME[b.type] || b.type }} · {{ kindLabel(b.type) }} · 第 {{ position }} / {{ count }} 位</p>
          <div class="badges">
            <Badge :tone="stateOf(b).tone" dot>{{ stateOf(b).label }}</Badge>
            <Badge v-if="!b.verified" tone="orange" title="这类来源的实现尚未经过实际验证">未验证</Badge>
            <Badge v-if="b.reason === 'configured' && b.enabled" tone="neutral">配置齐全（不代表在线探活）</Badge>
          </div>
        </div>
        <Switch :model-value="b.enabled" :loading="busy === 'enabled'" label="启用" @update:model-value="toggleEnabled" />
      </div>

      <DetailSection title="基本设置">
        <div class="form">
          <Field label="名称">
            <TextInput v-model="form.name" />
          </Field>
          <Field v-if="b.type === 'zhipu'" label="独立 Key 站点" hint="只影响独立填写的 Coding Plan Key；引用的账号按账号自身站点连接。">
            <Select v-model="form.endpoint" :options="ZHIPU_SITES" />
          </Field>
          <Field v-else-if="KEY_TYPES.has(b.type)" label="接口地址" hint="留空使用默认地址。">
            <TextInput v-model="form.endpoint" mono placeholder="默认" clearable />
          </Field>
          <Field
            v-if="MODEL_TYPES.has(b.type)"
            label="搜索模型"
            :hint="
              choices && !choices.models.length
                ? '当前账户没有可用的模型目录，可以保持自动，或手动输入模型名。搜索会真实调用该模型并计入费用。'
                : '从该来源可用账户的模型目录中选择，也可以手动输入。搜索会真实调用该模型并计入费用。'
            "
          >
            <div class="row-inline">
              <Select v-model="modelChoice" :options="modelOptions" />
              <Btn
                size="sm"
                variant="ghost"
                icon
                title="查看模型"
                :disabled="!(form.model || defaultModel)"
                @click="openModel(form.model || defaultModel)"
              >
                <ExternalLink />
              </Btn>
            </div>
            <TextInput v-if="customModel" v-model="form.model" class="custom" mono placeholder="输入模型名称" clearable />
          </Field>
          <div class="acts">
            <Btn variant="primary" size="sm" :disabled="!basicDirty" :loading="busy === 'basic'" @click="saveBasic"><Check /> 保存</Btn>
            <Btn v-if="basicDirty" variant="ghost" size="sm" @click="syncForm">还原</Btn>
          </div>
        </div>
      </DetailSection>

      <DetailSection v-if="KEY_TYPES.has(b.type)" title="API Key" :note="`已设置 ${b.keyCount} 个`">
        <p class="hint">Key 只写不读，不会回显；修改不影响其它来源。{{ b.type === 'zhipu' ? '先尝试独立 Key，再尝试所选账号的模型 Key。' : '' }}</p>
        <div v-if="b.keyCount" class="keys">
          <span v-for="i in b.keyCount" :key="i" class="key">
            <KeyRound />Key #{{ i }}
            <button type="button" title="移除" :disabled="busy === 'keys'" @click="removeKey(i - 1)"><X /></button>
          </span>
        </div>
        <div class="acts">
          <Btn size="sm" :loading="busy === 'keys'" @click="addKeys"><Plus /> 追加 Key</Btn>
          <Btn size="sm" :disabled="busy === 'keys'" @click="replaceKeys"><Replace /> 替换全部</Btn>
        </div>
      </DetailSection>

      <DetailSection
        v-if="ACCOUNT_TYPES.has(b.type)"
        title="使用的账户"
        :note="b.accountIds.length ? `指定 ${b.accountIds.length} 个` : '全部符合条件账户'"
      >
        <p class="hint">不勾选任何账户 = 使用全部符合条件账户。只影响搜索选用的账户，不改变普通对话的账户状态。</p>
        <p v-if="accError" class="faint">{{ accError }}</p>
        <div v-else-if="!accounts" class="skl"><Skeleton h="14px" /><Skeleton h="14px" w="70%" /></div>
        <p v-else-if="!accounts.length" class="faint small">此来源暂时没有符合条件的账户。</p>
        <ul v-else class="accs">
          <li v-for="a in accounts" :key="a.id">
            <button type="button" class="ck" :class="{ on: picked.includes(a.id) }" :aria-pressed="picked.includes(a.id)" @click="togglePick(a.id)">
              <Check v-if="picked.includes(a.id)" />
            </button>
            <button type="button" class="an truncate" :title="a.id" @click="openAccount(a.id, a.name)">{{ a.name }}</button>
            <Badge v-if="!a.enabled" tone="orange">普通对话已停用</Badge>
            <Badge v-if="!a.credentialConfigured" tone="red">缺凭据</Badge>
          </li>
        </ul>
        <Field inline label="允许使用手动停用的账户" hint="仅允许你手动停用的账户参与搜索；不会绕过认证或额度失效。">
          <Switch
            :model-value="b.allowDisabledAccounts"
            :loading="busy === 'allow'"
            @update:model-value="(v: boolean) => patch({ allowDisabledAccounts: v }, v ? '已允许' : '已关闭', 'allow')"
          />
        </Field>
        <div v-if="accDirty" class="acts">
          <Btn variant="primary" size="sm" :loading="busy === 'accounts'" @click="saveAccounts"><Users /> 保存账户选择</Btn>
          <Btn variant="ghost" size="sm" @click="picked = [...b.accountIds]">还原</Btn>
        </div>
      </DetailSection>

      <DetailSection title="测试" note="只调用这一个来源，失败不会切换到其它来源">
        <div class="test">
          <Segmented
            v-if="EXTRACT_TYPES.has(b.type)"
            v-model="testOp"
            size="sm"
            :options="[
              { value: 'search', label: '测试搜索' },
              { value: 'extract', label: '测试提取' },
            ]"
          />
          <div class="row-inline">
            <TextInput
              v-model="testInput"
              :placeholder="testOp === 'search' ? '输入搜索词' : '输入公开网页地址 https://…'"
              :mono="testOp === 'extract'"
              @enter="runTest"
            />
            <Btn variant="primary" :loading="testing" :disabled="!testInput.trim()" @click="runTest"><FlaskConical /> 测试</Btn>
          </div>
          <p class="hint">会真实调用该来源，可能消耗额度。</p>
          <div v-if="testResult" class="result" :class="testResult.ok ? 'ok' : 'bad'">
            <p class="rt"><component :is="testResult.ok ? Check : TriangleAlert" /> {{ testResult.text }}</p>
            <dl v-if="testResult.log" class="rl">
              <div><dt>操作</dt><dd>{{ OPERATION_LABEL[testResult.log.operation] || testResult.log.operation }}</dd></div>
              <div><dt>耗时</dt><dd class="mono">{{ testResult.log.elapsedMs ?? '—' }} ms</dd></div>
              <div><dt>结果</dt><dd class="mono">{{ testResult.log.resultCount }} 条 · {{ integer(testResult.log.contentChars) }} 字符</dd></div>
              <div><dt>凭据</dt><dd class="mono truncate">{{ testResult.log.credentialLabel || '—' }}</dd></div>
              <div v-if="testResult.log.model"><dt>模型</dt><dd><button type="button" class="lk mono" @click="openModel(testResult.log.model)">{{ testResult.log.model }}</button></dd></div>
              <div><dt>时间</dt><dd>{{ dateTime(testResult.log.startedAt) }}</dd></div>
            </dl>
            <template v-if="testResult.ok && testResult.hits">
              <ol v-if="testResult.hits.length" class="hits">
                <li v-for="(h, i) in testResult.hits" :key="i">
                  <a v-if="h.url" :href="h.url" target="_blank" rel="noopener noreferrer" class="ht">{{ h.title || h.url }}</a>
                  <span v-else class="ht">{{ h.title || '（无标题）' }}</span>
                  <span v-if="h.url" class="hu mono">{{ h.url }}</span>
                </li>
              </ol>
              <p v-else class="hint">这次测试没有返回网页条目{{ testOp === 'extract' ? '（提取只返回正文）' : '' }}。</p>
              <p v-if="(testResult.hits.length || 0) >= 5" class="hint">只显示前 5 条。</p>
            </template>
          </div>
        </div>
      </DetailSection>

      <div class="acts">
        <Btn @click="showLogs"><ListFilter /> 查看该来源的调用记录</Btn>
        <Btn variant="danger" :loading="busy === 'delete'" @click="remove"><Trash2 /> 删除来源</Btn>
      </div>
      <p class="faint mono id">来源 ID：{{ id }}</p>
    </template>
  </div>
</template>

<style scoped>
.bd {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.skl {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hero {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.t {
  flex: 1;
  min-width: 0;
}
.name {
  font-size: 15px;
  font-weight: 650;
}
.sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-2);
}
.badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.row-inline {
  display: flex;
  align-items: center;
  gap: 6px;
}
.acts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.hint {
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-3);
}
.small {
  font-size: 12px;
}
.keys {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.key {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 4px 0 9px;
  border-radius: 7px;
  font-size: 12px;
  background: var(--fill-2);
}
.key svg {
  width: 12px;
  height: 12px;
  color: var(--text-3);
}
.key button {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 5px;
}
.key button:hover {
  color: var(--red);
  background: var(--fill-2);
}
.accs {
  list-style: none;
  margin: 0;
  padding: 4px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
  max-height: 260px;
  overflow-y: auto;
}
.accs li {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 0 6px;
  border-radius: 6px;
}
.accs li:hover {
  background: var(--fill-3);
}
.ck {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  border-radius: 4px;
  color: #fff;
  box-shadow: inset 0 0 0 1px var(--border-strong);
}
.ck.on {
  background: var(--accent);
  box-shadow: none;
}
.ck svg {
  width: 12px;
  height: 12px;
  stroke-width: 3;
}
.an {
  flex: 1;
  text-align: left;
  font-size: 12.5px;
}
.an:hover,
.lk:hover {
  color: var(--accent);
}
.test {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.result {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.result.ok {
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
.result.bad {
  background: color-mix(in srgb, var(--red) 10%, transparent);
}
.rt {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 550;
}
.ok .rt { color: var(--green); }
.bad .rt { color: var(--red); }
.rt svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.rl {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 12px;
}
.rl dt {
  font-size: 11px;
  color: var(--text-3);
}
.rl dd {
  margin: 1px 0 0;
  font-size: 12px;
  min-width: 0;
}
.id {
  font-size: 11px;
}
.custom {
  margin-top: 6px;
}
.hits {
  margin: 0;
  padding: 0 0 0 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}
.hits li {
  min-width: 0;
}
.ht {
  display: block;
  color: var(--accent);
  font-weight: 550;
  word-break: break-word;
}
a.ht:hover {
  text-decoration: underline;
}
.hu {
  display: block;
  font-size: 11px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
