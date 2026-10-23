<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Info, ListFilter, PencilLine, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import type { StatsPeriod } from '@/api/types'
import { goLogs, openChannel, openModel } from '@/links'
import { refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { integer, percent } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import DetailSection from '@/components/DetailSection.vue'
import StatsMini from '@/components/StatsMini.vue'
import { loadCompare, type Compare, type ComparePeriod } from '@/utils/compare'
import MetadataForm, { type MetaScope } from './MetadataForm.vue'
import SourceCard from './SourceCard.vue'
import AliasSection from './AliasSection.vue'
import { useCardDrag } from '@/pages/channels/useCardDrag'
import { runMetadataSync } from './metadataSync'
import {
  EFFORTS,
  KIND_LABEL,
  META_FIELDS,
  META_GROUP_LABEL,
  capabilityTags,
  channelLabel,
  effortLabel,
  findModel,
  formatMeta,
  groupSources,
  isManualSource,
  loadModels,
  matchTarget,
  metaSourceLabel,
  metaValue,
  modelStatus,
  modelStore,
  overrideKeys,
  setCompressionLocal,
  rateSummary,
  sourceName,
  type ModelData,
  type ModelMetadata,
  type SourceGroup,
} from './shared'

type ModelStats = Schemas['ModelStatsData']
type Registry = Schemas['ModelRegistryData']
type Compression = Schemas['CompressionModelData']

// `id` is the client-visible model name (it may also be an alias or a name that only
// appears in request logs). The model center record is looked up by name.
const props = defineProps<{ id: string }>()

const period = ref<StatsPeriod>('month')
const stats = ref<ModelStats | null>(null)
const statsError = ref('')
const statsLoading = ref(false)
/** This period against the one before it, for the tiles' top-right change. */
const compare = ref<Compare | null>(null)
const model = ref<ModelData | null>(null)
const resolved = ref(false)
const meta = ref<ModelMetadata | null>(null)
const metaMissing = ref(false)
const registry = ref<Registry | null>(null)
const busy = ref('')
/** Inline metadata editor: 'common' or a source group key. */
const editing = ref('')

const isAlias = computed(() => Boolean(model.value && model.value.modelId !== props.id))
const modelId = computed(() => model.value?.modelId || props.id)

let statsSeq = 0
async function loadStats(): Promise<void> {
  const my = ++statsSeq
  statsLoading.value = true
  try {
    const p = period.value
    const [data, cmp] = await Promise.all([
      api.get<ModelStats>(`/stats/models/${enc(props.id)}`, { period: p }).then((r) => r.data),
      p === 'lifetime' ? Promise.resolve(null) : loadCompare({ period: p as ComparePeriod, model: props.id }).catch(() => null),
    ])
    if (my !== statsSeq) return
    stats.value = data
    compare.value = cmp
    statsError.value = ''
  } catch (err) {
    if (my !== statsSeq) return
    // The stats endpoint answers 404 when the logs hold no request for this name at all.
    statsError.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND' ? '请求日志里还没有这个模型的请求。' : errorTitle(err)
    stats.value = null
  } finally {
    if (my === statsSeq) statsLoading.value = false
  }
}

async function resolveModel(force = false): Promise<void> {
  await loadModels(force)
  const hit = findModel(props.id)
  if (hit) {
    try {
      model.value = (await api.get<ModelData>(`/models/${enc(hit.resourceKey)}`)).data
    } catch {
      model.value = hit
    }
  } else model.value = null
  resolved.value = true
}

async function loadMeta(): Promise<void> {
  if (!model.value) return
  try {
    meta.value = (await api.get<ModelMetadata>(`/model-metadata/${enc(model.value.modelId)}`)).data
    metaMissing.value = false
  } catch (err) {
    meta.value = null
    metaMissing.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND'
  }
}

async function loadRegistry(): Promise<void> {
  registry.value = (await api.get<Registry>('/models/registry').catch(() => null))?.data || null
}

async function loadExtras(): Promise<void> {
  if (!model.value) return
  await Promise.all([loadMeta(), loadRegistry()])
}

async function loadAll(force = false): Promise<void> {
  resolved.value = false
  stats.value = null
  model.value = null
  meta.value = null
  editing.value = ''
  refreshCatalog()
  loadStats()
  await resolveModel(force)
  await loadExtras()
}

onMounted(() => loadAll())
watch(() => props.id, () => loadAll())
watch(period, loadStats)

// ---- state ----
const editable = computed(() => Boolean(model.value?.editable) && model.value?.identity.type !== 'video')
const status = computed(() => (model.value ? modelStatus(model.value) : null))

async function setState(scope: { type: string; id?: string }, target: { enabled?: boolean; visible?: boolean }, key: string, done: string): Promise<void> {
  if (!model.value) return
  busy.value = key
  try {
    await api.patch('/models/actions/state', { scope, selection: { mode: 'ids', modelIds: [model.value.modelId] }, target }, { ifMatch: model.value.revision })
    toast(done, 'success')
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') toast('模型状态已在别处被修改', 'warning', '已载入最新状态，请确认后再操作。', 7000)
    else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    await resolveModel(true)
    busy.value = ''
  }
}

async function toggleGlobal(on: boolean): Promise<void> {
  if (!on) {
    const ok = await confirmAction({
      title: `停用 ${modelId.value}？`,
      detail: '停用后所有来源都不再调度这个模型，客户端调用会失败。之后可以随时重新启用。',
      confirmText: '停用模型',
      danger: true,
    })
    if (!ok) return
  }
  await setState({ type: 'global' }, { enabled: on }, 'global', on ? '模型已启用' : '模型已停用')
}

async function toggleVisible(on: boolean): Promise<void> {
  await setState({ type: 'global' }, { visible: on }, 'visible', on ? '已对下游展示' : '已对下游隐藏')
}

const hasOverrides = computed(() => (model.value?.sources || []).some((s) => s.enabledOverride))
async function clearOverrides(): Promise<void> {
  const ok = await confirmAction({ title: '全局停用并清除来源例外？', detail: '单独启用的来源也会一起停用。', confirmText: '全局停用', danger: true })
  if (ok) await setState({ type: 'global' }, { enabled: false }, 'clear', '已全局停用')
}

async function toggleSource(g: SourceGroup, on: boolean): Promise<void> {
  const name = sourceName(g.items[0])
  if (!on) {
    const ok = await confirmAction({
      title: `停用 ${name} 的这个模型？`,
      detail: '只是不再用这个账户/渠道调度本模型；要停用整个模型，请使用上方的「启用模型」开关。',
      confirmText: '停用此来源',
      danger: true,
    })
    if (!ok) return
  }
  await setState({ type: g.type, id: g.id }, { enabled: on }, `src:${g.key}`, on ? `已启用 ${name}` : `已停用 ${name}`)
}

// ---- sources: getModel orders them as TG does and ranks them in every mode ----
const groups = computed(() => (model.value ? groupSources(model.value) : []))
const liveGroups = computed(() => groups.value.filter((g) => g.live))
const offGroups = computed(() => groups.value.filter((g) => !g.live))

const SCHEDULE: Record<string, string> = {
  smart: '智能调度 · 按实时评分挑选，没有固定位次',
  order: '顺序调度 · 按渠道注册顺序依次尝试',
}
const scheduleText = computed(() => {
  const sc = model.value?.schedule
  if (!sc?.mode) return ''
  if (sc.mode === 'priority') return sc.ownOrder ? '优先级调度 · 本模型专属顺序' : '优先级调度 · 继承统一渠道顺序'
  return SCHEDULE[sc.mode] || ''
})

// ---- source order: drag the scheduled cards (priority scheduling only; other modes have no fixed order) ----
const canOrder = computed(
  () => editable.value && model.value?.identity.type === 'chat' && model.value?.schedule?.mode === 'priority' && liveGroups.value.length > 1,
)
const liveKeys = ref<string[]>([])
const orderSaving = ref(false)
const drag = useCardDrag(liveKeys, async (next, previous) => {
  const channelOf = new Map(liveGroups.value.map((g) => [g.key, g.channelId]))
  const moved = next.map((k) => channelOf.get(k)).filter((c): c is string => Boolean(c))
  orderSaving.value = true
  try {
    const current = (await api.get<Schemas['OrderData']>(`/load-balancing/model-orders/${enc(modelId.value)}`)).data
    const present = new Set(current.order)
    const slots = moved.filter((c) => present.has(c))
    const movedSet = new Set(slots)
    // Sources that are not scheduled keep their positions; the scheduled ones fill their own slots.
    let i = 0
    const order = current.order.map((c) => (movedSet.has(c) ? slots[i++] : c))
    order.push(...moved.filter((c) => !present.has(c)))
    await api.put(`/load-balancing/model-orders/${enc(modelId.value)}`, { order }, { ifMatch: current.revision })
    toast('已保存来源顺序', 'success')
    await resolveModel(true)
  } catch (err) {
    liveKeys.value = previous
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    orderSaving.value = false
  }
})
watch(
  () => liveGroups.value.map((g) => g.key),
  (keys) => {
    if (!drag.dragging.value && !orderSaving.value) liveKeys.value = keys
  },
  { immediate: true },
)
const orderedLive = computed(() => {
  const byKey = new Map(liveGroups.value.map((g) => [g.key, g]))
  const out = liveKeys.value.map((k) => byKey.get(k)).filter((g): g is SourceGroup => Boolean(g))
  return out.length === liveGroups.value.length ? out : liveGroups.value
})

function sourceScope(g: SourceGroup): MetaScope {
  return { type: g.type === 'api' ? 'api' : 'oauth', id: g.id, outboundModel: g.items[0].outboundModel, label: sourceName(g.items[0]) }
}
function toggleEditor(key: string): void {
  editing.value = editing.value === key ? '' : key
}

// ---- metadata (model-common values) ----
/** TG head block: values shared by the scheduled sources (sharedMetadata); the common record otherwise. */
const hasShared = computed(() => Object.keys(model.value?.sharedMetadata || {}).length > 0)
const headValues = computed(() =>
  hasShared.value ? (model.value?.sharedMetadata as unknown as Schemas['MetadataValues']) : meta.value?.effective,
)
const summary = computed(() => {
  const eff = headValues.value
  if (!eff) return [] as { g: string; rows: { key: string; label: string; value: string; manual: boolean }[] }[]
  const manual = new Set(overrideKeys(meta.value?.commonOverride))
  return (['capacity', 'capability', 'price'] as const)
    .map((g) => ({
      g,
      rows: META_FIELDS.filter((f) => f.group === g)
        .map((f) => ({ f, v: metaValue(eff, f.key) }))
        .filter((x) => x.v !== null && x.v !== undefined && x.v !== '' && !(Array.isArray(x.v) && !x.v.length && x.f.key !== 'reasoningEfforts'))
        .map((x) => ({ key: x.f.key, label: x.f.label, value: formatMeta(x.f, x.v), manual: manual.has(x.f.key) })),
    }))
    .filter((x) => x.rows.length)
})
const maxContext = computed(() => {
  const m = headValues.value?.contextWindowMaxMode
  return m && m > (headValues.value?.contextWindow || 0) ? m : null
})
const manualCount = computed(() => overrideKeys(meta.value?.commonOverride).length)
const rates = computed(() => rateSummary(model.value?.creditRates))

async function syncMeta(): Promise<void> {
  if (!model.value) return
  busy.value = 'sync'
  const ok = await runMetadataSync({ mode: 'one', targets: [{ modelId: model.value.modelId }], refreshCatalog: true }, meta.value?.revision)
  busy.value = ''
  if (ok) onMetaChanged()
}
async function onMetaChanged(): Promise<void> {
  await Promise.all([loadMeta(), resolveModel(true)])
}

// ---- default effort ----
const effortChoices = computed(() => {
  const declared = new Set<string>()
  for (const s of model.value?.sources || []) for (const x of (s.effectiveMetadata?.reasoningEfforts as string[] | undefined) || []) declared.add(x)
  return EFFORTS.filter((e) => declared.has(e))
})
const globalEffortText = computed(() => {
  const g = registry.value?.globalDefaultEffort
  return g ? `跟随全局（${effortLabel(g)}）` : '跟随全局'
})
const currentEffort = computed(() => registry.value?.defaultEfforts?.[modelId.value] ?? null)
const effortOptions = computed(() => [
  { value: '' as string, label: globalEffortText.value },
  ...effortChoices.value.map((e) => ({ value: e as string, label: effortLabel(e) })),
])
async function setEffort(v: string): Promise<void> {
  if (!registry.value) return
  busy.value = 'effort'
  try {
    await api.put(`/models/registry/models/${enc(modelId.value)}/default-effort`, { effort: v || null }, { ifMatch: registry.value.revision })
    toast(v ? `默认推理强度：${effortLabel(v)}` : `默认推理强度：${globalEffortText.value}`, 'success')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    await loadRegistry()
    busy.value = ''
  }
}

// ---- compression ----
const compression = computed(() => modelStore.compression)
const isCompression = computed(() => compression.value?.modelId === modelId.value)
async function toggleCompression(on: boolean): Promise<void> {
  busy.value = 'compression'
  try {
    const cur = (await api.get<Compression>('/compression-model')).data
    if (on) {
      if (cur.modelId && cur.modelId !== modelId.value) {
        const ok = await confirmAction({
          title: `把压缩模型换成 ${modelId.value}？`,
          detail: `当前压缩模型是 ${cur.modelId}，同一时间只能指定一个。`,
          confirmText: '更换',
        })
        if (!ok) return
      }
      const res = await api.put<Compression>('/compression-model', { modelId: modelId.value }, { ifMatch: cur.revision })
      setCompressionLocal(res.data.modelId, res.data.revision)
      toast('已设为压缩模型', 'success')
    } else {
      await api.del('/compression-model', { ifMatch: cur.revision })
      const after = (await api.get<Compression>('/compression-model')).data
      setCompressionLocal(after.modelId, after.revision)
      if (after.modelId) toast('取消没有生效', 'warning', `服务端仍返回 ${after.modelId} 作为压缩模型，请刷新后重试。`, 8000)
      else toast('已取消压缩模型', 'success', '上下文压缩将不再指定专用模型')
    }
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
    loadModels(true)
  }
}

// ---- stats ----
const maxChannel = computed(() => Math.max(1, ...(stats.value?.channels || []).map((c) => c.count)))
const unpriced = computed(() => stats.value?.metrics.unpricedSuccess || 0)
</script>

<template>
  <div class="d">
    <!-- header -->
    <section class="hero">
      <p class="name mono">{{ id }}</p>
      <div class="badges">
        <template v-if="model">
          <Badge tone="purple">{{ KIND_LABEL[model.identity.type] }}</Badge>
          <Badge v-if="status" :tone="status.tone" dot>{{ status.label }}</Badge>
          <Badge v-if="model.visible === false" tone="orange">已对下游隐藏</Badge>
          <Badge v-if="isCompression" tone="teal">压缩模型</Badge>
          <span v-for="r in rates" :key="r" class="rate">{{ r }}</span>
        </template>
        <Badge v-else-if="resolved" tone="neutral">不在模型中心</Badge>
      </div>
      <p v-if="isAlias && model" class="alias-note">
        <Info /> 这是别名，实际指向
        <button class="lnk mono" @click="openModel(model.modelId)">{{ model.modelId }}</button>
      </p>
    </section>

    <div v-if="resolved && !model" class="notice">
      <TriangleAlert />
      <span>这个模型不在模型中心里，可能来自映射或已删除。下面仅显示请求统计。</span>
    </div>

    <!-- settings -->
    <section v-if="model && editable" class="state">
      <div class="st-row">
        <div>
          <p class="st-k">启用模型</p>
          <p class="st-h">停用后所有来源都不再调度</p>
        </div>
        <Switch :model-value="model.globalEnabled !== false" :loading="busy === 'global'" label="启用模型" @update:model-value="toggleGlobal" />
      </div>
      <div class="st-row">
        <div>
          <p class="st-k">对下游展示</p>
          <p class="st-h">关闭后不出现在 /v1/models，仍可按名称调用</p>
        </div>
        <Switch :model-value="model.visible !== false" :loading="busy === 'visible'" label="对下游展示" @update:model-value="toggleVisible" />
      </div>
      <div v-if="model.identity.type === 'chat'" class="st-row">
        <div>
          <p class="st-k">压缩模型</p>
          <p class="st-h">
            <template v-if="isCompression">当前用于内部上下文压缩</template>
            <template v-else-if="compression?.modelId">
              当前是 <button class="lnk mono" @click="openModel(compression.modelId)">{{ compression.modelId }}</button>，打开即替换
            </template>
            <template v-else>长对话超过压缩阈值时用它压缩上下文</template>
          </p>
        </div>
        <Switch :model-value="isCompression" :loading="busy === 'compression'" label="设为压缩模型" @update:model-value="toggleCompression" />
      </div>
      <div v-if="model.identity.type === 'chat' && effortChoices.length && registry" class="st-row">
        <div>
          <p class="st-k">默认推理强度</p>
          <p class="st-h">下游没传推理强度时使用；输出上限 ≤1024 的短请求不补</p>
        </div>
        <Select :model-value="currentEffort || ''" :options="effortOptions" :disabled="busy === 'effort'" width="172px" @update:model-value="setEffort" />
      </div>
      <Btn v-if="model.globalEnabled === false && hasOverrides" size="sm" variant="danger" :loading="busy === 'clear'" @click="clearOverrides">
        全局停用（清除所有来源例外）
      </Btn>
    </section>
    <p v-else-if="model && model.identity.type === 'video'" class="faint small">视频模型由图片与视频设置管理，这里只读。</p>

    <!-- stats -->
    <DetailSection title="用量统计">
      <template #actions>
        <Segmented
          v-model="period"
          size="sm"
          :options="[
            { value: 'today', label: '今天' },
            { value: '7d', label: '7 天' },
            { value: 'month', label: '本月' },
          ]"
        />
      </template>
      <p v-if="statsError" class="faint small">{{ statsError }}</p>
      <div v-if="stats && !statsError" class="stats-body" :class="{ dim: statsLoading }">
        <StatsMini :metrics="stats.metrics" :compare="compare" :period="period === 'lifetime' ? null : (period as ComparePeriod)" />
        <p class="faint small">
          <template v-if="stats.metrics.averageTokensPerSecond != null">
            输出速度 平均 {{ stats.metrics.averageTokensPerSecond.toFixed(1) }} · 峰值 {{ (stats.metrics.maximumTokensPerSecond ?? 0).toFixed(0) }} · 最低
            {{ (stats.metrics.minimumTokensPerSecond ?? 0).toFixed(1) }} tok/s
          </template>
          <template v-if="unpriced"> · {{ integer(unpriced) }} 次成功请求没有价格，未计入费用</template>
        </p>
        <div v-if="stats.channels.length" class="chs">
          <button v-for="c in stats.channels" :key="c.key" class="ch-row" @click="openChannel(c.key)">
            <span class="ch-name truncate">{{ channelLabel(c.key) }}</span>
            <span class="bar"><i :style="{ width: `${(c.count / maxChannel) * 100}%` }" /></span>
            <span class="mono ch-n">{{ integer(c.count) }}</span>
            <span class="mono faint ch-p">{{ percent(c.count, stats.metrics.total, 0) }}</span>
          </button>
        </div>
        <p v-else class="faint small">这段时间没有请求</p>
      </div>
      <Skeleton v-else-if="!statsError" h="110px" r="10px" />
    </DetailSection>

    <template v-if="model">
      <!-- metadata -->
      <DetailSection title="容量、能力与价格" :note="(hasShared ? '参与调度的来源一致' : '模型通用值') + (manualCount ? ` · ${manualCount} 项手工设置` : '')">
        <template #actions>
          <Btn size="sm" variant="ghost" :loading="busy === 'sync'" title="从 models.dev 重新匹配；人工匹配与手工字段保持" @click="syncMeta"><RefreshCw /> 同步</Btn>
          <Btn size="sm" :variant="editing === 'common' ? 'primary' : 'secondary'" @click="toggleEditor('common')"><PencilLine /> {{ editing === 'common' ? '编辑中' : '编辑' }}</Btn>
        </template>
        <MetadataForm v-if="editing === 'common'" :model-id="model.modelId" :scope="{ type: 'global' }" @close="editing = ''" @changed="onMetaChanged" />
        <template v-else>
          <p v-if="metaMissing" class="faint small">没有元数据记录。点「同步」从公共目录匹配，或「编辑」手工填写。</p>
          <template v-else-if="meta">
            <p class="match">
              <span class="faint">目录匹配</span>
              <span v-if="matchTarget(meta.target)" class="mono truncate">{{ meta.target }}</span>
              <span v-else class="faint">未匹配</span>
              <Badge :tone="isManualSource(meta.source) ? 'blue' : meta.source === 'none' ? 'neutral' : 'green'">{{ metaSourceLabel(meta) }}</Badge>
            </p>
            <div v-if="capabilityTags(meta.effective).length" class="tags">
              <Badge v-for="t in capabilityTags(meta.effective)" :key="t.key" tone="blue">{{ t.label }}</Badge>
            </div>
            <div v-for="grp in summary" :key="grp.g" class="grp">
              <p class="grp-t">{{ META_GROUP_LABEL[grp.g] }}</p>
              <dl class="facts">
                <div v-for="r in grp.rows" :key="r.key">
                  <dt>{{ r.label }}<span v-if="r.manual" class="manual"> · 手工</span></dt>
                  <dd class="mono">{{ r.value }}</dd>
                </div>
                <div v-if="grp.g === 'capacity' && maxContext">
                  <dt>Max Context 上下文</dt>
                  <dd class="mono">{{ maxContext.toLocaleString('en-US') }}</dd>
                </div>
              </dl>
            </div>
            <p v-if="!summary.length" class="faint small">目录里没有这个模型的容量和价格资料。</p>
          </template>
          <Skeleton v-else h="80px" r="10px" />
        </template>
      </DetailSection>

      <!-- aliases -->
      <AliasSection v-if="model.identity.type !== 'video'" :model-id="model.modelId" @changed="loadModels(true).then(() => resolveModel(true))" />

      <!-- sources -->
      <DetailSection title="来源" :note="orderSaving ? '正在保存顺序…' : `${liveGroups.length} / ${groups.length} 个参与调度${canOrder ? ' · 拖动卡片调整顺序' : ''}`">
        <p v-if="scheduleText" class="faint small">{{ scheduleText }}</p>
        <p v-if="!groups.length" class="faint small">这个模型目前没有来源。</p>
        <template v-for="(list, i) in [orderedLive, offGroups]" :key="i">
          <p v-if="list.length && groups.length > 1" class="sub-t">{{ i === 0 ? `参与调度 · ${list.length}` : `未参与调度 · ${list.length}` }}</p>
          <div v-if="list.length" class="srcs" :data-drag-grid="i === 0 && canOrder ? '' : undefined">
            <template v-for="g in list" :key="g.key">
              <SourceCard
                :data-drag-id="i === 0 && canOrder ? g.key : undefined"
                :group="g"
                :model="model"
                :common="headValues"
                :editable="editable"
                :busy="busy === `src:${g.key}`"
                :editing="editing === g.key"
                :draggable="i === 0 && canOrder && !orderSaving"
                :dragging="drag.dragging.value === g.key"
                @toggle="(v: boolean) => toggleSource(g, v)"
                @edit="toggleEditor(g.key)"
                @dragstart="(e: PointerEvent) => drag.start(e, g.key)"
              />
              <MetadataForm v-if="editing === g.key" :model-id="model.modelId" :scope="sourceScope(g)" @close="editing = ''" @changed="onMetaChanged" />
            </template>
          </div>
        </template>
        <p v-if="offGroups.some((g) => !g.containerEnabled)" class="faint small">整个账户 / 渠道已停用的来源要在账户或渠道管理里启用，这里不提供开关。</p>
      </DetailSection>
    </template>
    <div v-else-if="!resolved" class="sk"><Skeleton h="140px" r="10px" /><Skeleton h="100px" r="10px" /></div>

    <div class="actions">
      <Btn @click="goLogs({ model: id })"><ListFilter /> 查看相关请求</Btn>
    </div>
  </div>
</template>

<style scoped>
.d {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.hero {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.name {
  font-size: 15px;
  font-weight: 650;
  word-break: break-all;
}
.badges,
.tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.rate {
  font-size: 11.5px;
  color: var(--purple);
}
.alias-note,
.notice {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-2);
}
.alias-note svg,
.notice svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
.notice {
  padding: 10px 12px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--orange) 12%, transparent);
  flex-wrap: nowrap;
  align-items: flex-start;
}
.notice svg {
  color: var(--orange);
  margin-top: 2px;
}
.lnk {
  color: var(--accent);
  text-align: left;
}
.lnk:hover {
  text-decoration: underline;
}
.state {
  display: flex;
  flex-direction: column;
  padding: 2px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.state > :deep(.btn) {
  margin: 6px 0 8px;
  align-self: flex-start;
}
.st-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.st-row:last-of-type {
  border-bottom: 0;
}
.st-row > div {
  flex: 1;
  min-width: 0;
}
.st-row > :deep(.wrap) {
  flex-shrink: 0;
}
.st-k {
  font-size: 13px;
}
.st-h {
  font-size: 11.5px;
  color: var(--text-3);
}
.stats-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: opacity 0.2s;
}
.stats-body.dim {
  opacity: 0.45;
}
.chs {
  display: flex;
  flex-direction: column;
}
.ch-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 70px auto 34px;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 0 8px;
  margin: 0 -8px;
  width: calc(100% + 16px);
  border-radius: 6px;
  font-size: 12.5px;
  text-align: left;
}
.ch-row:hover {
  background: var(--fill-3);
}
.bar {
  height: 5px;
  border-radius: 3px;
  background: var(--fill-2);
  overflow: hidden;
}
.bar i {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--indigo);
}
.ch-n {
  font-size: 12px;
  text-align: right;
}
.ch-p {
  font-size: 11px;
  text-align: right;
}
.match {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
}
.grp {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.grp-t {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-2);
}
.facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
}
.facts div {
  min-width: 0;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 1px 0 0;
  font-size: 12px;
  word-break: break-word;
}
.manual {
  color: var(--accent);
}
.sub-t {
  margin-top: 2px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-2);
}
.srcs {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.small {
  font-size: 12px;
  line-height: 1.55;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>
