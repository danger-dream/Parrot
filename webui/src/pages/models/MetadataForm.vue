<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { RotateCcw, Search, Unlink } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Badge from '@/components/ui/Badge.vue'
import Switch from '@/components/ui/Switch.vue'
import TextInput from '@/components/ui/TextInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import CatalogPicker from './CatalogPicker.vue'
import {
  META_FIELDS,
  META_GROUP_LABEL,
  formatMeta,
  isManualSource,
  matchTarget,
  metaSourceLabel,
  metaValue,
  overrideKeys,
  valueSourceLabel,
  type MetaField,
  type ModelMetadata,
} from './shared'

// Inline editor (inside the model detail) for the sparse metadata override layer of one
// model: the common layer (scope=global) or one source (scope=oauth/api). Also corrects
// the catalog binding.

export type MetaScope = { type: 'global' } | { type: 'oauth' | 'api'; id: string; outboundModel: string; label: string }

const props = defineProps<{ modelId: string; scope: MetaScope }>()
const emit = defineEmits<{ close: []; changed: [] }>()

type CatalogData = Schemas['CatalogData']
interface Draft {
  on: boolean
  text: string
  bool: 'yes' | 'no'
}

const record = ref<ModelMetadata | null>(null)
const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const busy = ref('')
const errors = reactive<Record<string, string>>({})
const draft = reactive<Record<string, Draft>>({})
const initial = ref<Record<string, Draft>>({})
const catalogOpen = ref(false)

const scopeQuery = computed(() => (props.scope.type === 'global' ? {} : { scopeId: props.scope.id }))
const layer = computed<Record<string, unknown>>(() => {
  const r = record.value
  if (!r) return {}
  return (props.scope.type === 'global' ? r.commonOverride : r.sourceOverride) || {}
})

function toText(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (Array.isArray(v)) return v.join(', ')
  return String(v)
}

function resetDraft(): void {
  const keys = new Set(overrideKeys(layer.value))
  const eff = record.value?.effective
  const next: Record<string, Draft> = {}
  for (const f of META_FIELDS) {
    const on = keys.has(f.key)
    const v = on ? metaValue(layer.value, f.key) : metaValue(eff, f.key)
    next[f.key] = { on, text: f.kind === 'bool' ? '' : toText(v), bool: v ? 'yes' : 'no' }
  }
  initial.value = JSON.parse(JSON.stringify(next))
  for (const k of Object.keys(draft)) delete draft[k]
  Object.assign(draft, JSON.parse(JSON.stringify(next)))
  for (const k of Object.keys(errors)) delete errors[k]
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    record.value = (await api.get<ModelMetadata>(`/model-metadata/${enc(props.modelId)}`, scopeQuery.value)).data
    resetDraft()
  } catch (err) {
    record.value = null
    loadError.value =
      err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND' ? '这个模型或来源目前没有元数据记录，可能来源已移除。' : errorTitle(err)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelId, props.scope.type, props.scope.type === 'global' ? '' : props.scope.id],
  () => load(),
  { immediate: true },
)

function parseTokens(s: string): number | null {
  const m = s.trim().replace(/,/g, '').match(/^(\d+(?:\.\d+)?)\s*([kKmM]?)$/)
  if (!m) return null
  const n = Number(m[1]) * (m[2].toLowerCase() === 'k' ? 1000 : m[2].toLowerCase() === 'm' ? 1_000_000 : 1)
  return Number.isInteger(n) && n >= 1 && n <= 2_147_483_647 ? n : null
}

function parse(field: MetaField, d: Draft): { ok: true; value: unknown } | { ok: false; error: string } {
  switch (field.kind) {
    case 'tokens': {
      const n = parseTokens(d.text)
      return n === null ? { ok: false, error: '请输入正整数，可写 300k、1M' } : { ok: true, value: n }
    }
    case 'bool':
      return { ok: true, value: d.bool === 'yes' }
    case 'list': {
      const arr = d.text
        .split(/[,，\s]+/)
        .map((x) => x.trim())
        .filter(Boolean)
      if (arr.length > 20 || arr.some((x) => x.length > 80)) return { ok: false, error: '最多 20 项，每项不超过 80 个字符' }
      return { ok: true, value: arr }
    }
    case 'date':
      return /^\d{4}-\d{2}(-\d{2})?$/.test(d.text.trim()) ? { ok: true, value: d.text.trim() } : { ok: false, error: '格式为 YYYY-MM 或 YYYY-MM-DD' }
    case 'price': {
      const s = d.text.trim()
      const n = Number(s)
      return s !== '' && Number.isFinite(n) && n >= 0 ? { ok: true, value: n } : { ok: false, error: '请输入不小于 0 的数字，0 表示免费' }
    }
  }
}

const changedKeys = computed(() =>
  META_FIELDS.filter((f) => JSON.stringify(draft[f.key]) !== JSON.stringify(initial.value[f.key])).map((f) => f.key),
)
const dirty = computed(() => changedKeys.value.length > 0)

function selectorBody(): Record<string, string> {
  if (props.scope.type === 'global') return { scope: 'global' }
  const base: Record<string, string> = { scope: props.scope.type, outboundModel: props.scope.outboundModel }
  if (props.scope.type === 'oauth') base.accountId = props.scope.id
  else base.channelId = props.scope.id
  return base
}

async function save(): Promise<void> {
  for (const k of Object.keys(errors)) delete errors[k]
  const set: Record<string, unknown> = {}
  const cost: Record<string, unknown> = {}
  const unset: string[] = []
  for (const key of changedKeys.value) {
    const f = META_FIELDS.find((x) => x.key === key)!
    const d = draft[key]
    if (!d.on) {
      if (initial.value[key].on) unset.push(key)
      continue
    }
    const r = parse(f, d)
    if (!r.ok) {
      errors[key] = r.error
      continue
    }
    if (key.startsWith('cost.')) cost[key.slice(5)] = r.value
    else set[key] = r.value
  }
  if (Object.keys(errors).length) return
  if (Object.keys(cost).length) set.cost = cost
  if (!Object.keys(set).length && !unset.length) return
  saving.value = true
  try {
    await api.patch(`/model-metadata/${enc(props.modelId)}/overrides`, { ...selectorBody(), set, unset }, { ifMatch: record.value?.revision })
    toast('元数据已保存', 'success')
    emit('changed')
    await load()
  } catch (err) {
    if (err instanceof ApiError && err.fields?.length) {
      for (const fe of err.fields) {
        const k = fe.path.replace(/^set\./, '').replace(/^body\./, '')
        if (META_FIELDS.some((f) => f.key === k)) errors[k] = fe.message
      }
    }
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('元数据已在别处被修改', 'warning', '已重新载入，请检查后再保存。', 7000)
      await load()
    } else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}

async function resetLayer(): Promise<void> {
  const ok = await confirmAction({
    title: '恢复这一层全部字段的继承？',
    detail: '只删除这一层的手工设置。目录匹配、模型启停以及其他来源的设置保持不变。',
    confirmText: '确认恢复',
    danger: true,
  })
  if (!ok) return
  busy.value = 'reset'
  try {
    const q = new URLSearchParams({ scope: props.scope.type })
    if (props.scope.type === 'oauth') q.set('accountId', props.scope.id)
    if (props.scope.type === 'api') q.set('channelId', props.scope.id)
    await api.del(`/model-metadata/${enc(props.modelId)}/overrides?${q}`, { ifMatch: record.value?.revision })
    toast('已恢复全部字段继承', 'success')
    emit('changed')
    await load()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
  }
}

async function bind(entry: CatalogData): Promise<void> {
  catalogOpen.value = false
  busy.value = 'bind'
  try {
    const body: Record<string, string> = { ...selectorBody(), targetModelId: entry.key, providerId: entry.providerId }
    await api.put(`/model-metadata/${enc(props.modelId)}/binding`, body, { ifMatch: record.value?.revision })
    toast('目录匹配已校正', 'success', `${entry.name}（${entry.providerName}）`)
    emit('changed')
    await load()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
  }
}

async function unbind(): Promise<void> {
  const global = props.scope.type === 'global'
  const ok = await confirmAction({
    title: global ? '恢复自动匹配？' : '恢复继承通用匹配？',
    detail: global
      ? '删除手动选择的目录条目，立即按公共目录重新自动匹配。手工填写的字段保留。'
      : '删除这个来源单独选择的目录条目，回到模型通用的匹配。手工填写的字段保留。',
    confirmText: global ? '恢复自动匹配' : '恢复继承',
    danger: true,
  })
  if (!ok) return
  busy.value = 'unbind'
  try {
    const q = new URLSearchParams({ scope: props.scope.type })
    if (props.scope.type === 'oauth') q.set('accountId', props.scope.id)
    if (props.scope.type === 'api') q.set('channelId', props.scope.id)
    await api.del(`/model-metadata/${enc(props.modelId)}/binding?${q}`, { ifMatch: record.value?.revision })
    toast(global ? '已恢复自动匹配' : '已恢复继承通用匹配', 'success')
    emit('changed')
    await load()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
  }
}

const groups = computed(() => (['capacity', 'capability', 'price'] as const).map((g) => ({ g, fields: META_FIELDS.filter((f) => f.group === g) })))
const hasLayer = computed(() => overrideKeys(layer.value).length > 0)
const title = computed(() => (props.scope.type === 'global' ? '编辑模型通用元数据' : `编辑 ${props.scope.label} 的元数据`))
const catalogQuery = computed(() => props.modelId.replace(/[-_.]/g, ' '))

function close(): void {
  if (dirty.value) {
    confirmAction({ title: '放弃未保存的修改？', confirmText: '放弃', danger: true }).then((ok) => ok && emit('close'))
    return
  }
  emit('close')
}
</script>

<template>
  <div class="mf">
    <header class="mf-head">
      <p class="mf-title">{{ title }}</p>
      <Btn size="sm" variant="ghost" @click="close">收起</Btn>
    </header>
    <div v-if="loading && !record" class="sk"><Skeleton h="60px" r="10px" /><Skeleton h="200px" r="10px" /></div>
    <p v-else-if="loadError" class="err">{{ loadError }}</p>
    <div v-else-if="record" class="ed">
      <section class="bind">
        <div class="bind-info">
          <p class="k">目录匹配</p>
          <p class="v">
            <span v-if="matchTarget(record.target)" class="mono">{{ record.target }}</span>
            <span v-else class="faint">未匹配</span>
            <Badge :tone="isManualSource(record.source) ? 'blue' : record.source === 'none' ? 'neutral' : 'green'">{{ metaSourceLabel(record) }}</Badge>
          </p>
          <p class="faint small">仅在自动匹配到错误型号时校正；不会修改真实模型名称。</p>
        </div>
        <div class="bind-act">
          <Btn size="sm" :loading="busy === 'bind'" @click="catalogOpen = true"><Search /> 从目录选择</Btn>
          <Btn v-if="isManualSource(record.source)" size="sm" variant="ghost" :loading="busy === 'unbind'" @click="unbind">
            <Unlink /> {{ scope.type === 'global' ? '恢复自动匹配' : '恢复继承' }}
          </Btn>
        </div>
      </section>

      <p class="note">打开「手工设置」后填写的值会覆盖目录和账户资料；关闭即恢复继承。0、不支持、空列表都会按明确值保存。</p>

      <section v-for="grp in groups" :key="grp.g" class="grp">
        <p class="grp-title">{{ META_GROUP_LABEL[grp.g] }}</p>
        <div v-for="f in grp.fields" :key="f.key" class="row" :class="{ on: draft[f.key]?.on }">
          <div class="lab">
            <p class="name">{{ f.label }}</p>
            <p class="cur faint">
              当前 {{ formatMeta(f, metaValue(record.effective, f.key)) }}
              <template v-if="record.valueSource?.[f.key]"> · {{ valueSourceLabel(record.valueSource?.[f.key]) }}</template>
            </p>
            <p v-if="record.constrainedBy?.[f.key]?.length" class="cur warn">已按服务方硬限制收紧，手工值不能超过限制</p>
          </div>
          <div class="fctl">
            <template v-if="draft[f.key]?.on">
              <Segmented
                v-if="f.kind === 'bool'"
                v-model="draft[f.key].bool"
                size="sm"
                :options="[
                  { value: 'yes', label: '支持' },
                  { value: 'no', label: '不支持' },
                ]"
              />
              <TextInput
                v-else
                v-model="draft[f.key].text"
                :mono="f.kind !== 'list'"
                :invalid="Boolean(errors[f.key])"
                :placeholder="f.kind === 'tokens' ? '如 200k' : f.kind === 'price' ? '美元 / 百万 Token' : f.hint"
                width="140px"
              />
            </template>
            <span v-else class="inherit faint">继承</span>
            <Switch v-model="draft[f.key].on" :label="`手工设置${f.label}`" />
          </div>
          <p v-if="errors[f.key]" class="ferr">{{ errors[f.key] }}</p>
        </div>
      </section>
    </div>
    <footer v-if="record" class="foot">
      <Btn v-if="hasLayer" size="sm" variant="ghost" :loading="busy === 'reset'" @click="resetLayer"><RotateCcw /> 全部恢复继承</Btn>
      <span class="spacer" />
      <Btn size="sm" variant="ghost" :disabled="!dirty || saving" @click="resetDraft">还原</Btn>
      <Btn size="sm" variant="primary" :loading="saving" :disabled="!dirty" @click="save">保存</Btn>
    </footer>
  </div>
  <CatalogPicker
    :open="catalogOpen"
    :initial-query="catalogQuery"
    :current-key="record?.target"
    :subtitle="`为 ${modelId} 选择正确的目录条目`"
    @close="catalogOpen = false"
    @pick="bind"
  />
</template>

<style scoped>
.mf {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 12px 0;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 35%, transparent);
}
.mf-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.mf-title {
  font-size: 13px;
  font-weight: 650;
}
.foot {
  position: sticky;
  bottom: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin: 0 -12px;
  padding: 10px 12px;
  border-top: 0.5px solid var(--hairline);
  border-radius: 0 0 var(--radius-md) var(--radius-md);
  background: color-mix(in srgb, var(--surface-solid) 92%, transparent);
  backdrop-filter: blur(12px);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.err {
  color: var(--text-2);
}
.ed {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.bind {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 16px;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--surface);
}
.bind-info {
  flex: 1;
  min-width: 200px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.k {
  font-size: 11.5px;
  color: var(--text-3);
}
.v {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  word-break: break-all;
}
.bind-act {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.note {
  font-size: 12px;
  color: var(--text-2);
  line-height: 1.6;
}
.grp {
  display: flex;
  flex-direction: column;
}
.grp-title {
  font-size: 12px;
  font-weight: 650;
  color: var(--text-2);
  padding-bottom: 4px;
}
.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  padding: 8px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.row:last-child {
  border-bottom: 0;
}
.lab {
  flex: 1;
  min-width: 150px;
}
.name {
  font-size: 13px;
}
.row.on .name {
  font-weight: 600;
  color: var(--accent);
}
.cur {
  font-size: 11.5px;
  margin-top: 1px;
  font-variant-numeric: tabular-nums;
}
.cur.warn {
  color: var(--orange);
}
.fctl {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 32px;
}
.inherit {
  font-size: 12px;
}
.ferr {
  width: 100%;
  font-size: 11.5px;
  color: var(--red);
  text-align: right;
}
.small {
  font-size: 11.5px;
}
.spacer {
  flex: 1;
}
@media (max-width: 560px) {
  .row {
    flex-wrap: nowrap;
  }
  .row.on {
    flex-wrap: wrap;
  }
  .lab {
    min-width: 0;
  }
  .row.on .fctl {
    width: 100%;
    justify-content: flex-end;
  }
  .row.on .fctl :deep(.ctl) {
    flex: 1;
  }
}
</style>
