<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Database, ExternalLink, FlaskConical, Languages, Pencil, RotateCcw, Trash2, TriangleAlert } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { waitOperation, operationErrorText, type Operation } from '@/api/operations'
import { useResource, useSection } from '@/composables/useResource'
import { catalog, channelName, refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { integer } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import { openChannel, openModel } from '@/links'
import Page from '@/components/ui/Page.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Field from '@/components/ui/Field.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import TextArea from '@/components/ui/TextArea.vue'

// `defaultPrompt` (round 2, read-only) is not in the generated schema yet.
type Settings = Schemas['TranslationSettingsData'] & { defaultPrompt?: string }
type Cache = Schemas['TranslationCacheData']
type Langs = Schemas['TranslationLanguagesData']

const res = useResource<Settings>('/translation', { successText: '翻译设置已保存' })
const d = computed(() => res.draft.value)
const loading = computed(() => res.loading.value && !d.value)

const main = useSection(res, ['enabled', 'model', 'fallbackModel', 'targetLanguage', 'translateSystemMessages'])
const nums = useSection(res, [
  'timeoutSeconds',
  'maxHistoryMessages',
  'cacheTtlDays',
  'cachePreloadCount',
  'failureAlertThreshold',
  'memoryCacheMaxMb',
  'memoryCacheTtlSeconds',
])
const scope = useSection(res, ['scope'])
const overrides = useSection(res, ['modelOverrides'])
const promptSec = useSection(res, ['prompt'])

// ---- options ----
const models = ref<string[]>([])
const langs = ref<Langs['items']>([])
onMounted(async () => {
  refreshCatalog()
  try {
    langs.value = (await api.get<Langs>('/translation/languages')).data.items
  } catch {
    langs.value = []
  }
  try {
    const r = await api.get<{ modelId: string; globalEnabled: boolean }[]>('/models', { type: 'chat', pageSize: 200 })
    models.value = r.data.map((m) => m.modelId)
  } catch {
    models.value = []
  }
})

const modelOptions = computed(() => {
  const list = new Set(models.value)
  if (d.value?.model) list.add(d.value.model)
  return [...list].sort().map((m) => ({ value: m, label: m }))
})
const fallbackOptions = computed(() => {
  const list = new Set(models.value)
  if (d.value?.fallbackModel) list.add(d.value.fallbackModel)
  return [{ value: '', label: '不使用备用模型' }, ...[...list].sort().map((m) => ({ value: m, label: m }))]
})
const langOptions = computed(() => {
  const items = langs.value.map((l) => ({ value: l.id, label: l.displayName }))
  if (d.value?.targetLanguage && !items.some((i) => i.value === d.value!.targetLanguage)) items.unshift({ value: d.value.targetLanguage, label: d.value.targetLanguage })
  return items
})
const scopeModelOptions = computed(() => {
  const list = new Set(models.value)
  for (const m of d.value?.scope.models || []) list.add(m)
  return [...list].sort().map((m) => ({ value: m }))
})
const scopeChannelOptions = computed(() => {
  const ids = new Set(Object.keys(catalog.channels))
  for (const c of d.value?.scope.channels || []) ids.add(c)
  return [...ids]
    .map((id) => ({ value: id, label: channelName(id), hint: id.startsWith('oauth:') ? `OAuth 账号 · ${id.split(':')[1]}` : 'API 渠道' }))
    .sort((a, b) => a.label.localeCompare(b.label))
})

// ---- model parameters (body override of the current translation model) ----
const paramModel = computed(() => d.value?.model || '')
const bodyText = ref('{}')
const bodyError = ref('')
function currentBody(): Record<string, unknown> {
  return (d.value?.modelOverrides?.[paramModel.value]?.body as Record<string, unknown>) || {}
}
function syncBodyText(): void {
  bodyText.value = JSON.stringify(currentBody(), null, 2)
  bodyError.value = ''
}
watch(
  () => [paramModel.value, JSON.stringify(currentBody())],
  () => {
    try {
      if (JSON.stringify(JSON.parse(bodyText.value)) === JSON.stringify(currentBody())) return
    } catch {
      /* fall through and overwrite */
    }
    syncBodyText()
  },
  { immediate: true },
)
function setBody(body: Record<string, unknown>): void {
  if (!d.value || !paramModel.value) return
  const next = { ...(d.value.modelOverrides || {}) }
  if (Object.keys(body).length) next[paramModel.value] = { body: body as never }
  else delete next[paramModel.value]
  d.value.modelOverrides = next
}
function onBodyInput(text: string): void {
  bodyText.value = text
  let parsed: unknown
  try {
    parsed = text.trim() ? JSON.parse(text) : {}
  } catch (e) {
    bodyError.value = `不是有效的 JSON：${(e as Error).message}`
    return
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    bodyError.value = '必须是一个 JSON 对象，例如 {}'
    return
  }
  const bad = Object.keys(parsed).filter((k) => k.startsWith('_parrot_'))
  if (bad.length) {
    bodyError.value = `不能使用以 _parrot_ 开头的字段：${bad.join('、')}`
    return
  }
  bodyError.value = ''
  setBody(parsed as Record<string, unknown>)
}
type Thinking = 'none' | 'high' | 'max'
const thinking = computed<Thinking>({
  get: () => {
    const b = currentBody()
    const t = b.thinking as { type?: string } | undefined
    if (t?.type === 'enabled' && (b.reasoning_effort === 'high' || b.reasoning_effort === 'max')) return b.reasoning_effort as Thinking
    return 'none'
  },
  set: (mode) => {
    const b = { ...currentBody() }
    if (mode === 'none') {
      delete b.thinking
      delete b.reasoning_effort
    } else {
      b.thinking = { type: 'enabled' }
      b.reasoning_effort = mode
    }
    setBody(b)
    syncBodyText()
  },
})
const otherOverrides = computed(() => Object.keys(d.value?.modelOverrides || {}).filter((m) => m !== paramModel.value))
function dropOverride(model: string): void {
  if (!d.value) return
  const next = { ...(d.value.modelOverrides || {}) }
  delete next[model]
  d.value.modelOverrides = next
}

// ---- prompt ----
const defaultPrompt = computed(() => res.data.value?.defaultPrompt || '')
/** Editing when the draft holds a custom prompt; otherwise the built-in default is shown read-only. */
const promptEditing = computed(() => Boolean(d.value && d.value.prompt))
function editFromDefault(): void {
  if (d.value) d.value.prompt = defaultPrompt.value
}
function savePrompt(): void {
  // Saving an untouched copy of the default stays on the default instead of freezing a custom copy.
  const same = d.value && defaultPrompt.value && d.value.prompt.trim() === defaultPrompt.value.trim()
  promptSec.save(same ? { prompt: null } : undefined)
}
async function resetPrompt(): Promise<void> {
  const ok = await confirmAction({
    title: '恢复默认翻译提示词？',
    detail: '自定义的提示词会被删除，改用 Parrot 内置的默认提示词。',
    confirmText: '恢复默认',
  })
  if (ok) await promptSec.save({ prompt: null })
}

// ---- test ----
/** Operation error: `UPSTREAM_ERROR: <reason>` (round 2) or a diagnostic sentence. */
function testFailure(op: Operation): string {
  const text = operationErrorText(op).trim()
  const m = /^UPSTREAM_ERROR:?\s*(.*)$/s.exec(text)
  if (!m) return text
  return m[1] || '翻译模型调用失败（上游没有给出原因）'
}
const testText = ref('')
const testing = ref(false)
const testResult = ref<{ ok: boolean; original?: string; translated?: string; cached?: boolean; message?: string } | null>(null)
async function runTest(): Promise<void> {
  const text = testText.value.trim()
  if (!text) return
  testing.value = true
  testResult.value = null
  try {
    const op = (await api.post<Operation>('/translation/actions/test', { text })).data
    const done = await waitOperation(op)
    if (done.status === 'succeeded') {
      const r = (done.result || {}) as { original?: string; translated?: string; cached?: boolean }
      testResult.value = { ok: true, ...r }
    } else {
      testResult.value = { ok: false, message: testFailure(done) }
    }
    loadCache()
  } catch (err) {
    testResult.value = { ok: false, message: `${errorTitle(err)}${errorDetail(err) ? `：${errorDetail(err)}` : ''}` }
  } finally {
    testing.value = false
  }
}

// ---- cache ----
const cache = ref<Cache | null>(null)
const cacheErr = ref('')
const clearing = ref(false)
async function loadCache(): Promise<void> {
  try {
    cache.value = (await api.get<Cache>('/translation/cache')).data
    cacheErr.value = ''
  } catch (err) {
    cacheErr.value = errorTitle(err)
  }
}
onMounted(loadCache)
const hitRate = computed(() => {
  const c = cache.value
  if (!c || !(c.hits + c.misses)) return '—'
  return `${((c.hits / (c.hits + c.misses)) * 100).toFixed(1)}%`
})
async function clearCache(): Promise<void> {
  const ok = await confirmAction({
    title: '清空翻译缓存？',
    detail: `当前缓存 ${integer(cache.value?.entries ?? 0)} 条记录。清空后，下次翻译会全部重新调用模型。`,
    danger: true,
    confirmText: '清空',
  })
  if (!ok) return
  clearing.value = true
  try {
    await api.del('/translation/cache')
    toast('翻译缓存已清空', 'success')
    await loadCache()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err))
  } finally {
    clearing.value = false
  }
}

const langName = computed(() => langs.value.find((l) => l.id === res.data.value?.targetLanguage)?.displayName || res.data.value?.targetLanguage || '')
</script>

<template>
  <Page title="翻译" subtitle="把用户输入先翻译成指定语言再发给模型；模型的回复不翻译（需要时在提示词里指定回复语言）。">
    <p v-if="res.error.value" class="err">翻译设置加载失败：{{ errorTitle(res.error.value) }}</p>

    <div class="cols">
      <div class="col">
        <SettingsSection
          title="翻译层"
          description="开启后，命中作用范围的请求会先经过翻译模型。"
          :loading="loading"
          :dirty="main.dirty.value"
          :saving="main.saving.value"
          @save="main.save()"
          @reset="main.reset()"
        >
          <template v-if="d">
            <Field inline label="启用翻译层" hint="开启前需要先设置翻译模型">
              <Switch v-model="d.enabled" />
            </Field>
            <Field inline label="翻译模型" hint="推荐轻量、快速的模型">
              <div class="pick">
                <Select v-model="d.model" width="220px" placeholder="选择模型" :options="modelOptions" />
                <Btn size="sm" variant="ghost" icon title="查看模型" :disabled="!d.model" @click="openModel(d.model)"><ExternalLink /></Btn>
              </div>
            </Field>
            <Field inline label="备用模型" hint="翻译模型失败时改用它">
              <div class="pick">
                <Select v-model="d.fallbackModel" width="220px" :options="fallbackOptions" />
                <Btn size="sm" variant="ghost" icon title="查看模型" :disabled="!d.fallbackModel" @click="openModel(d.fallbackModel)"><ExternalLink /></Btn>
              </div>
            </Field>
            <Field inline label="目标语言" hint="用户输入会被翻译成这种语言">
              <Select v-model="d.targetLanguage" width="220px" :options="langOptions" />
            </Field>
            <Field inline label="翻译系统消息" hint="同时翻译 system 提示词">
              <Switch v-model="d.translateSystemMessages" />
            </Field>
          </template>
        </SettingsSection>

        <SettingsSection
          title="数值设置"
          :loading="loading"
          :dirty="nums.dirty.value"
          :saving="nums.saving.value"
          @save="nums.save()"
          @reset="nums.reset()"
        >
          <template v-if="d">
            <Field inline label="超时" hint="单次翻译最长等待（1–60 秒）">
              <NumberInput v-model="d.timeoutSeconds" :min="1" :max="60" integer unit="秒" />
            </Field>
            <Field inline label="最多翻译历史消息" hint="每个请求最多翻译最近多少条消息（1–200）">
              <NumberInput v-model="d.maxHistoryMessages" :min="1" :max="200" integer unit="条" />
            </Field>
            <Field inline label="缓存保留" hint="翻译结果缓存多久（1–30 天）">
              <NumberInput v-model="d.cacheTtlDays" :min="1" :max="30" integer unit="天" />
            </Field>
            <Field inline label="启动预加载" hint="启动时把最近多少条缓存读入内存（0–1000）">
              <NumberInput v-model="d.cachePreloadCount" :min="0" :max="1000" integer unit="条" />
            </Field>
            <Field inline label="连续失败告警" hint="连续失败多少次后发送告警，0 表示不告警（0–100）">
              <NumberInput v-model="d.failureAlertThreshold" :min="0" :max="100" integer unit="次" />
            </Field>
            <Field inline label="内存缓存上限" hint="0 表示不使用内存缓存（0–1024 MB）">
              <NumberInput v-model="d.memoryCacheMaxMb" :min="0" :max="1024" integer unit="MB" />
            </Field>
            <Field inline label="内存缓存有效期" hint="0 表示不过期（0–86400 秒）">
              <NumberInput v-model="d.memoryCacheTtlSeconds" :min="0" :max="86400" integer unit="秒" />
            </Field>
          </template>
        </SettingsSection>

        <SettingsSection
          title="作用范围"
          description="不选择任何项 = 对全部生效；勾选后只对选中的模型、渠道 / 账号启用翻译。两者同时设置时需要都命中。"
          :loading="loading"
          :dirty="scope.dirty.value"
          :saving="scope.saving.value"
          @save="scope.save()"
          @reset="scope.reset()"
        >
          <template v-if="d">
            <div class="scope">
              <div>
                <p class="sl">
                  生效模型
                  <Badge :tone="d.scope.models.length ? 'blue' : 'neutral'">{{ d.scope.models.length ? `仅 ${d.scope.models.length} 个` : '全部模型' }}</Badge>
                </p>
                <CheckList v-model="d.scope.models" :options="scopeModelOptions" max-height="240px" empty-text="没有可选模型" />
                <div v-if="d.scope.models.length" class="tags">
                  <button v-for="m in d.scope.models" :key="m" type="button" class="tag mono" @click="openModel(m)">{{ m }}</button>
                </div>
              </div>
              <div>
                <p class="sl">
                  生效渠道 / 账号
                  <Badge :tone="d.scope.channels.length ? 'blue' : 'neutral'">{{ d.scope.channels.length ? `仅 ${d.scope.channels.length} 个` : '全部渠道' }}</Badge>
                </p>
                <CheckList v-model="d.scope.channels" :options="scopeChannelOptions" max-height="240px" empty-text="没有可选渠道" />
                <div v-if="d.scope.channels.length" class="tags">
                  <button v-for="c in d.scope.channels" :key="c" type="button" class="tag" :title="c" @click="openChannel(c)">{{ channelName(c) }}</button>
                </div>
              </div>
            </div>
          </template>
        </SettingsSection>
      </div>

      <div class="col">
        <Card title="测试翻译" subtitle="只测试翻译层效果，不会发送给业务模型">
          <template #icon><FlaskConical /></template>
          <TextArea v-model="testText" :rows="4" placeholder="输入一段要翻译的文字" />
          <div class="row">
            <span class="faint small">将翻译为 {{ langName || '目标语言' }}，使用 {{ res.data.value?.model || '（未设置翻译模型）' }}</span>
            <Btn variant="primary" :loading="testing" :disabled="!testText.trim()" @click="runTest"><Languages /> 翻译</Btn>
          </div>
          <div v-if="testResult" class="result" :class="testResult.ok ? 'ok' : 'bad'">
            <template v-if="testResult.ok">
              <p class="rk">译文 <Badge v-if="testResult.cached" tone="teal">命中缓存</Badge></p>
              <p class="rv">{{ testResult.translated }}</p>
              <p class="rk">原文</p>
              <p class="rv faint">{{ testResult.original }}</p>
            </template>
            <p v-else class="rt"><TriangleAlert /> 翻译测试失败：{{ testResult.message }}</p>
          </div>
        </Card>

        <Card title="翻译缓存">
          <template #icon><Database /></template>
          <template #actions>
            <Btn variant="danger" size="sm" :loading="clearing" :disabled="!cache" @click="clearCache"><Trash2 /> 清空缓存</Btn>
          </template>
          <p v-if="cacheErr" class="faint">{{ cacheErr }}</p>
          <div v-else-if="!cache" class="skl"><Skeleton h="40px" r="8px" /></div>
          <dl v-else class="cache">
            <div><dt>缓存条目</dt><dd>{{ integer(cache.entries) }}</dd></div>
            <div><dt>内存中</dt><dd>{{ integer(cache.memoryEntries) }} 条 · {{ (cache.memoryBytes / 1024 / 1024).toFixed(2) }} MB</dd></div>
            <div><dt>命中 / 未命中</dt><dd>{{ integer(cache.hits) }} / {{ integer(cache.misses) }}</dd></div>
            <div><dt>命中率</dt><dd>{{ hitRate }}</dd></div>
          </dl>
        </Card>

        <SettingsSection
          title="翻译模型参数"
          description="翻译层调用翻译模型时，附加到上游请求 body 的参数；不会影响业务模型的请求。"
          :loading="loading"
          :dirty="overrides.dirty.value"
          :saving="overrides.saving.value"
          @save="overrides.save()"
          @reset="overrides.reset(); syncBodyText()"
        >
          <template v-if="d">
            <p v-if="!paramModel" class="faint small">请先设置翻译模型。</p>
            <template v-else>
              <p class="cur">当前翻译模型 <button type="button" class="lk mono" @click="openModel(paramModel)">{{ paramModel }}</button></p>
              <Field label="思考（thinking）" hint="high / max 会写入 thinking.type=enabled 和 reasoning_effort；「不设置」只移除这两个字段">
                <Segmented
                  v-model="thinking"
                  :options="[
                    { value: 'none', label: '不设置' },
                    { value: 'high', label: 'high' },
                    { value: 'max', label: 'max' },
                  ]"
                />
              </Field>
              <Field label="body 参数（JSON 对象）" :error="bodyError" hint="例如 {&quot;temperature&quot;: 0}；留空或 {} 表示不附加参数" class="gap">
                <TextArea :model-value="bodyText" :rows="6" mono :invalid="Boolean(bodyError)" @update:model-value="onBodyInput" />
              </Field>
              <div v-if="otherOverrides.length" class="others">
                <p class="sl">其他模型的参数</p>
                <div class="tags">
                  <span v-for="m in otherOverrides" :key="m" class="tag2">
                    <button type="button" class="mono" @click="openModel(m)">{{ m }}</button>
                    <button type="button" class="x" title="删除这个模型的参数" @click="dropOverride(m)">删除</button>
                  </span>
                </div>
              </div>
            </template>
          </template>
        </SettingsSection>

        <SettingsSection
          title="翻译提示词"
          description="提示词里的 {target_language} 会被替换为目标语言。"
          :loading="loading"
          :dirty="promptSec.dirty.value"
          :saving="promptSec.saving.value"
          @save="savePrompt"
          @reset="promptSec.reset()"
        >
          <template #actions>
            <Badge :tone="res.data.value?.prompt ? 'blue' : 'neutral'">{{ res.data.value?.prompt ? '自定义' : '内置默认' }}</Badge>
            <Btn v-if="res.data.value?.prompt" size="sm" variant="ghost" @click="resetPrompt"><RotateCcw /> 恢复默认</Btn>
          </template>
          <template v-if="d">
            <template v-if="promptEditing">
              <TextArea v-model="d.prompt" :rows="10" placeholder="支持 {target_language} 占位符。" />
              <p v-if="!res.data.value?.prompt" class="faint small hint2">正在基于内置默认提示词修改，保存后改为使用自定义提示词。</p>
            </template>
            <template v-else>
              <pre class="dp">{{ defaultPrompt || '（服务端没有返回内置默认提示词）' }}</pre>
              <div class="row">
                <span class="faint small">当前使用内置默认提示词</span>
                <Btn size="sm" :disabled="!defaultPrompt" @click="editFromDefault"><Pencil /> 基于默认修改</Btn>
              </div>
            </template>
          </template>
        </SettingsSection>
      </div>
    </div>
  </Page>
</template>

<style scoped>
.err {
  padding: 10px 14px;
  border-radius: var(--radius-md);
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
  font-size: 12.5px;
}
.cols {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
@container (max-width: 1000px) {
  .cols {
    grid-template-columns: minmax(0, 1fr);
  }
}
.pick {
  display: flex;
  align-items: center;
  gap: 4px;
}
.scope {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
  padding-bottom: 6px;
}
@container (max-width: 760px) {
  .scope {
    grid-template-columns: minmax(0, 1fr);
  }
}
.sl {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 600;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
}
.tag {
  height: 22px;
  padding: 0 8px;
  border-radius: 6px;
  font-size: 11.5px;
  color: var(--accent);
  background: var(--accent-soft);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tag2 {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 4px 0 8px;
  border-radius: 6px;
  font-size: 11.5px;
  background: var(--fill-2);
}
.tag2 .x {
  font-size: 11px;
  color: var(--red);
  padding: 0 4px;
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 10px;
}
.small {
  font-size: 12px;
}
.result {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.result.ok {
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.result.bad {
  background: color-mix(in srgb, var(--red) 10%, transparent);
}
.rk {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-3);
}
.rk:not(:first-child) {
  margin-top: 6px;
}
.rv {
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.rt {
  display: flex;
  gap: 6px;
  color: var(--red);
  font-size: 12.5px;
}
.rt svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.skl {
  display: flex;
  flex-direction: column;
}
.cache {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 16px;
}
.cache dt {
  font-size: 11px;
  color: var(--text-3);
}
.cache dd {
  margin: 2px 0 0;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.cur {
  margin-bottom: 12px;
  font-size: 12.5px;
  color: var(--text-2);
}
.lk {
  color: var(--accent);
}
.lk:hover {
  text-decoration: underline;
}
.gap {
  margin-top: 14px;
}
.dp {
  margin: 0;
  padding: 10px 12px;
  max-height: 320px;
  overflow: auto;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
}
.hint2 {
  margin-top: 6px;
}
.others {
  margin-top: 14px;
  padding-bottom: 6px;
}
</style>
