<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, Inbox, Pause, Play, RefreshCw, Search, X } from '@lucide/vue'
import { api } from '@/api/client'
import { eventLink } from '@/api/events'
import type { RequestLogData, RequestLogFilterOptionsData } from '@/api/types'
import { live, seedFeed, qps } from '@/stores/live'
import { channelName } from '@/stores/catalog'
import { inspector } from '@/stores/ui'
import { integer, percent } from '@/utils/format'
import { openRequest } from './openRequest'
import RequestRow from './RequestRow.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import StatusDot from '@/components/ui/StatusDot.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

type Mode = 'live' | 'history'
type StatusFilter = '' | 'success' | 'error' | 'pending'

const mode = ref<Mode>('live')
const now = ref(Date.now())
const quick = ref('')
const statusFilter = ref<StatusFilter>('')
const pausedAt = ref(0)
const seedLoading = ref(false)
let ticker: number | undefined

onMounted(async () => {
  ticker = window.setInterval(() => (now.value = Date.now()), 500)
  if (!live.feed.length) {
    seedLoading.value = true
    try {
      const res = await api.get<RequestLogData[]>('/logs', { pageSize: 60, sort: 'createdAt', descending: true })
      seedFeed(res.data)
    } finally {
      seedLoading.value = false
    }
  }
})
onBeforeUnmount(() => {
  window.clearInterval(ticker)
  live.paused = false
})

const selectedId = computed(() => (inspector.open && inspector.key.startsWith('req:') ? inspector.key.slice(4) : ''))

const inFlight = computed(() =>
  [...live.inFlight.values()].sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1)),
)

const lastMinute = computed(() => {
  void qps.version
  const ok = qps.ok.slice(-60).reduce((a, b) => a + b, 0)
  const err = qps.err.slice(-60).reduce((a, b) => a + b, 0)
  return { total: Math.round(ok + err), err: Math.round(err) }
})

function matches(r: { status: string; requestedModel: string | null; finalModel: string | null; apiKeyName: string | null; channelId: string | null; id: string }): boolean {
  if (statusFilter.value && r.status !== statusFilter.value) return false
  const q = quick.value.trim().toLowerCase()
  if (!q) return true
  return [r.requestedModel, r.finalModel, r.apiKeyName, r.channelId ? channelName(r.channelId) : '', r.id]
    .filter(Boolean)
    .some((v) => String(v).toLowerCase().includes(q))
}

const feed = computed(() => live.feed.filter((r) => r.status !== 'pending' && matches(r)))
const missed = computed(() => (live.paused ? live.finished - pausedCount.value : 0))
const pausedCount = ref(0)

function togglePause(): void {
  live.paused = !live.paused
  if (live.paused) {
    pausedAt.value = Date.now()
    pausedCount.value = live.finished
  }
}

const statusOptions: { value: StatusFilter; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'success', label: '成功' },
  { value: 'error', label: '失败' },
]

// ---- history ----
const filterOptions = ref<RequestLogFilterOptionsData | null>(null)
const hist = reactive({
  items: [] as RequestLogData[],
  page: 1,
  total: 0,
  hasNext: false,
  loading: false,
  status: '' as StatusFilter,
  apiKey: '',
  model: '',
  channel: '',
  query: '',
})
const PAGE_SIZE = 50

async function loadHistory(): Promise<void> {
  hist.loading = true
  try {
    const res = await api.get<RequestLogData[]>('/logs', {
      status: hist.status || undefined,
      apiKey: hist.apiKey || undefined,
      model: hist.model || undefined,
      channel: hist.channel || undefined,
      query: hist.query.trim() || undefined,
      sort: 'createdAt',
      descending: true,
      page: hist.page,
      pageSize: PAGE_SIZE,
    })
    hist.items = res.data
    hist.total = res.meta.total ?? res.data.length
    hist.hasNext = Boolean(res.meta.hasNext)
  } finally {
    hist.loading = false
  }
}

watch(mode, async (m) => {
  if (m !== 'history') return
  if (!filterOptions.value) {
    api.get<RequestLogFilterOptionsData>('/logs/filter-options').then((r) => (filterOptions.value = r.data))
  }
  loadHistory()
})

let histTimer: number | undefined
watch(
  () => [hist.status, hist.apiKey, hist.model, hist.channel, hist.query],
  () => {
    window.clearTimeout(histTimer)
    histTimer = window.setTimeout(() => {
      hist.page = 1
      loadHistory()
    }, 250)
  },
)

function go(delta: number): void {
  hist.page = Math.max(1, hist.page + delta)
  loadHistory()
}

const hasHistFilter = computed(() => Boolean(hist.status || hist.apiKey || hist.model || hist.channel || hist.query))
function clearHist(): void {
  Object.assign(hist, { status: '', apiKey: '', model: '', channel: '', query: '' })
}
const pageCount = computed(() => Math.max(1, Math.ceil(hist.total / PAGE_SIZE)))
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <Segmented
        v-model="mode"
        :options="[
          { value: 'live', label: '实时' },
          { value: 'history', label: '历史' },
        ]"
      />
      <template v-if="mode === 'live'">
        <div class="field">
          <Search />
          <input v-model="quick" placeholder="筛选模型、Key、渠道" />
          <button v-if="quick" class="clear" @click="quick = ''"><X /></button>
        </div>
        <Segmented v-model="statusFilter" :options="statusOptions" size="sm" />
        <div class="spacer" />
        <Btn :variant="live.paused ? 'primary' : 'secondary'" @click="togglePause">
          <component :is="live.paused ? Play : Pause" />
          {{ live.paused ? (missed > 0 ? `继续（${missed} 条新请求）` : '继续') : '暂停' }}
        </Btn>
      </template>
      <template v-else>
        <div class="field">
          <Search />
          <input v-model="hist.query" placeholder="搜索请求 ID、错误信息" />
        </div>
        <select v-model="hist.status" class="select">
          <option value="">全部状态</option>
          <option value="success">成功</option>
          <option value="error">失败</option>
          <option value="pending">进行中</option>
        </select>
        <select v-model="hist.model" class="select">
          <option value="">全部模型</option>
          <option v-for="o in filterOptions?.models || []" :key="o.value" :value="o.value">{{ o.value }}（{{ o.count }}）</option>
        </select>
        <select v-model="hist.channel" class="select">
          <option value="">全部渠道</option>
          <option v-for="o in filterOptions?.channels || []" :key="o.value" :value="o.value">{{ channelName(o.value) }}（{{ o.count }}）</option>
        </select>
        <select v-model="hist.apiKey" class="select">
          <option value="">全部 Key</option>
          <option v-for="o in filterOptions?.apiKeys || []" :key="o.value" :value="o.value">{{ o.value }}（{{ o.count }}）</option>
        </select>
        <Btn v-if="hasHistFilter" variant="ghost" @click="clearHist"><X /> 清除</Btn>
        <div class="spacer" />
        <Btn icon title="刷新" :loading="hist.loading" @click="loadHistory"><RefreshCw /></Btn>
      </template>
    </div>

    <template v-if="mode === 'live'">
      <div class="stats">
        <div class="stat">
          <StatusDot :tone="eventLink.state === 'open' ? 'green' : 'orange'" :pulse="eventLink.state === 'open' && !live.paused" />
          <span>{{ eventLink.state === 'open' ? (live.paused ? '已暂停' : '实时接收中') : '正在连接实时推送…' }}</span>
        </div>
        <div class="stat"><span class="k">进行中</span><span class="v mono">{{ live.inFlight.size }}</span></div>
        <div class="stat"><span class="k">排队</span><span class="v mono">{{ live.snapshot?.channelTotals.waiting ?? 0 }}</span></div>
        <div class="stat"><span class="k">近 1 分钟</span><span class="v mono">{{ integer(lastMinute.total) }}</span></div>
        <div class="stat">
          <span class="k">近 1 分钟成功率</span>
          <span class="v mono">{{ lastMinute.total ? percent(lastMinute.total - lastMinute.err, lastMinute.total) : '—' }}</span>
        </div>
      </div>

      <section v-if="inFlight.length" class="block">
        <p class="block-title">正在处理 <span class="faint">{{ inFlight.length }}</span></p>
        <div class="table">
          <div class="rows">
            <RequestRow
              v-for="r in inFlight"
              :key="r.id"
              v-bind="r"
              :now="now"
              :selected="r.id === selectedId"
              @click="openRequest(r.id)"
            />
          </div>
        </div>
      </section>

      <section class="block">
        <p class="block-title">最近完成 <span class="faint">{{ feed.length }}</span></p>
        <div class="table">
          <div class="rows">
            <div class="head">
              <span class="c-time">时间</span><span class="c-status">状态</span><span class="c-key">Key</span>
              <span class="c-model">模型</span><span class="c-channel">渠道</span><span class="c-dur">耗时</span>
              <span class="c-tok">Token</span><span class="c-cost">费用</span>
            </div>
            <template v-if="seedLoading && !feed.length">
              <div v-for="i in 8" :key="i" class="sk-row"><Skeleton h="14px" /></div>
            </template>
            <TransitionGroup name="row">
              <RequestRow
                v-for="r in feed"
                :key="r.id"
                v-bind="r"
                :fresh="now - r.receivedAt < 1500"
                :selected="r.id === selectedId"
                @click="openRequest(r.id)"
              />
            </TransitionGroup>
            <Empty v-if="!seedLoading && !feed.length" title="还没有请求" :detail="quick || statusFilter ? '没有符合筛选条件的请求' : '新请求会实时出现在这里'">
              <template #icon><Inbox /></template>
            </Empty>
          </div>
        </div>
      </section>
    </template>

    <template v-else>
      <section class="block">
        <div class="table">
          <div class="rows">
            <div class="head">
              <span class="c-time">时间</span><span class="c-status">状态</span><span class="c-key">Key</span>
              <span class="c-model">模型</span><span class="c-channel">渠道</span><span class="c-dur">耗时</span>
              <span class="c-tok">Token</span><span class="c-cost">费用</span>
            </div>
            <template v-if="hist.loading && !hist.items.length">
              <div v-for="i in 10" :key="i" class="sk-row"><Skeleton h="14px" /></div>
            </template>
            <div :class="{ dim: hist.loading }">
              <RequestRow
                v-for="r in hist.items"
                :key="r.id"
                v-bind="r"
                :selected="r.id === selectedId"
                @click="openRequest(r.id)"
              />
            </div>
            <Empty v-if="!hist.loading && !hist.items.length" title="没有找到请求" detail="换个筛选条件试试">
              <template #icon><Inbox /></template>
            </Empty>
          </div>
        </div>
        <div v-if="hist.total" class="pager">
          <span class="faint">共 {{ integer(hist.total) }} 条 · 第 {{ hist.page }} / {{ integer(pageCount) }} 页</span>
          <Btn icon size="sm" :disabled="hist.page <= 1" @click="go(-1)"><ChevronLeft /></Btn>
          <Btn icon size="sm" :disabled="!hist.hasNext" @click="go(1)"><ChevronRight /></Btn>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px 20px 28px;
  min-height: 100%;
}
.toolbar {
  position: sticky;
  top: -16px;
  z-index: 5;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: -16px -20px 0;
  padding: 12px 20px;
  background: color-mix(in srgb, var(--bg) 70%, transparent);
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  border-bottom: 0.5px solid var(--hairline);
}
.spacer {
  flex: 1;
}
.field {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  width: 220px;
  padding: 0 8px;
  border-radius: var(--radius-sm);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.field svg {
  width: 13px;
  height: 13px;
  color: var(--text-3);
  flex-shrink: 0;
}
.field input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: none;
  font-size: 12.5px;
}
.clear {
  display: grid;
  color: var(--text-3);
}
.select {
  height: 28px;
  max-width: 180px;
  padding: 0 8px;
  border-radius: var(--radius-sm);
  border: 0;
  outline: 0;
  font-size: 12.5px;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.stat {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 12px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--surface);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.stat .k {
  color: var(--text-3);
}
.stat .v {
  font-weight: 600;
}
.block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.block-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  padding-left: 2px;
}
.table {
  container-type: inline-size;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  overflow: hidden;
}
.rows {
  --cols: 76px 64px minmax(64px, 0.8fr) minmax(150px, 2fr) minmax(110px, 1.3fr) 64px 100px 64px;
  --areas: 'time status key model channel dur tok cost';
}
.head {
  display: grid;
  grid-template-columns: var(--cols);
  grid-template-areas: var(--areas);
  column-gap: 12px;
  height: 32px;
  align-items: center;
  padding: 0 14px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 0.5px solid var(--border);
}
.head .c-time { grid-area: time; }
.head .c-status { grid-area: status; }
.head .c-key { grid-area: key; }
.head .c-model { grid-area: model; }
.head .c-channel { grid-area: channel; }
.head .c-dur { grid-area: dur; text-align: right; }
.head .c-tok { grid-area: tok; text-align: right; }
.head .c-cost { grid-area: cost; text-align: right; }
.rows :deep(.row:last-child) {
  border-bottom: 0;
}
.sk-row {
  padding: 12px 14px;
  border-bottom: 0.5px solid var(--hairline);
}
.dim {
  opacity: 0.5;
  transition: opacity 0.2s;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  font-size: 12px;
}
.row-enter-active {
  transition: opacity 0.3s, transform 0.3s var(--ease-out);
}
.row-enter-from {
  opacity: 0;
  transform: translateY(-6px);
}
@container (max-width: 1000px) {
  .rows {
    --cols: 72px 60px minmax(150px, 2fr) minmax(100px, 1.2fr) 60px 96px;
    --areas: 'time status model channel dur tok';
  }
  .rows :deep(.c-key),
  .rows :deep(.c-cost) {
    display: none;
  }
}
@container (max-width: 720px) {
  .rows {
    --cols: 10px minmax(0, 1fr) auto;
    --areas: 'status model time' '. channel dur';
  }
  .head {
    display: none;
  }
  .rows :deep(.row) {
    padding: 9px 14px;
    row-gap: 3px;
  }
  .rows :deep(.c-tok),
  .rows :deep(.st-label) {
    display: none;
  }
  .rows :deep(.c-channel) {
    font-size: 11.5px;
  }
}
@media (max-width: 767px) {
  .page {
    padding: 12px 12px 24px;
  }
  .toolbar {
    margin: -12px -12px 0;
    padding: 10px 12px;
    top: -12px;
  }
  .field {
    width: 100%;
    order: 5;
  }
  .select {
    flex: 1;
    max-width: none;
  }
}
</style>
