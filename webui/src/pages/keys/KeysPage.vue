<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Activity, Coins, Inbox, KeyRound, Layers, Plus, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api } from '@/api/client'
import { onEvent } from '@/api/events'
import type { ConcurrencyData } from '@/api/types'
import type { Schemas } from '@/api/client'
import { openApiKey } from '@/links'
import { live } from '@/stores/live'
import { inspector, toast } from '@/stores/ui'
import { integer } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Page from '@/components/ui/Page.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { cost3, tokens } from '@/pages/channels/meta'
import { useCardDrag } from '@/pages/channels/useCardDrag'
import KeyCard from './KeyCard.vue'
import KeyRow from './KeyRow.vue'
import StatTile from '@/components/ui/StatTile.vue'
import { deltas, loadCompare, sparks, type Compare } from '@/utils/compare'
import KeyCreateModal from './KeyCreateModal.vue'
import SecretModal from './SecretModal.vue'
import { keyBus, type ApiKey } from './meta'

type Filter = 'all' | 'active' | 'idle' | 'disabled'

const route = useRoute()
const keys = ref<ApiKey[]>([])
const order = ref<string[]>([])
const revision = ref('')
const loading = ref(true)
const error = ref('')
const q = ref('')
const filter = ref<Filter>('all')
const createOpen = ref(false)
const created = ref<{ name: string; secret: string } | null>(null)
const savingOrder = ref(false)
const runtime = ref<Record<string, { inFlight: number; waiting: number }>>({})
const listSummary = ref<Schemas['ApiKeyListSummaryData'] | null>(null)
const historyLoading = ref(false)
let timer: number | undefined

const outcomes = ref<Record<string, Schemas['RecentOutcomeData']>>({})
const view = ref<'table' | 'cards'>(localStorage.getItem('parrot.keys.view') === 'cards' ? 'cards' : 'table')
watch(view, (v) => localStorage.setItem('parrot.keys.view', v))

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  api
    .get<Schemas['RecentOutcomeData'][]>('/stats/recent-outcomes', { dimension: 'apiKey', limit: 10 })
    .then((r) => (outcomes.value = Object.fromEntries(r.data.map((o) => [o.key, o]))))
    .catch(() => undefined)
  loadCompare({ period: 'month' })
    .then((x) => (compare.value = x))
    .catch(() => undefined)
  try {
    const all: ApiKey[] = []
    let page = 1
    for (;;) {
      const res = await api.get<Schemas['ApiKeyListData']>('/api-keys', { page, pageSize: 200 })
      all.push(...res.data.items)
      // The header summary covers every key, so the first page's copy is enough.
      if (page === 1) {
        listSummary.value = res.data.summary ?? null
        historyLoading.value = res.data.historyLoading
      }
      revision.value = (res.meta as { revision?: string }).revision || ''
      if (!res.meta.hasNext) break
      page++
    }
    keys.value = all
    if (!drag.dragging.value) order.value = all.map((k) => k.keyId)
    error.value = ''
  } catch (err) {
    if (!silent) error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}

function applySnapshot(s: ConcurrencyData | null): void {
  if (!s) return
  runtime.value = Object.fromEntries(s.apiKeys.map((r) => [r.keyName, { inFlight: r.inFlight, waiting: r.waiting }]))
}
const off = onEvent('runtime.snapshot', (s) => applySnapshot(s))

const byId = computed(() => Object.fromEntries(keys.value.map((k) => [k.keyId, k])))
const ordered = computed(() => order.value.map((id) => byId.value[id]).filter((k): k is ApiKey => Boolean(k)))
// Counts and month totals come from the list's own `summary` (the bot's header block); the
// summary has no cost or in-flight numbers, so those two are summed from the items.
/** This month against last month up to the same point, with this month's daily sparklines. */
const compare = ref<Compare | null>(null)
const change = computed(() => deltas(compare.value, 'month'))
const trend = computed(() => sparks(compare.value))

const summary = computed(() => {
  const s = listSummary.value
  let cost = 0
  let inFlight = 0
  for (const k of keys.value) {
    cost += k.monthStats.costTicks
    inFlight += runtime.value[k.name]?.inFlight ?? k.limiter.inFlight
  }
  return {
    all: s?.total ?? keys.value.length,
    active: s?.active ?? 0,
    idle: s?.idle ?? 0,
    disabled: s?.disabled ?? 0,
    calls: s?.monthRequests ?? 0,
    prompt: s?.monthPromptTokens ?? 0,
    output: s?.monthOutputTokens ?? 0,
    cost,
    inFlight,
  }
})
const filtered = computed(() => Boolean(q.value.trim()) || filter.value !== 'all')
const visible = computed(() => {
  const s = q.value.trim().toLowerCase()
  return ordered.value.filter((k) => {
    if (filter.value === 'disabled' && k.enabled) return false
    if (filter.value === 'active' && !k.monthStats.total) return false
    if (filter.value === 'idle' && k.monthStats.total) return false
    return !s || k.name.toLowerCase().includes(s) || k.allowedModels.some((m) => m.toLowerCase().includes(s))
  })
})
const selectedId = computed(() => (inspector.open && inspector.key.startsWith('apikey:') ? inspector.key.slice(7) : ''))

const drag = useCardDrag(order, async (next, previous) => {
  savingOrder.value = true
  try {
    const res = await api.put<{ revision: string }>('/api-keys/order', { keyIds: next } satisfies Schemas['ApiKeyOrderRequest'], { ifMatch: revision.value })
    revision.value = res.data.revision
    keys.value = next.map((id) => byId.value[id]).filter((k): k is ApiKey => Boolean(k))
    toast('已保存 API Key 顺序', 'success')
  } catch (err) {
    order.value = previous
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if ((err as { code?: string }).code === 'REVISION_CONFLICT') load(true)
  } finally {
    savingOrder.value = false
  }
})

function onCreated(k: ApiKey, secret: string): void {
  created.value = { name: k.name, secret }
  load(true)
}
function closeSecret(): void {
  const name = created.value?.name
  created.value = null
  if (name) openApiKey(name)
}

function applyDeepLink(): void {
  const id = route.query.key
  if (typeof id === 'string' && id) openApiKey(id)
}

watch(() => keyBus.version, () => load(true))
watch(() => route.query.key, applyDeepLink)
onMounted(() => {
  load()
  applySnapshot(live.snapshot)
  applyDeepLink()
  timer = window.setInterval(() => document.visibilityState === 'visible' && !drag.dragging.value && !savingOrder.value && load(true), 30_000)
})
onBeforeUnmount(() => {
  off()
  window.clearInterval(timer)
})
</script>

<template>
  <Page title="下游 API Key" subtitle="分发给客户端的访问密钥。卡片上可以直接复制完整 Key；拖动右上角的把手调整顺序。" width="wide">
    <template #actions>
      <Btn variant="primary" @click="createOpen = true"><Plus /> 新建 Key</Btn>
    </template>
    <template #toolbar>
      <SearchField v-model="q" placeholder="搜索名称或模型" />
      <Segmented
        v-model="filter"
        size="sm"
        :options="[
          { value: 'all', label: `全部 ${summary.all}` },
          { value: 'active', label: `活跃 ${summary.active}` },
          { value: 'idle', label: `闲置 ${summary.idle}` },
          { value: 'disabled', label: `停用 ${summary.disabled}` },
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
      <Btn icon title="刷新" :loading="loading && !!keys.length" @click="load()"><RefreshCw /></Btn>
    </template>

    <section v-if="keys.length" class="kpis">
      <StatTile dense label="本月请求" :icon="Inbox" tone="blue" :value="integer(summary.calls)" :sub="`${summary.active} 个活跃 · ${summary.idle} 个闲置`" :delta="change.requests" :spark="trend.requests" />
      <StatTile dense label="读入 ↑ / 写出 ↓" :icon="Layers" tone="purple" :value="`${tokens(summary.prompt)} / ${tokens(summary.output)}`" sub="本月 Token" :delta="change.tokens" :spark="trend.tokens" />
      <StatTile dense label="本月费用" :icon="Coins" tone="orange" :value="cost3(summary.cost)" :sub="`共 ${summary.all} 个 Key`" :delta="change.cost" :spark="trend.cost" />
      <StatTile dense label="进行中" :icon="Activity" tone="green" :value="summary.inFlight" :sub="summary.disabled ? `${summary.disabled} 个已停用` : '实时'" />
    </section>

    <p v-if="error" class="err"><TriangleAlert /> {{ error }} <Btn size="sm" @click="load()">重试</Btn></p>
    <div v-else-if="loading && !keys.length" class="grid">
      <Skeleton v-for="i in 6" :key="i" h="230px" r="14px" />
    </div>
    <Empty v-else-if="!keys.length" title="还没有 API Key" detail="新建一个 Key 分发给客户端，客户端用它来调用 Parrot。">
      <template #icon><KeyRound /></template>
      <Btn variant="primary" @click="createOpen = true"><Plus /> 新建 Key</Btn>
    </Empty>
    <Empty v-else-if="!visible.length" title="没有符合条件的 Key" detail="换个关键词或筛选条件试试" />
    <div v-else-if="view === 'table'" class="ktable">
      <div class="khead">
        <span /><span />
        <span>API Key</span>
        <span>状态 / 权限</span>
        <span>本月调用 · 成功/失败</span>
        <span>成功率</span>
        <span>Token</span>
        <span>费用</span>
        <span>最近状态</span>
        <span class="r">操作</span>
      </div>
      <TransitionGroup tag="div" name="reorder" class="klist" data-drag-grid>
        <KeyRow
          v-for="k in visible"
          :key="k.keyId"
          :k="k"
          :live="runtime[k.name]"
          :history="historyLoading ? null : k.historyTotal ?? null"
          :outcomes="outcomes[k.name]?.outcomes"
          :last-at="outcomes[k.name]?.lastAt"
          :selected="selectedId === k.keyId"
          :dragging="drag.dragging.value === k.keyId"
          :draggable="!filtered && !savingOrder && keys.length > 1"
          @open="openApiKey(k.keyId)"
          @dragstart="drag.start($event, k.keyId)"
        />
      </TransitionGroup>
    </div>
    <TransitionGroup v-else tag="div" name="reorder" class="grid" data-drag-grid>
      <KeyCard
        v-for="k in visible"
        :key="k.keyId"
        :k="k"
        :live="runtime[k.name]"
        :history="historyLoading ? null : k.historyTotal ?? null"
        :selected="selectedId === k.keyId"
        :dragging="drag.dragging.value === k.keyId"
        :draggable="!filtered && !savingOrder && keys.length > 1"
        @open="openApiKey(k.keyId)"
        @dragstart="drag.start($event, k.keyId)"
      />
    </TransitionGroup>

    <KeyCreateModal :open="createOpen" :existing="keys.map((k) => k.name)" @close="createOpen = false" @created="onCreated" />
    <SecretModal :secret="created?.secret || null" :name="created?.name || ''" @close="closeSecret" />
  </Page>
</template>

<style scoped>
.kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.ktable {
  --cols: 18px 14px minmax(220px, 1.6fr) minmax(130px, 1fr) 120px 72px 110px 96px 104px 76px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.khead {
  display: grid;
  grid-template-columns: var(--cols);
  column-gap: 14px;
  padding: 0 17px 0 9px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
}
.khead .r {
  text-align: right;
}
.klist {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
@media (max-width: 1279px) {
  .ktable {
    --cols: 18px 14px minmax(200px, 1.6fr) minmax(110px, 1fr) 110px 72px 96px 76px;
  }
  .khead > :nth-child(7),
  .khead > :nth-child(9),
  .ktable :deep(.krow > :nth-child(7)),
  .ktable :deep(.c-recent) {
    display: none;
  }
}
@media (max-width: 767px) {
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .ktable {
    --cols: 18px 14px minmax(0, 1fr) auto;
  }
  .khead {
    display: none;
  }
  .ktable :deep(.c-state),
  .ktable :deep(.c-num),
  .ktable :deep(.c-recent) {
    display: none;
  }
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
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
.sum {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  overflow: hidden;
}
.sum > div {
  padding: 10px 16px;
  border-right: 0.5px solid var(--hairline);
}
.sum > div:last-child {
  border-right: 0;
}
.k {
  font-size: 11px;
  color: var(--text-3);
}
.v {
  margin-top: 2px;
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 650;
}
.dim {
  color: var(--text-3);
  font-weight: 400;
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
  .sum {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .sum > div:nth-child(2) {
    border-right: 0;
  }
  .sum > div:nth-child(-n + 2) {
    border-bottom: 0.5px solid var(--hairline);
  }
}
</style>
