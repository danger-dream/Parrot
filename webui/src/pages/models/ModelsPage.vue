<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Boxes, Eye, EyeOff, Power, PowerOff, RefreshCw, X } from '@lucide/vue'
import { api, ApiError, type Schemas } from '@/api/client'
import { openModel } from '@/links'
import { refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { inspector, toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Page from '@/components/ui/Page.vue'
import Btn from '@/components/ui/Btn.vue'
import Segmented from '@/components/ui/Segmented.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Empty from '@/components/ui/Empty.vue'
import ModelRow from './ModelRow.vue'
import { runMetadataSync } from './metadataSync'
import { displayStatus, fetchAll, loadModels, modelStore, providerLabel, sourceName, statusRank, type ModelData } from './shared'

const RegistryPanel = defineAsyncComponent(() => import('./RegistryPanel.vue'))

type Tab = 'list' | 'registry'
type KindFilter = '' | 'chat' | 'image' | 'video'
/** '' = all, 'hidden' = not listed to clients, otherwise a status label exactly as the rows show it. */
type StatusFilter = string

const route = useRoute()
const router = useRouter()
const tab = ref<Tab>('list')
const q = ref('')
const kind = ref<KindFilter>('')
const statusFilter = ref<StatusFilter>('')
const sourceFilter = ref('')
const selected = ref<string[]>([])
const busy = ref('')

const TABS: { value: Tab; label: string }[] = [
  { value: 'list', label: '模型' },
  { value: 'registry', label: '统一命名' },
]

function applyQuery(): void {
  const t = route.query.tab
  if (typeof t === 'string' && TABS.some((x) => x.value === t)) tab.value = t as Tab
  const m = route.query.model
  if (typeof m === 'string' && m) openModel(m)
}
onMounted(() => {
  applyQuery()
  loadModels(true).then(pollUsage)
  refreshCatalog()
})
onBeforeUnmount(() => window.clearTimeout(usageTimer))

// ---- usage (listModels `usage`; TG projects only the chosen source when filtered) ----
let usageTimer: number | undefined
let usageTries = 0
/** The server answers `usageLoading` on a cold snapshot; re-read a few times until it is ready. */
function pollUsage(): void {
  window.clearTimeout(usageTimer)
  const loading = modelStore.models.some((m) => m.usageLoading) || Object.values(sourceUsage.value).some((x) => x.loading)
  if (!loading || usageTries >= 10) return
  usageTries++
  usageTimer = window.setTimeout(async () => {
    await loadModels(true)
    if (sourceRef.value) await loadSourceUsage()
    pollUsage()
  }, 3000)
}
const sourceUsage = ref<Record<string, { usage: ModelData['usage']; loading: boolean }>>({})
async function loadSourceUsage(): Promise<void> {
  const r = sourceRef.value
  if (!r) {
    sourceUsage.value = {}
    return
  }
  try {
    const res = await fetchAll<ModelData>('/models', { sourceType: r.type, sourceId: r.id })
    if (sourceRef.value !== r) return
    const out: Record<string, { usage: ModelData['usage']; loading: boolean }> = {}
    for (const m of res.items) out[m.modelId] = { usage: m.usage, loading: m.usageLoading }
    sourceUsage.value = out
  } catch {
    sourceUsage.value = {}
  }
}
function usageOf(m: ModelData): { usage: ModelData['usage']; loading: boolean } {
  if (sourceRef.value) return sourceUsage.value[m.modelId] || { usage: null, loading: true }
  return { usage: m.usage, loading: m.usageLoading }
}
watch(() => route.query, applyQuery)
watch(tab, (t) => {
  if ((route.query.tab || 'list') !== t) router.replace({ query: { ...route.query, tab: t === 'list' ? undefined : t, model: undefined } })
})

// ---- filters ----
const sourceOptions = computed(() => {
  const map = new Map<string, string>()
  for (const m of modelStore.models) for (const s of m.sources || []) map.set(`${s.type}|${s.id}`, sourceName(s))
  const opts = [...map.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => ({ value, label }))
  return [{ value: '', label: '全部来源' }, ...opts]
})
const sourceRef = computed(() => {
  if (!sourceFilter.value) return null
  const [type, ...rest] = sourceFilter.value.split('|')
  return { type: type as 'oauth' | 'api', id: rest.join('|') }
})
watch(sourceFilter, async () => {
  usageTries = 0
  await loadSourceUsage()
  pollUsage()
})
const sourceLabel = computed(() => sourceOptions.value.find((o) => o.value === sourceFilter.value)?.label || '')

function inSource(m: ModelData) {
  const r = sourceRef.value
  return r ? (m.sources || []).find((s) => s.type === r.type && s.id === r.id) : undefined
}

function byStatusThenName(a: ModelData, b: ModelData): number {
  const sa = inSource(a)
  const sb = inSource(b)
  return (
    statusRank(a, sa) - statusRank(b, sb) ||
    displayStatus(a, sa).label.localeCompare(displayStatus(b, sb).label, 'zh-CN') ||
    a.modelId.localeCompare(b.modelId, 'en', { numeric: true })
  )
}

/** Every filter except the status one, so the status options can count what they would show. */
function matchesOthers(m: ModelData, s: string): boolean {
  if (kind.value && m.identity.type !== kind.value) return false
  if (sourceRef.value && !inSource(m)) return false
  if (!s) return true
  return (
    m.modelId.toLowerCase().includes(s) ||
    (m.aliases || []).some((a) => a.toLowerCase().includes(s)) ||
    (m.sources || []).some((x) => x.outboundModel.toLowerCase().includes(s) || sourceName(x).toLowerCase().includes(s) || providerLabel(x.provider).toLowerCase().includes(s))
  )
}
const base = computed(() => {
  const s = q.value.trim().toLowerCase()
  return modelStore.models.filter((m) => matchesOthers(m, s))
})

const filtered = computed(() =>
  base.value
    .filter((m) => {
      if (!statusFilter.value) return true
      if (statusFilter.value === 'hidden') return m.visible === false
      return displayStatus(m, inSource(m)).label === statusFilter.value
    })
    .sort(byStatusThenName),
)

const counts = computed(() => {
  const c = { all: modelStore.models.length, chat: 0, image: 0, video: 0 }
  for (const m of modelStore.models) c[m.identity.type as 'chat' | 'image' | 'video']++
  return c
})
const kindOptions = computed(() => [
  { value: '' as KindFilter, label: `全部 ${counts.value.all}` },
  { value: 'chat' as KindFilter, label: `对话 ${counts.value.chat}` },
  { value: 'image' as KindFilter, label: `图片 ${counts.value.image}` },
  { value: 'video' as KindFilter, label: `视频 ${counts.value.video}` },
])
/** The statuses the rows actually show, in list order, with how many models each would list. */
const statusOptions = computed(() => {
  const seen = new Map<string, { rank: number; n: number }>()
  for (const m of base.value) {
    const src = inSource(m)
    const label = displayStatus(m, src).label
    const hit = seen.get(label) || { rank: statusRank(m, src), n: 0 }
    hit.n++
    seen.set(label, hit)
  }
  if (statusFilter.value && statusFilter.value !== 'hidden' && !seen.has(statusFilter.value)) seen.set(statusFilter.value, { rank: 9, n: 0 })
  const hidden = base.value.filter((m) => m.visible === false).length
  return [
    { value: '', label: `全部状态 ${base.value.length}` },
    ...[...seen.entries()]
      .sort((a, b) => a[1].rank - b[1].rank || a[0].localeCompare(b[0], 'zh-CN'))
      .map(([label, v]) => ({ value: label, label: `${label} ${v.n}` })),
    ...(hidden || statusFilter.value === 'hidden' ? [{ value: 'hidden', label: `对下游隐藏 ${hidden}` }] : []),
  ]
})
const hasFilter = computed(() => Boolean(q.value || kind.value || statusFilter.value || sourceFilter.value))
function clearFilters(): void {
  q.value = ''
  kind.value = ''
  statusFilter.value = ''
  sourceFilter.value = ''
}

// ---- selection ----
const selectable = (m: ModelData) => m.editable && m.identity.type !== 'video'
const selectedSet = computed(() => new Set(selected.value))
const visibleSelectable = computed(() => filtered.value.filter(selectable))
const allChecked = computed(() => visibleSelectable.value.length > 0 && visibleSelectable.value.every((m) => selectedSet.value.has(m.modelId)))
const someChecked = computed(() => !allChecked.value && visibleSelectable.value.some((m) => selectedSet.value.has(m.modelId)))

function toggle(id: string): void {
  const next = new Set(selected.value)
  next.has(id) ? next.delete(id) : next.add(id)
  selected.value = [...next]
}
function toggleAll(): void {
  const next = new Set(selected.value)
  if (allChecked.value) visibleSelectable.value.forEach((m) => next.delete(m.modelId))
  else visibleSelectable.value.forEach((m) => next.add(m.modelId))
  selected.value = [...next]
}
watch(
  () => modelStore.models,
  (list) => {
    const ids = new Set(list.map((m) => m.modelId))
    selected.value = selected.value.filter((id) => ids.has(id))
  },
)

async function bulk(target: { enabled?: boolean; visible?: boolean }): Promise<void> {
  const ids = [...selected.value]
  if (!ids.length) return
  const scoped = target.enabled !== undefined && sourceRef.value
  const verb = target.enabled === true ? '启用' : target.enabled === false ? '停用' : target.visible ? '对下游展示' : '对下游隐藏'
  if (target.enabled === false || target.visible === false) {
    const ok = await confirmAction({
      title: `${verb} ${ids.length} 个模型？`,
      detail:
        target.enabled === false
          ? scoped
            ? `只停用这些模型在「${sourceLabel.value}」上的调度，其他来源不受影响。`
            : '停用后所有来源都不再调度这些模型，客户端调用会失败。'
          : '隐藏后它们不再出现在 /v1/models 列表里，但仍可按名称调用。',
      confirmText: verb,
      danger: target.enabled === false,
    })
    if (!ok) return
  }
  busy.value = verb
  try {
    const scope = scoped ? { type: sourceRef.value!.type, id: sourceRef.value!.id } : { type: 'global' }
    const res = await api.patch<Schemas['ModelStateData']>(
      '/models/actions/state',
      { scope, selection: { mode: 'ids', modelIds: ids }, target },
      { ifMatch: modelStore.revision },
    )
    const updated = res.data.items.filter((x) => x.status === 'updated').length
    toast(`已${verb} ${updated} 个模型`, 'success', updated < ids.length ? `${ids.length - updated} 个原本就是这个状态` : undefined)
    selected.value = []
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') toast('模型列表已在别处被修改', 'warning', '已重新载入，请确认选择后再操作。', 7000)
    else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
    await loadModels(true)
  }
}

async function syncSelected(): Promise<void> {
  busy.value = 'sync'
  const ok = await runMetadataSync({ mode: 'selected', targets: selected.value.map((modelId) => ({ modelId })), refreshCatalog: true }, modelStore.metadataRevision)
  busy.value = ''
  if (ok) loadModels(true)
}
async function syncAll(): Promise<void> {
  busy.value = 'syncAll'
  const ok = await runMetadataSync({ mode: 'full', refreshCatalog: true }, modelStore.metadataRevision)
  busy.value = ''
  if (ok) loadModels(true)
}

const selectedId = computed(() => (inspector.open && inspector.key.startsWith('model:') ? inspector.key.slice(6) : ''))
</script>

<template>
  <Page title="模型中心" subtitle="所有可用模型的统一管理：启停与可见性、别名、压缩模型、元数据与价格、各来源的额度。" width="wide">
    <template #actions>
      <Segmented v-model="tab" :options="TABS" />
    </template>

    <template v-if="tab === 'list'" #toolbar>
      <SearchField v-model="q" placeholder="搜索模型、别名、上游名或来源" width="240px" />
      <Segmented v-model="kind" size="sm" :options="kindOptions" />
      <Select v-model="statusFilter" :options="statusOptions" width="140px" />
      <Select v-model="sourceFilter" :options="sourceOptions" width="200px" />
      <Btn v-if="hasFilter" variant="ghost" size="sm" @click="clearFilters"><X /> 清除</Btn>
      <span class="spacer" />
      <Btn variant="ghost" size="sm" :loading="busy === 'syncAll'" title="刷新 models.dev 公共目录并重新匹配；人工匹配和手工字段保持" @click="syncAll"><RefreshCw /> 同步全部元数据</Btn>
      <Btn icon size="sm" title="刷新" :loading="modelStore.loading" @click="loadModels(true)"><RefreshCw /></Btn>
    </template>

    <template v-if="tab === 'list'">
      <Transition name="bar">
        <div v-if="selected.length" class="bulk">
          <span class="bulk-n">已选 {{ selected.length }} 个</span>
          <span v-if="sourceRef" class="bulk-scope faint">启停仅对「{{ sourceLabel }}」生效</span>
          <span class="spacer" />
          <Btn size="sm" :loading="busy === '启用'" @click="bulk({ enabled: true })"><Power /> 启用</Btn>
          <Btn size="sm" variant="danger" :loading="busy === '停用'" @click="bulk({ enabled: false })"><PowerOff /> 停用</Btn>
          <Btn size="sm" :loading="busy === '对下游展示'" @click="bulk({ visible: true })"><Eye /> 显示</Btn>
          <Btn size="sm" :loading="busy === '对下游隐藏'" @click="bulk({ visible: false })"><EyeOff /> 隐藏</Btn>
          <Btn size="sm" :loading="busy === 'sync'" @click="syncSelected"><RefreshCw /> 同步元数据</Btn>
          <Btn size="sm" variant="ghost" @click="selected = []">取消选择</Btn>
        </div>
      </Transition>

      <div class="table">
        <div class="rows">
          <div class="head">
            <span class="c-check">
              <input type="checkbox" :checked="allChecked" :indeterminate="someChecked" aria-label="全选" @change="toggleAll" />
            </span>
            <span class="c-model">模型</span>
            <span class="c-src">来源</span>
            <span class="c-ctx">上下文 / 压缩</span>
            <span class="c-out">最大输出</span>
            <span class="c-price">价格 入 / 出</span>
            <span class="c-use">累计用量</span>
            <span class="c-state">状态</span>
          </div>
          <template v-if="modelStore.loading && !modelStore.models.length">
            <div v-for="i in 10" :key="i" class="sk-row"><Skeleton h="16px" /></div>
          </template>
          <p v-else-if="modelStore.error && !modelStore.models.length" class="err">{{ errorTitle(modelStore.error) }}</p>
          <ModelRow
            v-for="m in filtered"
            :key="m.resourceKey"
            :model="m"
            :meta="modelStore.metadata[m.modelId]"
            :source-meta="modelStore.sourceMetadata[m.modelId]"
            :source="inSource(m)"
            :usage="usageOf(m).usage"
            :usage-loading="usageOf(m).loading"
            :compression="modelStore.compression?.modelId === m.modelId"
            :checked="selectedSet.has(m.modelId)"
            :selectable="selectable(m)"
            :selected="selectedId === m.modelId"
            @toggle="toggle(m.modelId)"
            @open="openModel(m.modelId)"
          />
          <Empty v-if="modelStore.loaded && !filtered.length" title="没有匹配的模型" :detail="hasFilter ? '换个筛选条件试试' : '还没有任何模型，请先添加 OAuth 账户或 API 渠道'">
            <template #icon><Boxes /></template>
            <Btn v-if="hasFilter" size="sm" @click="clearFilters">清除筛选</Btn>
          </Empty>
        </div>
      </div>
      <p v-if="modelStore.loaded" class="foot faint">
        共 {{ filtered.length }} 个模型<template v-if="filtered.length !== modelStore.models.length">（全部 {{ modelStore.models.length }} 个）</template>。点一行查看详情；勾选后可批量启停、显示隐藏或同步元数据。数值后的 <span class="star">*</span> 表示部分来源的值不同，悬停可查看；价格单位为美元 / 百万 Token。
      </p>
    </template>

    <RegistryPanel v-else-if="tab === 'registry'" />

  </Page>
</template>

<style scoped>
.spacer {
  flex: 1;
}
.bulk {
  position: sticky;
  top: 30px;
  z-index: 4;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 8px 10px 8px 14px;
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--accent) 12%, var(--surface-solid));
  box-shadow: var(--shadow-md), inset 0 0 0 0.5px color-mix(in srgb, var(--accent) 40%, transparent);
}
.bulk-n {
  font-weight: 600;
  font-size: 13px;
}
.bulk-scope {
  font-size: 12px;
}
.bar-enter-active,
.bar-leave-active {
  transition: opacity 0.18s, transform 0.18s var(--ease-out);
}
.bar-enter-from,
.bar-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
.table {
  container-type: inline-size;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  overflow: hidden;
}
.rows {
  --cols: 22px minmax(220px, 2.6fr) 96px 84px 68px 112px 150px 104px;
  --areas: 'check model src ctx out price use state';
}
.head {
  display: grid;
  grid-template-columns: var(--cols);
  grid-template-areas: var(--areas);
  column-gap: 12px;
  height: 34px;
  align-items: center;
  padding: 0 14px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 0.5px solid var(--border);
}
.head .c-check { grid-area: check; display: flex; }
.head .c-model { grid-area: model; }
.head .c-src { grid-area: src; }
.head .c-out { grid-area: out; text-align: right; }
.head .c-ctx { grid-area: ctx; text-align: right; }
.head .c-use { grid-area: use; text-align: right; }
.head .c-price { grid-area: price; text-align: right; }
.head .c-state { grid-area: state; text-align: right; }
.c-check input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
}
.rows :deep(.row:last-child) {
  border-bottom: 0;
}
.sk-row {
  padding: 13px 14px;
  border-bottom: 0.5px solid var(--hairline);
}
.err {
  padding: 20px;
  color: var(--text-2);
}
.foot {
  font-size: 12px;
}
.star {
  color: var(--orange);
}
@container (max-width: 1080px) {
  .rows {
    --cols: 22px minmax(180px, 2.4fr) 88px 80px 64px 140px 100px;
    --areas: 'check model src ctx out use state';
  }
  .rows :deep(.c-price) {
    display: none;
  }
}
@container (max-width: 720px) {
  .rows {
    --cols: 22px minmax(0, 1fr) auto;
    --areas: 'check model state' '. meta meta';
  }
  .head {
    display: none;
  }
  .rows :deep(.c-src),
  .rows :deep(.c-out),
  .rows :deep(.c-ctx),
  .rows :deep(.c-use),
  .rows :deep(.c-price) {
    display: none;
  }
  .rows :deep(.c-meta) {
    display: flex !important;
  }
  .rows :deep(.row) {
    padding: 10px 14px;
    row-gap: 4px;
  }
}
@media (max-width: 767px) {
  .bulk {
    top: 0;
  }
  /* The filter bar wraps to several lines on phones; keep it in the flow instead of sticky. */
  :deep(.toolbar) {
    position: static;
  }
}
</style>
