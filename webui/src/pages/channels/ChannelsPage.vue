<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Eraser, Link2Off, Plus, RefreshCw, TriangleAlert, Waypoints } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { runAction } from '@/composables/useResource'
import { openChannel } from '@/links'
import { refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { inspector, toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Page from '@/components/ui/Page.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import ChannelCard from './ChannelCard.vue'
import ChannelRow from './ChannelRow.vue'
import ChannelCreateModal from './ChannelCreateModal.vue'
import { useCardDrag } from './useCardDrag'
import { channelBus, notifyChannelsChanged, type Catalog, type Channel, type ChannelListItem } from './meta'

type Filter = 'all' | 'ok' | 'problem' | 'off'

const route = useRoute()
const channels = ref<ChannelListItem[]>([])
const order = ref<string[]>([])
const orderRevision = ref('')
const providers = ref<Record<string, { name: string; presets: Record<string, string> }>>({})
const loading = ref(true)
const error = ref('')
const q = ref('')
const filter = ref<Filter>('all')
const createOpen = ref(false)
const savingOrder = ref(false)
const busy = ref('')
let timer: number | undefined

async function loadAll(): Promise<ChannelListItem[]> {
  const all: ChannelListItem[] = []
  let page = 1
  for (;;) {
    const res = await api.get<ChannelListItem[]>('/channels', { page, pageSize: 200 })
    all.push(...res.data)
    orderRevision.value = (res.meta as { orderRevision?: string }).orderRevision || ''
    if (!res.meta.hasNext) return all
    page++
  }
}

const outcomes = ref<Record<string, Schemas['RecentOutcomeData']>>({})
const view = ref<'table' | 'cards'>(localStorage.getItem('parrot.channels.view') === 'cards' ? 'cards' : 'table')
watch(view, (v) => localStorage.setItem('parrot.channels.view', v))
const toggling = ref('')

async function toggleChannel(c: ChannelListItem, on: boolean): Promise<void> {
  toggling.value = c.id
  try {
    await api.patch(`/channels/${enc(c.id)}`, { enabled: on } satisfies Schemas['ChannelUpdateRequest'], { ifMatch: c.revision })
    toast(on ? '渠道已启用' : '渠道已停用', 'success')
    notifyChannelsChanged()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    load(true)
  } finally {
    toggling.value = ''
  }
}

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    // Each item carries its month snapshot (monthStats / monthStatsLoading); no extra stats call.
    const [list, recent] = await Promise.all([
      loadAll(),
      api.get<Schemas['RecentOutcomeData'][]>('/stats/recent-outcomes', { dimension: 'channel', limit: 10 }).catch(() => null),
    ])
    if (recent) outcomes.value = Object.fromEntries(recent.data.map((o) => [o.key, o]))
    channels.value = list
    if (!drag.dragging.value) order.value = list.map((c) => c.id)
    error.value = ''
  } catch (err) {
    if (!silent) error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}

async function loadProviders(): Promise<void> {
  try {
    const cat = (await api.get<Catalog>('/channel-catalog')).data
    providers.value = Object.fromEntries(cat.providers.map((p) => [p.id, { name: p.name, presets: Object.fromEntries(p.presets.map((x) => [x.id, x.name])) }]))
  } catch {
    /* names are cosmetic */
  }
}

function providerName(c: Channel): string {
  if (!c.providerId) return ''
  return providers.value[c.providerId]?.name || c.providerId
}
function presetName(c: Channel): string {
  if (!c.providerId || !c.providerPresetId) return ''
  return providers.value[c.providerId]?.presets[c.providerPresetId] || c.providerPresetId
}

const byId = computed(() => Object.fromEntries(channels.value.map((c) => [c.id, c])))
const ordered = computed(() => order.value.map((id) => byId.value[id]).filter((c): c is ChannelListItem => Boolean(c)))
const counts = computed(() => {
  const out = { all: channels.value.length, ok: 0, problem: 0, off: 0 }
  for (const c of channels.value) {
    if (!c.enabled) out.off++
    else if (['healthy', 'unknown'].includes(c.health)) out.ok++
    else out.problem++
  }
  return out
})
const filtered = computed(() => Boolean(q.value.trim()) || filter.value !== 'all')
const visible = computed(() => {
  const s = q.value.trim().toLowerCase()
  return ordered.value.filter((c) => {
    if (filter.value === 'off' && c.enabled) return false
    if (filter.value === 'ok' && (!c.enabled || !['healthy', 'unknown'].includes(c.health))) return false
    if (filter.value === 'problem' && (!c.enabled || ['healthy', 'unknown'].includes(c.health))) return false
    if (!s) return true
    return [c.name, c.baseUrl, providerName(c), ...c.models.map((m) => m.alias)].some((v) => v && v.toLowerCase().includes(s))
  })
})
const priorityOf = computed(() => new Map(ordered.value.map((c, i) => [c.id, i + 1])))
const selectedId = computed(() => (inspector.open && inspector.key.startsWith('channel:') ? inspector.key.slice(8) : ''))

// Drag a card by its handle; the new order is saved as soon as it is dropped.
const drag = useCardDrag(order, async (next, previous) => {
  savingOrder.value = true
  try {
    const res = await api.put<{ revision: string }>('/channels/order', { channelIds: next } satisfies Schemas['ChannelOrderRequest'], { ifMatch: orderRevision.value })
    orderRevision.value = res.data.revision
    channels.value = next.map((id) => byId.value[id]).filter((c): c is ChannelListItem => Boolean(c))
    toast('已保存渠道顺序', 'success')
  } catch (err) {
    order.value = previous
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if ((err as { code?: string }).code === 'REVISION_CONFLICT') load(true)
  } finally {
    savingOrder.value = false
  }
})

async function clearAllErrors(): Promise<void> {
  const ok = await confirmAction({
    title: '清除全部渠道的错误？',
    detail: '会清空所有渠道（含 OAuth 账号）的冷却与失败计数，被冷却的模型会立即重新参与调度。',
    confirmText: '全部清除',
    danger: true,
  })
  if (!ok) return
  busy.value = 'errors'
  const res = await runAction(() => api.post<{ affected: number }>('/channels/actions/clear-errors'))
  busy.value = ''
  if (res) toast(`已清除 ${res.affected} 条冷却记录`, 'success')
  load(true)
}
async function clearAllAffinity(): Promise<void> {
  const ok = await confirmAction({
    title: '清除全部亲和绑定？',
    detail: '会清空所有会话与渠道之间的亲和记录（服务端和客户端两类），之后的请求会重新选择渠道，可能导致缓存命中率暂时下降。',
    confirmText: '全部清除',
    danger: true,
  })
  if (!ok) return
  busy.value = 'affinity'
  const res = await runAction(() => api.post<{ affected: number }>('/channels/actions/clear-affinity'))
  busy.value = ''
  if (res) toast(`已清除 ${res.affected} 条亲和记录`, 'success')
  load(true)
}

async function onCreated(c: Channel): Promise<void> {
  await refreshCatalog(true).catch(() => undefined)
  await load(true)
  openChannel(c.id)
}

async function applyDeepLink(): Promise<void> {
  const id = route.query.channel
  if (typeof id !== 'string' || !id) return
  await refreshCatalog().catch(() => undefined)
  openChannel(id)
}

watch(() => channelBus.version, () => load(true))
watch(() => route.query.channel, applyDeepLink)
onMounted(() => {
  load()
  loadProviders()
  applyDeepLink()
  timer = window.setInterval(() => document.visibilityState === 'visible' && !drag.dragging.value && !savingOrder.value && load(true), 30_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <Page title="API 渠道" subtitle="接入第三方 API 服务商：每个渠道包含地址、密钥和一组模型。点击渠道查看详情、测试和修改；拖动右上角的把手调整顺序。" width="wide">
    <template #actions>
      <Btn :loading="busy === 'errors'" @click="clearAllErrors"><Eraser /> 清除全部错误</Btn>
      <Btn :loading="busy === 'affinity'" @click="clearAllAffinity"><Link2Off /> 清除全部亲和</Btn>
      <Btn variant="primary" @click="createOpen = true"><Plus /> 添加渠道</Btn>
    </template>
    <template #toolbar>
      <SearchField v-model="q" placeholder="搜索名称、地址或模型" />
      <Segmented
        v-model="filter"
        size="sm"
        :options="[
          { value: 'all', label: `全部 ${counts.all}` },
          { value: 'ok', label: `正常 ${counts.ok}` },
          { value: 'problem', label: `需关注 ${counts.problem}` },
          { value: 'off', label: `已停用 ${counts.off}` },
        ]"
      />
      <span class="grow" />
      <span v-if="savingOrder" class="saving">正在保存顺序…</span>
      <Segmented
        v-model="view"
        :options="[
          { value: 'table', label: '表格' },
          { value: 'cards', label: '卡片' },
        ]"
      />
      <Btn icon title="刷新" :loading="loading && !!channels.length" @click="load()"><RefreshCw /></Btn>
    </template>

    <p v-if="error" class="err"><TriangleAlert /> {{ error }} <Btn size="sm" @click="load()">重试</Btn></p>
    <div v-else-if="loading && !channels.length" class="grid">
      <Skeleton v-for="i in 6" :key="i" h="300px" r="14px" />
    </div>
    <Empty v-else-if="!channels.length" title="还没有 API 渠道" detail="添加一个第三方服务商的 API 渠道，就能把它的模型提供给下游使用。">
      <template #icon><Waypoints /></template>
      <Btn variant="primary" @click="createOpen = true"><Plus /> 添加渠道</Btn>
    </Empty>
    <Empty v-else-if="!visible.length" title="没有符合条件的渠道" detail="换个关键词或筛选条件试试" />
    <div v-else-if="view === 'table'" class="ctable">
      <div class="chead">
        <span />
        <span>渠道</span>
        <span>协议</span>
        <span>状态</span>
        <span>最近请求</span>
        <span>本月用量</span>
        <span>上游额度</span>
        <span class="r">操作</span>
      </div>
      <TransitionGroup tag="div" name="reorder" class="clist" data-drag-grid>
        <ChannelRow
          v-for="c in visible"
          :key="c.id"
          :channel="c"
          :priority="priorityOf.get(c.id) ?? 0"
          :provider-name="providerName(c)"
          :preset-name="presetName(c)"
          :outcomes="outcomes[c.id]?.outcomes"
          :last-at="outcomes[c.id]?.lastAt"
          :selected="selectedId === c.id"
          :dragging="drag.dragging.value === c.id"
          :draggable="!filtered && !savingOrder && channels.length > 1"
          :busy="toggling === c.id"
          @open="openChannel(c.id)"
          @dragstart="drag.start($event, c.id)"
          @toggle="toggleChannel(c, $event)"
        />
      </TransitionGroup>
    </div>
    <TransitionGroup v-else tag="div" name="reorder" class="grid" data-drag-grid>
      <ChannelCard
        v-for="c in visible"
        :key="c.id"
        :channel="c"
        :provider-name="providerName(c)"
        :preset-name="presetName(c)"
        :selected="selectedId === c.id"
        :dragging="drag.dragging.value === c.id"
        :draggable="!filtered && !savingOrder && channels.length > 1"
        @open="openChannel(c.id)"
        @dragstart="drag.start($event, c.id)"
      />
    </TransitionGroup>

    <ChannelCreateModal :open="createOpen" :existing-names="channels.map((c) => c.name)" @close="createOpen = false" @created="onCreated" />
  </Page>
</template>

<style scoped>
.ctable {
  --cols: 18px minmax(170px, 1.3fr) 128px minmax(120px, 0.9fr) 128px 190px minmax(160px, 1.2fr) 120px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chead {
  display: grid;
  grid-template-columns: var(--cols);
  column-gap: 14px;
  padding: 0 17px 0 9px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
}
.chead .r {
  text-align: right;
}
.clist {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
@media (max-width: 1279px) {
  .ctable {
    --cols: 18px minmax(160px, 1.3fr) minmax(110px, 0.9fr) 180px minmax(150px, 1.2fr) 120px;
  }
  .chead > :nth-child(3),
  .chead > :nth-child(5),
  .ctable :deep(.c-proto),
  .ctable :deep(.c-recent) {
    display: none;
  }
}
@media (max-width: 767px) {
  .ctable {
    --cols: 18px minmax(0, 1fr) auto;
  }
  .chead {
    display: none;
  }
  .ctable :deep(.c-state),
  .ctable :deep(.c-use),
  .ctable :deep(.c-quota) {
    display: none;
  }
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  align-items: stretch;
  gap: 12px;
}
.reorder-move {
  transition: transform 0.22s var(--ease-out);
}
.grow {
  flex: 1;
}
.saving {
  font-size: 12px;
  color: var(--text-3);
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
}
@container (max-width: 640px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
