<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowRight, ChevronLeft, ChevronRight, ChevronsLeft, CircleCheck, CircleX, Coins, DatabaseZap, Download, Inbox,
  Layers, Pause, Play, RefreshCw, Search, Upload, X,
} from '@lucide/vue'
import { api } from '@/api/client'
import { eventLink, onEvent } from '@/api/events'
import type { RequestLogData, RequestLogFilterOptionsData } from '@/api/types'
import { live, seedFeed, type LiveRequest } from '@/stores/live'
import { channelName } from '@/stores/catalog'
import { inspector } from '@/stores/ui'
import { compact, dateTime, integer, usd } from '@/utils/format'
import { openRequest } from './openRequest'
import { rowChannelName, type LogRow } from './logView'
import { loadScope, periodStart, PERIODS, type Period, type ScopeResult } from './scopeStats'
import { cancelledOf, pctText, pendingOf } from '@/pages/overview/metrics'
import { deltas, previousLabel, sparks } from '@/utils/compare'
import RequestRow from './RequestRow.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import StatTile from '@/components/ui/StatTile.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'

type Mode = 'live' | 'history'
type StatusFilter = '' | 'success' | 'error' | 'pending' | 'cancelled'

/** The live view keeps exactly this many rows; older ones live in the history view. */
const LIVE_LIMIT = 30
const PAGE_SIZE = 50

const route = useRoute()
const router = useRouter()
const mode = ref<Mode>(route.query.mode === 'history' ? 'history' : 'live')
const now = ref(Date.now())
const quick = ref('')
const statusFilter = ref<StatusFilter>('')
const seedLoading = ref(false)
const seedTotal = ref(0)
let ticker: number | undefined

// request.started / request.updated carry facts the shared live store drops; keep them for in-flight rows.
interface StartedExtra {
  reasoningEffort: string | null
  fastMode: boolean | null
  isStream: boolean
  proxyName: string | null
}
const startedExtras = reactive(new Map<string, StartedExtra>())
const offStarted = onEvent('request.started', (e) => {
  startedExtras.set(e.id, { reasoningEffort: e.reasoningEffort, fastMode: e.fastMode, isStream: e.isStream, proxyName: null })
  if (startedExtras.size > 200) startedExtras.delete(startedExtras.keys().next().value as string)
})
const offUpdated = onEvent('request.updated', (e) => {
  const cur = startedExtras.get(e.id)
  if (cur && e.proxyName) startedExtras.set(e.id, { ...cur, proxyName: e.proxyName })
})

onMounted(async () => {
  ticker = window.setInterval(() => (now.value = Date.now()), 500)
  seedLoading.value = !live.feed.length
  try {
    // More than the 30 shown rows, so the "recent status" bars have history from the start.
    const res = await api.get<RequestLogData[]>('/logs', { pageSize: 200, sort: 'createdAt', descending: true })
    seedTotal.value = res.meta.total ?? res.data.length
    seedFeed(res.data)
  } catch {
    /* rows still arrive from events */
  } finally {
    seedLoading.value = false
  }
})
onBeforeUnmount(() => {
  window.clearInterval(ticker)
  window.clearInterval(scopeTimer)
  offStarted()
  offUpdated()
  offFinished()
  live.paused = false
})

const selectedId = computed(() => (inspector.open && inspector.key.startsWith('req:') ? inspector.key.slice(4) : ''))

/** Live-store item → list row. Finished rows carry the full log; in-flight rows only what events said. */
function toRow(r: LiveRequest): LogRow {
  if (r.log && r.status !== 'pending') return r.log as LogRow
  const extra = startedExtras.get(r.id)
  return {
    id: r.id,
    status: r.status,
    createdAt: r.createdAt,
    apiKeyName: r.apiKeyName,
    requestedModel: r.requestedModel,
    finalModel: r.finalModel,
    channelId: r.channelId,
    protocol: r.protocol,
    ingressProtocol: r.protocol,
    retryCount: r.retryCount,
    durationMilliseconds: r.durationMilliseconds,
    inputTokens: r.inputTokens,
    outputTokens: r.outputTokens,
    costTicks: r.costTicks,
    billing: { costTicks: r.costTicks, actualCostTicks: 0, estimatedCostTicks: 0, actualCostedSuccess: 0, estimatedCostedSuccess: 0, costedSuccess: 0, unpricedSuccess: 0 },
    error: r.error,
    revision: '',
    isStream: extra?.isStream ?? r.isStream,
    reasoningEffort: extra?.reasoningEffort ?? null,
    speedMode: extra?.fastMode ? 'fast' : null,
    proxyName: extra?.proxyName ?? null,
    cacheCreationTokens: 0,
    cacheReadTokens: 0,
    promptTokens: 0,
    affinityHit: false,
    localSearchCount: 0,
  }
}

function matches(r: LogRow): boolean {
  if (statusFilter.value && r.status !== statusFilter.value) return false
  const q = quick.value.trim().toLowerCase()
  if (!q) return true
  return [r.requestedModel, r.finalModel, r.apiKeyName, r.channelId ? rowChannelName(r) : '', r.id]
    .filter(Boolean)
    .some((v) => String(v).toLowerCase().includes(q))
}

const byNewest = (a: LogRow, b: LogRow) => ((a.createdAt || '') < (b.createdAt || '') ? 1 : (a.createdAt || '') > (b.createdAt || '') ? -1 : 0)
const feedRows = computed(() => live.feed.map(toRow).sort(byNewest))
const liveMatching = computed(() => feedRows.value.filter(matches))
const liveRows = computed(() => liveMatching.value.slice(0, LIVE_LIMIT))
const hasMore = computed(() => liveMatching.value.length > LIVE_LIMIT || (!quick.value && !statusFilter.value && seedTotal.value > LIVE_LIMIT))
const receivedAt = computed(() => {
  const m = new Map<string, number>()
  for (const r of live.feed) m.set(r.id, r.receivedAt)
  return m
})

/** Per row: the last 10 finished outcomes of its channel up to and including that row. */
function recentByRow(rowsNewestFirst: LogRow[]): Map<string, string[]> {
  const out = new Map<string, string[]>()
  const perChannel = new Map<string, string[]>()
  for (let i = rowsNewestFirst.length - 1; i >= 0; i--) {
    const r = rowsNewestFirst[i]
    const key = r.channelId || ''
    const list = perChannel.get(key) || []
    if (r.status !== 'pending') {
      list.push(r.status)
      if (list.length > 10) list.shift()
      perChannel.set(key, list)
    }
    out.set(r.id, key ? [...list] : [])
  }
  return out
}
const liveRecent = computed(() => recentByRow(feedRows.value))

const pausedCount = ref(0)
const missed = computed(() => (live.paused ? live.finished - pausedCount.value : 0))
function togglePause(): void {
  live.paused = !live.paused
  if (live.paused) pausedCount.value = live.finished
}

const statusOptions: { value: StatusFilter; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'pending', label: '进行中' },
  { value: 'success', label: '成功' },
  { value: 'error', label: '失败' },
]

function openHistoryFromLive(): void {
  router.push({ path: '/live', query: { mode: 'history', ...(statusFilter.value && statusFilter.value !== 'pending' ? { status: statusFilter.value } : {}) } })
}

// ---- history ----
const filterOptions = ref<RequestLogFilterOptionsData | null>(null)
const hist = reactive({
  items: [] as LogRow[],
  page: 1,
  total: 0,
  hasNext: false,
  loading: false,
  loaded: false,
  period: 'today' as Period | 'custom',
  status: '' as StatusFilter,
  apiKey: '',
  model: '',
  channel: '',
  query: '',
  startedAt: '',
  endedAt: '',
})
const pageInput = ref('')
let histSeq = 0

function applyPeriod(p: Period): void {
  const start = periodStart(p)
  hist.period = p
  hist.startedAt = start ? new Date(start).toISOString() : ''
  hist.endedAt = ''
}

function applyRouteQuery(): void {
  const q = route.query
  if (q.mode !== 'history') return
  const str = (v: unknown) => (typeof v === 'string' ? v : '')
  Object.assign(hist, {
    status: (str(q.status) === 'pending' ? '' : str(q.status)) as StatusFilter,
    model: str(q.model),
    channel: str(q.channel),
    apiKey: str(q.apiKey),
    query: str(q.query),
    startedAt: str(q.from),
    endedAt: str(q.to),
    page: 1,
  })
  // A deep link with a time window keeps it; one without (from a model / channel / key) shows all time.
  if (hist.startedAt || hist.endedAt) hist.period = 'custom'
  else if (hist.model || hist.channel || hist.apiKey || hist.query || hist.status) applyPeriod('lifetime')
  else applyPeriod('today')
  if (mode.value === 'history') loadHistory()
  else mode.value = 'history'
}
watch(() => route.query, applyRouteQuery)
onMounted(applyRouteQuery)

function filterKey(): string {
  return JSON.stringify([hist.status, hist.apiKey, hist.model, hist.channel, hist.query.trim(), hist.startedAt, hist.endedAt])
}
let loadedKey = ''

function ensureFilterOptions(): void {
  if (filterOptions.value) return
  api.get<RequestLogFilterOptionsData>('/logs/filter-options').then((r) => (filterOptions.value = r.data)).catch(() => undefined)
}

async function loadHistory(): Promise<void> {
  const seq = ++histSeq
  loadedKey = filterKey()
  window.clearTimeout(histTimer)
  hist.loading = true
  loadScopeStats()
  try {
    const res = await api.get<RequestLogData[]>('/logs', {
      // History lists finished requests only; running ones live in the 实时 tab.
      status: hist.status || FINISHED,
      apiKey: hist.apiKey || undefined,
      model: hist.model || undefined,
      channel: hist.channel || undefined,
      query: hist.query.trim() || undefined,
      startedAt: hist.startedAt || undefined,
      endedAt: hist.endedAt || undefined,
      sort: 'createdAt',
      descending: true,
      page: hist.page,
      pageSize: PAGE_SIZE,
    })
    if (seq !== histSeq) return
    hist.items = res.data as LogRow[]
    hist.total = res.meta.total ?? res.data.length
    hist.hasNext = Boolean(res.meta.hasNext)
  } finally {
    if (seq === histSeq) {
      hist.loading = false
      hist.loaded = true
    }
  }
}
const histRecent = computed(() => recentByRow(hist.items))

watch(mode, (m) => {
  loadScopeStats()
  if (m === 'live') {
    if (route.query.mode === 'history') router.replace({ path: '/live' })
    return
  }
  ensureFilterOptions()
  loadHistory()
})
onMounted(() => {
  if (mode.value === 'history') ensureFilterOptions()
})

let histTimer: number | undefined
// Filter edits reload page 1 (debounced); a route-driven reload already used the same filters.
watch(
  () => [hist.status, hist.apiKey, hist.model, hist.channel, hist.query, hist.startedAt, hist.endedAt],
  () => {
    if (filterKey() === loadedKey) return
    window.clearTimeout(histTimer)
    histTimer = window.setTimeout(() => {
      hist.page = 1
      loadHistory()
    }, 250)
  },
)

const pageCount = computed(() => Math.max(1, Math.ceil(hist.total / PAGE_SIZE)))
function goPageNo(p: number): void {
  const next = Math.min(Math.max(1, Math.round(p) || 1), pageCount.value)
  if (next === hist.page) return
  hist.page = next
  loadHistory()
  document.querySelector('.content')?.scrollTo({ top: 0 })
}
function submitPage(): void {
  goPageNo(Number(pageInput.value))
  pageInput.value = ''
}

const hasHistFilter = computed(() => Boolean(hist.status || hist.apiKey || hist.model || hist.channel || hist.query))
function clearHist(): void {
  Object.assign(hist, { status: '', apiKey: '', model: '', channel: '', query: '' })
  if (hist.period === 'custom') applyPeriod('today')
  if (route.query.mode === 'history' && Object.keys(route.query).length > 1) router.replace({ path: '/live', query: { mode: 'history' } })
}
function pickPeriod(p: Period | 'custom'): void {
  if (p !== 'custom') applyPeriod(p)
}

type Opt = { value: string; label: string; hint?: string }
function optionsOf(list: { value: string; count: number }[] | undefined, current: string, all: string, label = (v: string) => v): Opt[] {
  const out: Opt[] = [{ value: '', label: all }]
  for (const o of list || []) out.push({ value: o.value, label: label(o.value), hint: integer(o.count) })
  if (current && !out.some((o) => o.value === current)) out.splice(1, 0, { value: current, label: label(current) })
  return out
}
const statusSelect: Opt[] = [
  { value: '', label: '全部状态' },
  { value: 'success', label: '成功' },
  { value: 'error', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]
const FINISHED = ['success', 'error', 'cancelled']
const modelOptions = computed(() => optionsOf(filterOptions.value?.models, hist.model, '全部模型'))
const channelOptions = computed(() => optionsOf(filterOptions.value?.channels, hist.channel, '全部渠道', channelName))
const keyOptions = computed(() => optionsOf(filterOptions.value?.apiKeys, hist.apiKey, '全部 Key'))
const periodOptions = computed(() => [...PERIODS, ...(hist.period === 'custom' ? [{ value: 'custom' as const, label: '自定义' }] : [])])

// ---- totals above the list ----
const scope = ref<ScopeResult | null>(null)
const scopeLoading = ref(false)
const liveDelta = reactive({ total: 0, success: 0, error: 0, prompt: 0, output: 0, cacheRead: 0, cost: 0 })
let scopeSeq = 0
let scopeTimer: number | undefined
async function loadScopeStats(): Promise<void> {
  const seq = ++scopeSeq
  scopeLoading.value = true
  try {
    const res =
      mode.value === 'live'
        ? await loadScope('today', {})
        : await loadScope(hist.period, { channel: hist.channel, model: hist.model, apiKey: hist.apiKey }, { from: hist.startedAt, to: hist.endedAt })
    if (seq !== scopeSeq) return
    scope.value = res
    Object.assign(liveDelta, { total: 0, success: 0, error: 0, prompt: 0, output: 0, cacheRead: 0, cost: 0 })
  } catch {
    if (seq === scopeSeq) scope.value = null
  } finally {
    if (seq === scopeSeq) scopeLoading.value = false
  }
}
onMounted(() => {
  if (mode.value === 'live') loadScopeStats()
  scopeTimer = window.setInterval(() => document.visibilityState === 'visible' && mode.value === 'live' && loadScopeStats(), 30_000)
})
const offFinished = onEvent('request.finished', ({ log }: { log: RequestLogData }) => {
  if (mode.value !== 'live') return
  liveDelta.total += 1
  if (log.status === 'success') liveDelta.success += 1
  else if (log.status === 'error') liveDelta.error += 1
  liveDelta.prompt += log.promptTokens
  liveDelta.output += log.outputTokens
  liveDelta.cacheRead += log.cacheReadTokens
  liveDelta.cost += log.billing?.costTicks ?? log.costTicks
})
/** Where the requests that are neither 成功 nor 失败 went, so the three tiles add up. */
function requestNote(k: { rate: number | null; cancelled: number; pending: number }): string {
  const parts: string[] = []
  if (k.cancelled) parts.push(`取消 ${integer(k.cancelled)}`)
  if (k.pending) parts.push(`进行中 ${integer(k.pending)}`)
  return parts.length ? parts.join(' · ') : `成功率 ${pctText(k.rate)}`
}
const kpi = computed(() => {
  const m = scope.value?.metrics
  if (!m) return null
  const d = mode.value === 'live' ? liveDelta : { total: 0, success: 0, error: 0, prompt: 0, output: 0, cacheRead: 0, cost: 0 }
  const total = m.total + d.total
  const success = m.successCount + d.success
  const prompt = m.inputTokens + m.cacheReadTokens + m.cacheCreationTokens + d.prompt
  const cacheRead = m.cacheReadTokens + d.cacheRead
  return {
    total,
    success,
    // From the snapshot only: live deltas carry no cancellations.
    cancelled: cancelledOf(m),
    pending: pendingOf(m),
    error: m.errorCount + d.error,
    rate: total ? success / total : null,
    prompt,
    output: m.outputTokens + d.output,
    cacheRead,
    cacheShare: prompt ? cacheRead / prompt : null,
    cost: m.costTicks + d.cost,
    unpriced: m.unpricedSuccess,
  }
})
const change = computed(() => deltas(scope.value?.compare || null, scope.value?.period))
const trend = computed(() => sparks(scope.value?.compare || null))
function vsText(s: ScopeResult | null): string {
  if (!s?.compare) return ''
  const against = previousLabel(s.compare, s.period)
  return s.compare.previousComplete ? `；右上角是对比${against}的变化` : `；${against}超出了日志保留范围，不显示变化`
}
const scopeNote = computed(() => {
  if (mode.value === 'live') return `今日统计${vsText(scope.value)}`
  if (hist.period === 'custom' && !hist.startedAt) return '没有开始时间的自定义时间段没有汇总统计，只显示请求列表'
  const p = hist.period === 'custom' ? '自定义时间段' : (PERIODS.find((x) => x.value === hist.period)?.label ?? '')
  const s = scope.value
  if (!s) return `${p}`
  return `${p} · ${s.label}${s.partial ? '（汇总只按这一项筛选）' : ''}${hist.status || hist.query ? '；状态和搜索只作用于列表' : ''}${vsText(s)}`
})
</script>

<template>
  <div class="page">
    <section class="bar">
      <div class="bar-row">
        <Segmented
          v-model="mode"
          :options="[
            { value: 'live', label: '实时' },
            { value: 'history', label: '历史' },
          ]"
        />
        <Segmented v-if="mode === 'history'" :model-value="hist.period" :options="periodOptions" @update:model-value="pickPeriod" />
        <label class="field">
          <Search />
          <input v-if="mode === 'live'" v-model="quick" placeholder="筛选模型、Key、渠道、请求 ID" />
          <input v-else v-model="hist.query" placeholder="搜索请求 ID、错误信息" />
          <button v-if="mode === 'live' ? quick : hist.query" class="clear" aria-label="清除" @click.prevent="mode === 'live' ? (quick = '') : (hist.query = '')"><X /></button>
        </label>
        <template v-if="mode === 'live'">
          <Segmented v-model="statusFilter" :options="statusOptions" />
          <span class="spacer" />
          <span class="conn" :class="eventLink.state === 'open' && !live.paused ? 'on' : ''">
            <i />{{ eventLink.state === 'open' ? (live.paused ? '已暂停' : '实时接收中') : '正在连接…' }}
          </span>
          <Btn :variant="live.paused ? 'primary' : 'secondary'" @click="togglePause">
            <component :is="live.paused ? Play : Pause" />
            {{ live.paused ? (missed > 0 ? `继续（${missed} 条新请求）` : '继续') : '暂停' }}
          </Btn>
        </template>
        <template v-else>
          <span class="spacer" />
          <Btn v-if="hasHistFilter || hist.period === 'custom'" variant="ghost" @click="clearHist"><X /> 清空筛选</Btn>
          <Btn variant="secondary" :loading="hist.loading" @click="loadHistory"><RefreshCw /> 刷新</Btn>
        </template>
      </div>
      <div v-if="mode === 'history'" class="bar-row selects">
        <Select v-model="hist.status" :options="statusSelect" />
        <Select v-model="hist.model" :options="modelOptions" searchable />
        <Select v-model="hist.channel" :options="channelOptions" searchable />
        <Select v-model="hist.apiKey" :options="keyOptions" searchable />
        <span v-if="hist.period === 'custom'" class="range">
          {{ hist.startedAt ? dateTime(hist.startedAt) : '最早' }} ~ {{ hist.endedAt ? dateTime(hist.endedAt) : '现在' }}
        </span>
      </div>
    </section>

    <section class="kpis">
      <StatTile dense label="请求" :icon="Inbox" tone="blue" :loading="scopeLoading" :value="kpi ? integer(kpi.total) : null" :sub="kpi ? requestNote(kpi) : scopeNote" :delta="change.requests" :spark="trend.requests" />
      <StatTile dense label="成功" :icon="CircleCheck" tone="green" :loading="scopeLoading" :value="kpi ? integer(kpi.success) : null" value-tone="green" :sub="kpi ? `成功率 ${pctText(kpi.rate, 2)}` : ''" :delta="change.success" :spark="trend.success" />
      <StatTile dense label="失败" :icon="CircleX" tone="red" :loading="scopeLoading" :value="kpi ? integer(kpi.error) : null" :value-tone="kpi && kpi.error ? 'red' : ''" :sub="kpi && kpi.total ? pctText(kpi.error / kpi.total, 2) : ''" :delta="change.errors" :spark="trend.errors" />
      <StatTile dense label="金额" :icon="Coins" tone="orange" :loading="scopeLoading" :value="kpi ? usd(kpi.cost) : null" :sub="kpi?.unpriced ? `未计价 ${integer(kpi.unpriced)} 次` : ''" :delta="change.cost" :spark="trend.cost" />
      <StatTile dense label="Token" :icon="Layers" tone="purple" :loading="scopeLoading" :value="kpi ? compact(kpi.prompt + kpi.output) : null" :sub="kpi ? `读入 + 写出` : ''" :delta="change.tokens" :spark="trend.tokens" />
      <StatTile dense label="读入 ↑" :icon="Download" tone="teal" :loading="scopeLoading" :value="kpi ? compact(kpi.prompt) : null" :sub="kpi && kpi.prompt + kpi.output ? `占比 ${pctText(kpi.prompt / (kpi.prompt + kpi.output))}` : ''" :delta="change.prompt" :spark="trend.prompt" />
      <StatTile dense label="写出 ↓" :icon="Upload" tone="indigo" :loading="scopeLoading" :value="kpi ? compact(kpi.output) : null" :sub="kpi && kpi.prompt + kpi.output ? `占比 ${pctText(kpi.output / (kpi.prompt + kpi.output))}` : ''" :delta="change.output" :spark="trend.output" />
      <StatTile dense label="缓存读" :icon="DatabaseZap" tone="cyan" :loading="scopeLoading" :value="kpi ? compact(kpi.cacheRead) : null" :sub="kpi ? `占读入 ${pctText(kpi.cacheShare)}` : ''" :delta="change.cacheRead" :spark="trend.cacheRead" />
    </section>
    <p class="note">{{ scopeNote }}</p>

    <section class="table">
      <div class="head">
        <span class="h-time">时间</span>
        <span class="h-key">API Key</span>
        <span class="h-channel">账户 / 渠道</span>
        <span class="h-model">模型</span>
        <span class="h-recent">最近状态</span>
        <span class="h-status">状态</span>
        <span class="h-tok">Token ↑/↓ · 缓存</span>
        <span class="h-cost">金额</span>
        <span class="h-lat">延迟</span>
      </div>

      <template v-if="mode === 'live'">
        <template v-if="seedLoading && !liveRows.length">
          <div v-for="i in 6" :key="i" class="ph-row" />
        </template>
        <RequestRow
          v-for="r in liveRows"
          :key="r.id"
          :row="r"
          :now="now"
          :recent="liveRecent.get(r.id)"
          :fresh="now - (receivedAt.get(r.id) || 0) < 1500"
          :selected="r.id === selectedId"
          @click="openRequest(r.id)"
        />
        <Empty v-if="!seedLoading && !liveRows.length" title="还没有请求" :detail="quick || statusFilter ? '没有符合筛选条件的请求' : '新请求会实时出现在这里'">
          <template #icon><Inbox /></template>
        </Empty>
        <button v-if="hasMore" class="more" @click="openHistoryFromLive">
          <span>实时视图只保留最近 {{ LIVE_LIMIT }} 条</span>
          <span class="go">去历史请求查看更多 <ArrowRight /></span>
        </button>
      </template>

      <template v-else>
        <template v-if="hist.loading && !hist.items.length">
          <div v-for="i in 6" :key="i" class="ph-row" />
        </template>
        <div class="rows" :class="{ dim: hist.loading && hist.items.length }">
          <RequestRow
            v-for="r in hist.items"
            :key="r.id"
            :row="r"
            with-date
            :recent="histRecent.get(r.id)"
            :selected="r.id === selectedId"
            @click="openRequest(r.id)"
          />
        </div>
        <Empty v-if="hist.loaded && !hist.loading && !hist.items.length" title="没有找到请求" :detail="hasHistFilter ? '换个筛选条件试试' : '这个时间段还没有请求'">
          <template #icon><Inbox /></template>
        </Empty>
        <div v-if="hist.total" class="pager">
          <span class="faint">第 {{ hist.page }} / {{ integer(pageCount) }} 页 · 共 {{ integer(hist.total) }} 条</span>
          <div class="pager-ctl">
            <Btn icon size="sm" title="第一页" :disabled="hist.page <= 1" @click="goPageNo(1)"><ChevronsLeft /></Btn>
            <Btn icon size="sm" title="上一页" :disabled="hist.page <= 1" @click="goPageNo(hist.page - 1)"><ChevronLeft /></Btn>
            <form class="jump" @submit.prevent="submitPage">
              <input v-model="pageInput" inputmode="numeric" :placeholder="String(hist.page)" aria-label="跳转到页" />
            </form>
            <Btn icon size="sm" title="下一页" :disabled="!hist.hasNext && hist.page >= pageCount" @click="goPageNo(hist.page + 1)"><ChevronRight /></Btn>
          </div>
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.page {
  max-width: 1720px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px 24px 40px;
}
.bar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 16px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
}
.bar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.selects {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
  padding-top: 10px;
  border-top: 1px solid var(--hairline);
}
.spacer {
  flex: 1;
}
.field {
  flex: 1;
  min-width: 220px;
  max-width: 520px;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 10px 0 12px;
  border-radius: var(--radius-md);
  background: var(--surface);
  box-shadow: inset 0 0 0 1px var(--border-strong);
  color: var(--text-3);
}
.field:focus-within {
  box-shadow: inset 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}
.field svg {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
}
.field input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: none;
  color: var(--text);
}
.clear {
  display: grid;
  place-items: center;
  color: var(--text-3);
}
.conn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 12.5px;
  color: var(--text-2);
  background: var(--surface-sunken);
  border: 0.5px solid var(--border);
}
.conn i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-3);
}
.conn.on {
  color: var(--text-green);
  background: color-mix(in srgb, var(--green) 9%, transparent);
  border-color: color-mix(in srgb, var(--green) 28%, transparent);
}
.conn.on i {
  background: var(--green);
  animation: pulse 1.6s ease-in-out infinite;
}
@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}
.range {
  font-size: 12.5px;
  color: var(--text-2);
  white-space: nowrap;
}
.kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.note {
  margin-top: -6px;
  font-size: 12px;
  color: var(--text-3);
}
.table {
  --cols: 74px minmax(96px, 0.8fr) minmax(140px, 1.15fr) minmax(170px, 1.5fr) 64px 76px 124px 84px 128px;
  --areas: 'time key channel model recent status tok cost lat' 'err err err err err err err err err';
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: opacity 0.15s;
}
.rows.dim {
  opacity: 0.55;
}
.head {
  display: grid;
  grid-template-columns: var(--cols);
  column-gap: 12px;
  padding: 2px 17px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
}
.ph-row {
  height: 66px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  border: 0.5px solid var(--border);
}
.more {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-radius: var(--radius-lg);
  font-size: 13px;
  color: var(--text-2);
  background: var(--surface-sunken);
  border: 1px dashed var(--border-strong);
}
.more:hover {
  border-color: var(--accent-line);
}
.go {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--accent);
  font-weight: 600;
}
.go svg {
  width: 14px;
  height: 14px;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 4px;
  font-size: 12.5px;
}
.faint {
  color: var(--text-3);
}
.pager-ctl {
  display: flex;
  align-items: center;
  gap: 6px;
}
.jump input {
  width: 56px;
  height: 28px;
  padding: 0 8px;
  border: 0;
  outline: 0;
  border-radius: var(--radius-sm);
  text-align: center;
  background: var(--surface);
  box-shadow: inset 0 0 0 1px var(--border-strong);
  color: var(--text);
}
@media (max-width: 1279px) {
  .table {
    --cols: 70px minmax(110px, 1fr) minmax(150px, 1.3fr) 76px 120px 80px 124px;
    --areas: 'time channel model status tok cost lat' 'err err err err err err err';
  }
  .head .h-key,
  .head .h-recent,
  .table :deep(.c-key),
  .table :deep(.c-recent) {
    display: none !important;
  }
}
/* Tablets: the seven-column row needs ~800px; three columns with latency spanning the rows fit. */
@media (min-width: 768px) and (max-width: 1023px) {
  .table {
    --cols: minmax(0, 1.4fr) minmax(0, 1fr) minmax(0, 1fr);
    --areas: 'model status lat' 'channel tok lat' 'time cost lat' 'err err err';
  }
  .head {
    display: none;
  }
}
@media (max-width: 767px) {
  .page {
    padding: 12px 10px 28px;
  }
  .kpis {
    display: flex;
    gap: 10px;
    margin: 0 -10px;
    padding: 0 10px 2px;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
  }
  .kpis > * {
    flex: 0 0 132px;
    scroll-snap-align: start;
  }
  .selects {
    grid-template-columns: 1fr 1fr;
  }
  .table {
    --cols: minmax(0, 1fr) minmax(0, 1fr);
    --areas: 'model model' 'channel status' 'tok lat' 'time cost' 'err err';
  }
  .head {
    display: none;
  }
  .table :deep(.c-cost) {
    align-items: flex-end;
  }
}
</style>
