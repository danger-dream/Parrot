<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { FlaskConical, Lightbulb, MousePointerClick, TriangleAlert, Undo2 } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { errorTitle } from '@/utils/errors'
import { confirmAction } from '@/stores/dialogs'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Switch from '@/components/ui/Switch.vue'
import TextArea from '@/components/ui/TextArea.vue'
import TextInput from '@/components/ui/TextInput.vue'
import ConditionGroup from './ConditionGroup.vue'
import StepList from './StepList.vue'
import ThresholdList from './ThresholdList.vue'
import TraceView from './TraceView.vue'
import {
  CATEGORY_LOOK,
  DEFAULT_MESSAGES,
  FINAL_OPTIONS,
  RESPONSE_STATUS_OPTIONS,
  TEMPLATE_VARS,
  categoryLabel,
  clone,
  describeStep,
  describeThreshold,
  filledConds,
  finalLabel,
  flattenJson,
  newCond,
  parseJsonBody,
  providerLabel,
  pruneTree,
  renderTemplate,
  treeSegments,
  type Category,
  type EditorSeed,
  type ErrorRulesData,
  type Final,
  type Group,
  type Phase,
  type RuleResponse,
  type Step,
  type TestResult,
  type Threshold,
} from './model'

const props = defineProps<{
  open: boolean
  data: ErrorRulesData
  seed: EditorSeed | null
  saving: boolean
  channelOptions: { value: string; label: string; hint?: string }[]
  channelLabel: (id: string) => string
}>()
const emit = defineEmits<{ close: []; save: [payload: Record<string, unknown>, id: string | null] }>()

interface Handling {
  steps: Step[]
  final: Final
  thresholds: Threshold[]
  response: RuleResponse
}

function handlingFor(category: Category): Handling {
  return clone(props.data.categories.find((c) => c.id === category)!.handling) as Handling
}

const form = reactive({
  name: '',
  enabled: true,
  note: '',
  when: { kind: 'group', mode: 'all', items: [] } as Group,
  category: 'quota_exhausted' as Category,
  steps: [] as Step[],
  final: 'next' as Final,
  thresholds: [] as Threshold[],
  response: { status: 0, message: '', retryAfter: false } as RuleResponse,
})
const editingId = ref<string | null>(null)
const previous = ref<{ category: Category; handling: Handling } | null>(null)
const attempted = ref(false)
const messageBox = ref<HTMLElement | null>(null)
const pathToInsert = ref('$.error.message')
const pristine = ref('')
const showAllFields = ref(false)

const sample = reactive({ provider: '', channelKey: '', model: '', status: null as number | null, phase: 'http' as Phase, body: '' })
const testResult = ref<TestResult | null>(null)
const testError = ref('')
const testing = ref(false)

function load(seed: EditorSeed | null): void {
  const r = seed?.rule || {}
  editingId.value = seed?.editingId ?? null
  form.name = r.name || ''
  form.enabled = r.enabled ?? true
  form.note = r.note || ''
  form.category = r.category || 'quota_exhausted'
  form.when = r.when ? clone(r.when) : { kind: 'group', mode: 'all', items: [newCond('status')] }
  if (!form.when.items.length) form.when.items.push(newCond('status'))
  const h = handlingFor(form.category)
  form.steps = clone(r.steps ?? h.steps)
  form.final = r.final ?? h.final
  form.thresholds = clone(r.thresholds ?? h.thresholds)
  form.response = clone(r.response ?? h.response)
  previous.value = null
  attempted.value = false
  showAllFields.value = false
  const s = seed?.sample
  sample.provider = s?.provider || ''
  sample.channelKey = s?.channelKey || ''
  sample.model = s?.model || ''
  sample.status = s?.status ?? null
  sample.phase = s?.phase || 'http'
  sample.body = s?.body || ''
  testResult.value = null
  testError.value = ''
  pristine.value = snapshot()
  if (sample.body) void nextTick(runTest)
}

function snapshot(): string {
  return JSON.stringify([form.name, form.enabled, form.note, pruneTree(form.when), form.category, form.steps,
    form.final, form.thresholds, form.response])
}

async function requestClose(): Promise<void> {
  if (!props.saving && snapshot() !== pristine.value) {
    const ok = await confirmAction({
      title: '放弃未保存的修改？',
      detail: '这条规则的修改还没有保存，关闭后会丢失。',
      confirmText: '放弃修改',
      cancelText: '继续编辑',
      danger: true,
    })
    if (!ok) return
  }
  emit('close')
}

watch(
  () => [props.open, props.seed] as const,
  ([open]) => {
    if (open) load(props.seed)
  },
  { immediate: true },
)

const labels = computed(() => ({ provider: providerLabel, channel: props.channelLabel }))
const hasCondition = computed(() => filledConds(form.when) > 0)
const catLabel = computed(() => categoryLabel(props.data.categories, form.category))

function shortText(text: string, max = 24): string {
  if (text.length <= max) return text
  const cut = text.slice(0, max)
  const space = cut.lastIndexOf(' ')
  return `${space > max / 2 ? cut.slice(0, space) : cut}…`
}

function firstValue(field: string): string {
  for (const item of form.when.items) if (item.kind === 'cond' && item.field === field && item.values?.length) return item.values[0]
  return ''
}

const suggestedName = computed(() => {
  const provider = firstValue('provider')
  const what = firstValue('code') || (firstValue('status') && `HTTP ${firstValue('status')}`) || shortText(firstValue('message'))
  const head = [provider && providerLabel(provider), what].filter(Boolean).join(' ')
  return head ? `${head} · ${catLabel.value}` : catLabel.value
})

const payload = computed(() => ({
  name: form.name.trim() || suggestedName.value,
  enabled: form.enabled,
  note: form.note.trim(),
  when: pruneTree(form.when),
  category: form.category,
  steps: form.steps,
  final: form.final,
  thresholds: form.thresholds,
  response: form.response,
}))

const sampleData = computed(() => (sample.body ? parseJsonBody(sample.body) : null))
const sampleFields = computed(() => flattenJson(sampleData.value))

const warnings = computed(() => {
  const out: string[] = []
  if (!hasCondition.value) out.push('还没有填好任何条件。至少要有一个，否则会命中所有错误。')
  const types = form.steps.map((s) => s.type)
  if ((types.includes('disable') || form.thresholds.some((t) => t.steps.some((s) => s.type === 'disable')))
    && !JSON.stringify(form.when).includes('"channelType"'))
    out.push('「停用账号」对 API 渠道会停用整个渠道。只想停用 OAuth 账号，可以加一个条件「账号类型 等于 OAuth 账号」。')
  const pauseAt = types.indexOf('pause')
  if (pauseAt >= 0 && types.indexOf('retry', pauseAt) > pauseAt) out.push('暂停之后又在同一渠道重试：重试会照常发出，暂停只影响之后的新请求。')
  if (form.final === 'respond' && types.includes('pause')) out.push('选了「直接返回给客户端」，同时还会暂停这个渠道：之后的请求会改用其他渠道。')
  return out
})

function pickCategory(id: Category): void {
  if (id === form.category) return
  previous.value = { category: form.category, handling: clone({ steps: form.steps, final: form.final, thresholds: form.thresholds, response: form.response }) }
  form.category = id
  Object.assign(form, handlingFor(id))
}

function undoCategory(): void {
  if (!previous.value) return
  form.category = previous.value.category
  Object.assign(form, previous.value.handling)
  previous.value = null
}

function resetHandling(): void {
  Object.assign(form, handlingFor(form.category))
  previous.value = null
}

function addFieldCondition(path: string, value: string): void {
  form.when.items.push({ kind: 'cond', field: 'path', op: 'eq', path, values: [value] })
}

function insertToken(token: string): void {
  const el = messageBox.value?.querySelector('textarea')
  const text = form.response.message || ''
  if (!el) {
    form.response.message = text + token
    return
  }
  const start = el.selectionStart ?? text.length
  const end = el.selectionEnd ?? text.length
  form.response.message = text.slice(0, start) + token + text.slice(end)
  void nextTick(() => {
    el.focus()
    el.setSelectionRange(start + token.length, start + token.length)
  })
}

const statusOptions = RESPONSE_STATUS_OPTIONS.map((o) => ({ value: o.value, label: o.label }))
const finalOptions = FINAL_OPTIONS.map((o) => ({ value: o.value, label: o.label }))
const responsePreview = computed(() => renderTemplate(form.response.message, form.category, sampleData.value))
const sampleProviderOptions = computed(() => [
  { value: '', label: '按渠道自动识别' },
  ...props.data.providers.map((p) => ({ value: p.id, label: providerLabel(p.id) })),
])
const sampleChannelOptions = computed(() => [{ value: '', label: '不指定渠道' }, ...props.channelOptions])

let timer: number | undefined
watch(
  () => [payload.value, sample.body, sample.status, sample.provider, sample.channelKey, sample.model, sample.phase],
  () => {
    if (!props.open || !sample.body.trim()) return
    window.clearTimeout(timer)
    timer = window.setTimeout(runTest, 500)
  },
  { deep: true },
)

async function runTest(): Promise<void> {
  if (!sample.body.trim() && !sample.status) return
  if (!hasCondition.value) {
    testResult.value = null
    testError.value = '先填好至少一个条件'
    return
  }
  testing.value = true
  try {
    const res = await api.post<TestResult>('/error-rules/test', {
      channelKey: sample.channelKey,
      provider: sample.provider,
      model: sample.model.trim(),
      phase: sample.phase,
      status: sample.status || null,
      body: sample.body,
      draftRule: payload.value as Schemas['ErrorRuleInput'],
    } satisfies Schemas['ErrorRulesTestRequest'])
    testResult.value = res.data
    testError.value = ''
  } catch (err) {
    testError.value = errorTitle(err)
  } finally {
    testing.value = false
  }
}

const draftHit = computed(() => testResult.value?.trace.find((x) => x.layer === 'draft') || null)

function save(): void {
  attempted.value = true
  if (!hasCondition.value) return
  emit('save', payload.value, editingId.value)
}
</script>

<template>
  <Modal
    :open="open"
    :title="editingId ? '编辑规则' : '新建规则'"
    :subtitle="seed?.origin || '告诉 Parrot：遇到什么样的错误、它是什么问题、按什么步骤处理。'"
    width="xl"
    @close="requestClose"
  >
    <div class="editor">
      <div class="form">
        <div class="name-row">
          <Field label="规则名称" class="grow">
            <TextInput v-model="form.name" :placeholder="suggestedName" />
          </Field>
          <Field label="启用" inline>
            <Switch v-model="form.enabled" label="启用这条规则" />
          </Field>
        </div>

        <!-- 1 -->
        <section class="step">
          <header class="step-head">
            <span class="num">1</span>
            <div>
              <h4>什么样的错误</h4>
              <p>每个条件选一个字段、怎么比、和什么比；也可以用 JSON 路径取上游返回里的任意字段。需要「或」的时候添加条件组。</p>
            </div>
          </header>
          <ConditionGroup
            :group="form.when"
            :providers="data.providers"
            :channel-options="channelOptions"
            :channel-label="channelLabel"
          />
          <div v-if="sampleFields.length" class="picker">
            <p class="picker-title"><MousePointerClick class="p-ico" /> 从右侧的错误内容里点一个字段，直接加成条件</p>
            <div class="fields">
              <button
                v-for="f in showAllFields ? sampleFields : sampleFields.slice(0, 10)"
                :key="f.path"
                type="button"
                class="fieldchip"
                :title="`添加条件：${f.path} 等于 ${f.value}`"
                @click="addFieldCondition(f.path, f.value)"
              >
                <span class="fp">{{ f.path }}</span>
                <span class="fv">{{ f.value.length > 28 ? f.value.slice(0, 28) + '…' : f.value }}</span>
              </button>
              <button v-if="sampleFields.length > 10" type="button" class="link" @click="showAllFields = !showAllFields">
                {{ showAllFields ? '收起' : `全部 ${sampleFields.length} 个字段` }}
              </button>
            </div>
          </div>
        </section>

        <!-- 2 -->
        <section class="step">
          <header class="step-head">
            <span class="num">2</span>
            <div>
              <h4>这是什么错误</h4>
              <p>选好后会填入推荐的处理步骤，下一步可以再改。类型也用于命中统计和默认的提示文案。</p>
            </div>
          </header>
          <p v-if="seed?.guessed && !editingId" class="guess"><Lightbulb class="g-ico" /> 根据错误内容推测是「{{ catLabel }}」，请确认。</p>
          <div class="cats" role="radiogroup" aria-label="错误类型">
            <button
              v-for="c in data.categories"
              :key="c.id"
              type="button"
              role="radio"
              :aria-checked="form.category === c.id"
              class="cat"
              :class="{ on: form.category === c.id }"
              :style="{ '--c': CATEGORY_LOOK[c.id].color }"
              @click="pickCategory(c.id)"
            >
              <component :is="CATEGORY_LOOK[c.id].icon" class="cat-ico" />
              <span class="cat-label">{{ c.label }}</span>
              <span class="cat-desc">{{ c.description }}</span>
            </button>
          </div>
          <p v-if="previous" class="swap">
            已换成「{{ catLabel }}」的推荐处理步骤。
            <button type="button" class="link" @click="undoCategory"><Undo2 class="l-ico" /> 恢复刚才的设置</button>
          </p>
        </section>

        <!-- 3 -->
        <section class="step">
          <header class="step-head">
            <span class="num">3</span>
            <div>
              <h4>怎么处理</h4>
              <p>每次遇到这个错误，从上往下执行这些步骤，最后决定这次请求往哪走。</p>
            </div>
            <button type="button" class="link reset" @click="resetHandling">恢复推荐设置</button>
          </header>

          <p class="sub-title">每次遇到时</p>
          <StepList :steps="form.steps" empty-text="不做额外的事，直接走「最后」的去向。" />
          <div class="final">
            <span class="final-label">最后</span>
            <Segmented v-model="form.final" :options="finalOptions" size="sm" />
          </div>
          <p class="final-hint">{{ FINAL_OPTIONS.find((o) => o.value === form.final)?.hint }}</p>

          <p class="sub-title">累计到一定次数时 <small>可选。比如同一个模型连续 3 次 429，就暂停它 30 分钟并发通知</small></p>
          <ThresholdList :thresholds="form.thresholds" />
        </section>

        <!-- 4 -->
        <section v-if="form.final !== 'pass'" class="step">
          <header class="step-head">
            <span class="num">4</span>
            <div>
              <h4>客户端会看到什么</h4>
              <p>{{ form.final === 'respond' ? '这条规则直接结束请求时，客户端收到这里设定的回复。' : '所有渠道都失败、或者都在暂停中时，客户端收到这里设定的回复。' }}</p>
            </div>
          </header>
          <div class="row">
            <div class="row-label">
              <span>返回状态码</span>
              <small>{{ RESPONSE_STATUS_OPTIONS.find((o) => o.value === form.response.status)?.hint }}</small>
            </div>
            <Select v-model="form.response.status" :options="statusOptions" width="200px" />
          </div>
          <template v-if="form.response.status">
            <div class="row">
              <div class="row-label">
                <span>附带 Retry-After</span>
                <small>告诉客户端多久以后再试，按暂停结束的时间计算</small>
              </div>
              <Switch v-model="form.response.retryAfter" label="附带 Retry-After" />
            </div>
            <Field label="提示文案" hint="留空用默认文案。点下面的变量插入到光标处">
              <div ref="messageBox">
                <TextArea v-model="form.response.message" :rows="2" :placeholder="DEFAULT_MESSAGES[form.category] || '{upstream_message}'" />
              </div>
              <div class="vars">
                <button v-for="v in TEMPLATE_VARS" :key="v.key" type="button" class="var" :title="`例：${v.sample}`" @click="insertToken(`{${v.key}}`)">
                  {{ v.label }} <code>{{ '{' + v.key + '}' }}</code>
                </button>
              </div>
              <div class="pathvar">
                <span>引用上游返回的字段</span>
                <TextInput v-model="pathToInsert" mono width="200px" placeholder="$.error.message" />
                <Btn size="sm" :disabled="!/^\$./.test(pathToInsert)" @click="insertToken(`{${pathToInsert.trim()}}`)">插入</Btn>
              </div>
            </Field>
            <p class="reply-cap">{{ sampleData ? '用右侧的错误内容预览' : '示例（模型和时间是示意值，实际以「试一下」为准）' }}</p>
            <div class="reply mono">
              <span class="reply-status">HTTP {{ form.response.status }}</span>
              <span v-if="form.response.retryAfter" class="reply-hdr">Retry-After: …</span>
              <p class="reply-msg">{{ responsePreview }}</p>
            </div>
          </template>
        </section>

        <Field label="备注" hint="只给自己看，比如为什么加这条规则">
          <TextInput v-model="form.note" placeholder="可不填" />
        </Field>
      </div>

      <aside class="side">
        <div class="side-inner">
          <p class="side-title">这条规则会……</p>
          <div class="summary">
            <p class="s-when">
              <span class="s-k">当</span>
              <template v-for="(s, i) in treeSegments(pruneTree(form.when), labels)" :key="i"><b v-if="s.strong">{{ s.text }}</b><span v-else :class="{ muted: s.muted }">{{ s.text }}</span></template>
            </p>
            <p class="s-cat" :style="{ '--c': CATEGORY_LOOK[form.category].color }">
              <component :is="CATEGORY_LOOK[form.category].icon" class="s-ico" />
              判定为「{{ catLabel }}」
            </p>
            <ol class="s-steps">
              <li v-for="(s, i) in form.steps" :key="i">{{ describeStep(s) }}</li>
              <li class="s-final">最后：{{ finalLabel(form.final) }}</li>
            </ol>
            <p v-for="(t, i) in form.thresholds" :key="`t${i}`" class="s-th">累计：{{ describeThreshold(t) }}</p>
            <p v-if="form.final !== 'pass' && form.response.status" class="s-reply">客户端收到 {{ form.response.status }}{{ form.response.retryAfter ? ' + Retry-After' : '' }}</p>
          </div>
          <ul v-if="warnings.length" class="warns" :class="{ loud: attempted && !hasCondition }">
            <li v-for="w in warnings" :key="w"><TriangleAlert class="w-ico" /> {{ w }}</li>
          </ul>

          <div class="try">
            <p class="side-title"><FlaskConical class="side-ico" /> 用一段错误试一下</p>
            <div class="try-grid">
              <Select v-model="sample.provider" :options="sampleProviderOptions" width="100%" />
              <NumberInput v-model="sample.status" :min="100" :max="599" nullable integer placeholder="状态码" width="100%" />
            </div>
            <Select v-model="sample.channelKey" :options="sampleChannelOptions" searchable width="100%" />
            <TextInput v-model="sample.model" mono placeholder="模型（可不填）" />
            <TextArea v-model="sample.body" :rows="5" mono placeholder="粘贴上游返回的错误内容，左边会列出可点选的字段" />
            <Btn size="sm" :loading="testing" :disabled="!sample.body.trim() && !sample.status" @click="runTest">试一下</Btn>
            <p v-if="testError" class="try-err">{{ testError }}</p>
            <template v-if="testResult">
              <p class="try-head" :class="draftHit?.status">
                <template v-if="draftHit?.status === 'matched'">✓ 这条规则会命中这段错误</template>
                <template v-else-if="draftHit">✗ 不会命中：{{ draftHit.reason }}</template>
              </p>
              <TraceView :result="testResult" :categories="data.categories" compact />
            </template>
          </div>
        </div>
      </aside>
    </div>

    <template #footer>
      <span v-if="attempted && !hasCondition" class="foot-err">请先填好至少一个条件</span>
      <Btn @click="requestClose">取消</Btn>
      <Btn variant="primary" :loading="saving" @click="save">{{ editingId ? '保存修改' : '创建规则' }}</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.editor {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 22px;
  align-items: start;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}
.name-row {
  display: flex;
  align-items: flex-end;
  gap: 16px;
}
.name-row .grow {
  flex: 1;
}
.name-row :deep(.field.inline) {
  border-bottom: 0;
  padding: 0 0 6px;
}
.step {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px 16px 16px;
  border-radius: 14px;
  border: 0.5px solid var(--hairline);
  background: var(--surface-raised, transparent);
}
.step-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.step-head h4 {
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.step-head p {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.5;
}
.num {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-inverse, #fff);
  background: var(--accent, var(--blue));
}
.reset {
  margin-left: auto;
  flex-shrink: 0;
}
.picker {
  padding: 10px 12px;
  border-radius: 10px;
  background: var(--fill-3, var(--fill-2));
}
.picker-title {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-2);
  margin-bottom: 6px;
}
.p-ico {
  width: 13px;
  height: 13px;
  color: var(--accent, var(--blue));
}
.fields {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
.fieldchip {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  max-width: 100%;
  padding: 3px 8px;
  border-radius: 7px;
  border: 0.5px solid var(--border);
  background: var(--surface-solid, var(--surface));
  cursor: pointer;
  font-size: 11.5px;
}
.fieldchip:hover {
  border-color: var(--accent, var(--blue));
}
.fp {
  font-family: var(--font-mono);
  color: var(--accent, var(--blue));
}
.fv {
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.guess {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-orange, var(--orange));
}
.g-ico {
  width: 14px;
  height: 14px;
}
.cats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}
.cat {
  --c: var(--blue);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: 10px 11px;
  border-radius: 11px;
  border: 0.5px solid var(--border);
  text-align: left;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
}
.cat:hover {
  background: var(--fill-2);
}
.cat.on {
  border-color: var(--c);
  background: color-mix(in srgb, var(--c) 12%, transparent);
  box-shadow: 0 0 0 0.5px var(--c) inset;
}
.cat-ico {
  width: 16px;
  height: 16px;
  color: var(--c);
}
.cat-label {
  margin-top: 2px;
  font-size: 12.5px;
  font-weight: 650;
}
.cat-desc {
  font-size: 11px;
  line-height: 1.45;
  color: var(--text-3);
}
.swap {
  font-size: 12px;
  color: var(--text-2);
}
.link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  color: var(--accent, var(--blue));
  cursor: pointer;
}
.l-ico {
  width: 12px;
  height: 12px;
}
.sub-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text-2);
}
.sub-title small {
  margin-left: 6px;
  font-weight: 400;
  color: var(--text-3);
}
.final {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 11px;
  background: var(--fill-2);
}
.final-label {
  font-size: 12.5px;
  font-weight: 650;
}
.final-hint {
  margin-top: -6px;
  font-size: 11.5px;
  color: var(--text-3);
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.row-label {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.row-label span {
  font-size: 13px;
  font-weight: 550;
}
.row-label small {
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.45;
}
.vars {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.var {
  font-size: 11.5px;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--fill-2);
  color: var(--text-2);
  cursor: pointer;
}
.var:hover {
  background: var(--fill);
  color: var(--text);
}
.var code {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-3);
}
.pathvar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-2);
}
.reply-cap {
  margin-bottom: -6px;
  font-size: 11.5px;
  color: var(--text-3);
}
.reply {
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--surface-sunken, var(--fill-2));
  border: 0.5px solid var(--hairline);
  font-size: 11.5px;
}
.reply-status {
  font-weight: 650;
}
.reply-hdr {
  margin-left: 10px;
  color: var(--text-2);
}
.reply-msg {
  margin-top: 4px;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
}
.side {
  position: sticky;
  top: 0;
  min-width: 0;
  max-height: calc(min(86dvh, 860px) - 150px);
  overflow-y: auto;
  overscroll-behavior: contain;
  margin-right: -10px;
  padding-right: 10px;
}
.side-inner {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.side-title {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  font-weight: 650;
  color: var(--text-3);
  letter-spacing: 0.02em;
}
.side-ico {
  width: 13px;
  height: 13px;
}
.summary {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 13px;
  border-radius: 12px;
  background: var(--fill-2);
  font-size: 12.5px;
  line-height: 1.6;
}
.s-when {
  color: var(--text-2);
  word-break: break-word;
}
.s-k {
  margin-right: 4px;
}
.s-when b {
  color: var(--text);
  font-weight: 600;
}
.s-when .muted {
  color: var(--text-3);
}
.s-cat {
  --c: var(--blue);
  display: flex;
  align-items: center;
  gap: 5px;
  font-weight: 650;
  color: color-mix(in srgb, var(--c) 70%, var(--text));
}
.s-ico {
  width: 14px;
  height: 14px;
}
.s-steps {
  margin: 0;
  padding-left: 18px;
  color: var(--text-2);
}
.s-steps li::marker {
  color: var(--text-3);
}
.s-final {
  color: var(--text);
  font-weight: 550;
  list-style: none;
  margin-left: -18px;
}
.s-th,
.s-reply {
  color: var(--text-2);
}
.warns {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.warns li {
  display: flex;
  gap: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-orange, var(--orange));
}
.warns.loud li:first-child {
  color: var(--text-red, var(--red));
  font-weight: 600;
}
.w-ico {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
  margin-top: 3px;
}
.try {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 12px;
  border-top: 0.5px solid var(--hairline);
}
.try-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 96px;
  gap: 6px;
}
.try :deep(.btn) {
  align-self: flex-start;
}
.try-err {
  font-size: 12px;
  color: var(--text-red, var(--red));
}
.try-head {
  font-size: 12.5px;
  font-weight: 650;
}
.try-head.matched {
  color: var(--text-green, var(--green));
}
.try-head.missed,
.try-head.skipped {
  color: var(--text-2);
}
.foot-err {
  margin-right: auto;
  align-self: center;
  font-size: 12.5px;
  color: var(--text-red, var(--red));
}
@media (max-width: 900px) {
  .editor {
    grid-template-columns: 1fr;
  }
  .side {
    position: static;
    max-height: none;
    overflow: visible;
  }
  .cats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 560px) {
  .row {
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
  }
  .final {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
