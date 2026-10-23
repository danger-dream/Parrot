<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ListFilter, RefreshCw } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { refreshCatalog } from '@/stores/catalog'
import { errorTitle } from '@/utils/errors'
import { relative } from '@/utils/format'
import { goLogs } from '@/links'
import Page from '@/components/ui/Page.vue'
import Btn from '@/components/ui/Btn.vue'
import Segmented from '@/components/ui/Segmented.vue'
import KpiGrid from './KpiGrid.vue'
import { loadCompare, type Compare } from '@/utils/compare'
import FamilyCards from './FamilyCards.vue'
import GroupTrend from './GroupTrend.vue'
import RankingCard from './RankingCard.vue'
import CacheMisses from './CacheMisses.vue'
import { DIMS, FOCI, parseDim, parseFocus, parsePeriod, PERIODS, periodStart, type Dim, type Focus, type Period } from './metrics'

type Summary = Schemas['StatsSummaryData']
type Row = Schemas['StatsBreakdownData']

const route = useRoute()
const router = useRouter()

// Deep link: /analytics?period=7d&dimension=channel&metric=cost — applied on entry and whenever the query changes.
const period = ref<Period>(parsePeriod(route.query.period) || 'today')
const dim = ref<Dim>(parseDim(route.query.dimension) || 'model')
const focus = ref<Focus>(parseFocus(route.query.metric) || 'requests')

watch(
  () => route.query,
  (q) => {
    if (route.path !== '/analytics') return
    period.value = parsePeriod(q.period) || 'today'
    dim.value = parseDim(q.dimension) || 'model'
    focus.value = parseFocus(q.metric) || 'requests'
  },
)

watch([period, dim, focus], () => {
  const want: Record<string, string> = { period: period.value, dimension: dim.value }
  if (focus.value !== 'requests') want.metric = focus.value
  const cur = route.query
  const same =
    cur.period === want.period && cur.dimension === want.dimension && (cur.metric || undefined) === (want.metric || undefined)
  if (!same) router.replace({ path: '/analytics', query: want })
})

const summary = ref<Summary | null>(null)
const summaryLoading = ref(false)
const summaryError = ref('')
const rows = ref<Row[]>([])
const rowsTruncated = ref(false)
const rowsLoading = ref(false)
const rowsError = ref('')
const loadedAt = ref(0)
/** This period against the one before it; null for 累计. */
const compare = ref<Compare | null>(null)
const now = ref(Date.now())

let sSeq = 0
async function loadSummary(): Promise<void> {
  const my = ++sSeq
  summaryLoading.value = true
  try {
    const p = period.value
    const [res, cmp] = await Promise.all([
      api.get<Summary>('/stats/summary', { period: p }),
      p === 'lifetime' ? Promise.resolve(null) : loadCompare({ period: p }).catch(() => null),
    ])
    if (my !== sSeq) return
    summary.value = res.data
    compare.value = cmp
    summaryError.value = ''
    loadedAt.value = Date.now()
  } catch (err) {
    if (my === sSeq) summaryError.value = errorTitle(err)
  } finally {
    if (my === sSeq) summaryLoading.value = false
  }
}

let bSeq = 0
async function loadRows(): Promise<void> {
  const my = ++bSeq
  rowsLoading.value = true
  try {
    const res = await api.get<Row[]>('/stats/breakdown', {
      dimension: dim.value,
      period: period.value,
      sort: 'total',
      descending: true,
      pageSize: 200,
    })
    if (my !== bSeq) return
    rows.value = res.data
    rowsTruncated.value = Boolean(res.meta.hasNext)
    rowsError.value = ''
  } catch (err) {
    if (my === bSeq) rowsError.value = errorTitle(err)
  } finally {
    if (my === bSeq) rowsLoading.value = false
  }
}

const trend = ref<InstanceType<typeof GroupTrend> | null>(null)
const misses = ref<InstanceType<typeof CacheMisses> | null>(null)

function refresh(): void {
  loadSummary()
  loadRows()
  trend.value?.reload()
  misses.value?.reload()
}

watch(period, () => {
  loadSummary()
  loadRows()
})
watch(dim, (d, old) => {
  if (d !== old) rows.value = []
  loadRows()
})

let timer: number | undefined
let tick: number | undefined
onMounted(() => {
  refreshCatalog()
  loadSummary()
  loadRows()
  // Statistics are served from the backend's period cache, so every range (lifetime too) refreshes itself.
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    loadSummary()
    loadRows()
    misses.value?.reload()
  }, 60_000)
  tick = window.setInterval(() => (now.value = Date.now()), 15_000)
})
onBeforeUnmount(() => {
  window.clearInterval(timer)
  window.clearInterval(tick)
})
</script>

<template>
  <Page title="统计分析" subtitle="按时间范围查看用量、金额、缓存与速度，并按模型、渠道或 Key 拆分。">
    <template #actions>
      <span v-if="loadedAt" class="faint upd">更新于 {{ relative(loadedAt, now) }}</span>
      <Btn :loading="summaryLoading || rowsLoading" @click="refresh"><RefreshCw /> 刷新</Btn>
      <Btn @click="goLogs({ from: periodStart(period) })"><ListFilter /> 查看这段时间的请求</Btn>
    </template>
    <template #toolbar>
      <Segmented v-model="period" :options="PERIODS" />
      <Segmented v-model="dim" :options="DIMS" />
      <span class="spacer" />
      <span class="lab faint">重点指标</span>
      <Segmented v-model="focus" size="sm" :options="FOCI" />
    </template>

    <p v-if="summaryError" class="err">统计加载失败：{{ summaryError }}</p>
    <KpiGrid
      :metrics="summary?.overall || null"
      :compare="compare"
      :period="period === 'lifetime' ? null : period"
      :focus="focus"
      :loading="summaryLoading"
      @focus="focus = $event"
    />
    <GroupTrend ref="trend" :dim="dim" :focus="focus" />
    <RankingCard :rows="rows" :dim="dim" :period="period" :focus="focus" :loading="rowsLoading" :truncated="rowsTruncated" :error="rowsError" />
    <div class="two">
      <FamilyCards :families="summary?.families || null" :overall="summary?.overall || null" :loading="summaryLoading" />
      <div class="side">
        <CacheMisses ref="misses" :period="period" />
      </div>
    </div>
  </Page>
</template>

<style scoped>
.spacer {
  flex: 1;
}
.lab {
  font-size: 12px;
}
.upd {
  font-size: 12px;
}
.err {
  padding: 10px 14px;
  border-radius: var(--radius-md);
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
  font-size: 12.5px;
}
.side {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.side > * {
  height: auto;
}
.side > :last-child {
  flex: 1;
}
.two {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}
@container (max-width: 1100px) {
  .side {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.side > * {
  height: auto;
}
.side > :last-child {
  flex: 1;
}
.two {
    grid-template-columns: minmax(0, 1fr);
  }
}
@container (max-width: 600px) {
  .spacer,
  .lab {
    display: none;
  }
}
</style>
