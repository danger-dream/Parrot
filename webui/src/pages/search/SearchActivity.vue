<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChartBar, ChevronLeft, ChevronRight, RefreshCw, ScrollText, X } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { inspector, openInspector } from '@/stores/ui'
import { compact, dateTime, integer, ms, pctValue, relative, TICKS_PER_USD, usdOne } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Meter from '@/components/ui/Meter.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import SearchLogDetail from './SearchLogDetail.vue'
import { errorLabel, OPERATION_LABEL, ORIGIN_LABEL, STATUS_LABEL, usdFloat } from './labels'
import { searchRes, searchUi } from './store'

type Stat = Schemas['SearchSourceStatsData']
type Log = Schemas['SearchCallLogData']

const period = ref<'today' | 'month'>('month')
const stats = ref<Stat[] | null>(null)
const statsError = ref('')
const logs = ref<Log[]>([])
const logsLoading = ref(false)
const logsError = ref('')
const offset = ref(0)
const LIMIT = 20
const hasNext = ref(false)
const now = ref(Date.now())

const sourceOptions = computed(() => [
  { value: '', label: '全部来源' },
  ...(searchRes.value?.data.value?.backends || []).map((b) => ({ value: b.id, label: b.name })),
])

async function loadStats(): Promise<void> {
  try {
    stats.value = (await api.get<Stat[]>('/search/stats', { period: period.value })).data
    statsError.value = ''
  } catch (err) {
    statsError.value = errorTitle(err)
  }
}

let seq = 0
async function loadLogs(): Promise<void> {
  const my = ++seq
  logsLoading.value = true
  try {
    const res = await api.get<Log[]>('/search/logs', {
      period: period.value,
      sourceId: searchUi.logSource || undefined,
      limit: LIMIT + 1,
      offset: offset.value,
    })
    if (my !== seq) return
    hasNext.value = res.data.length > LIMIT
    logs.value = res.data.slice(0, LIMIT)
    logsError.value = ''
    now.value = Date.now()
  } catch (err) {
    if (my === seq) logsError.value = errorTitle(err)
  } finally {
    if (my === seq) logsLoading.value = false
  }
}

function reload(): void {
  loadStats()
  loadLogs()
}
onMounted(reload)
watch(period, () => {
  offset.value = 0
  reload()
})
watch(
  () => searchUi.logSource,
  () => {
    offset.value = 0
    loadLogs()
  },
)
watch(
  () => searchUi.logsTick,
  () => reload(),
)

function go(d: number): void {
  offset.value = Math.max(0, offset.value + d * LIMIT)
  loadLogs()
}

const totals = computed(() => {
  const s = stats.value || []
  return {
    attempts: s.reduce((a, r) => a + r.attempts, 0),
    success: s.reduce((a, r) => a + r.success, 0),
    cost: s.reduce((a, r) => a + r.costUsd, 0),
  }
})

function rate(r: Stat): number | null {
  const done = r.success + r.failed
  return done ? (r.success / done) * 100 : null
}

function openLog(l: Log): void {
  openInspector(`searchlog:${l.id}`, '搜索调用详情', SearchLogDetail, { log: l }, `${l.sourceName} · ${dateTime(l.startedAt)}`)
}
const selected = computed(() => (inspector.open && inspector.key.startsWith('searchlog:') ? inspector.key.slice(10) : ''))
</script>

<template>
  <div class="act-grid">
    <Card title="来源统计" :subtitle="`${period === 'today' ? '今天' : '本月'}每个来源的真实上游调用`" flush>
      <template #icon><ChartBar /></template>
      <template #actions>
        <Segmented
          v-model="period"
          size="sm"
          :options="[
            { value: 'today', label: '今天' },
            { value: 'month', label: '本月' },
          ]"
        />
      </template>
      <p v-if="statsError" class="state">{{ statsError }}</p>
      <div v-else-if="!stats" class="pad"><Skeleton v-for="i in 4" :key="i" h="14px" /></div>
      <Empty v-else-if="!stats.length" title="这段时间还没有搜索调用" />
      <div v-else class="stats">
        <div class="sum">
          <div><p class="k">调用</p><p class="v">{{ integer(totals.attempts) }}</p></div>
          <div><p class="k">成功</p><p class="v">{{ integer(totals.success) }}</p></div>
          <div><p class="k">金额</p><p class="v">{{ usdFloat(totals.cost) }}</p></div>
        </div>
        <button
          v-for="r in stats"
          :key="r.sourceId"
          type="button"
          class="srow"
          :class="{ on: searchUi.logSource === r.sourceId }"
          title="只看这个来源的调用记录"
          @click="searchUi.logSource = searchUi.logSource === r.sourceId ? '' : r.sourceId"
        >
          <span class="s1">
            <span class="sn truncate">{{ r.sourceName }}</span>
            <span class="mono">{{ integer(r.attempts) }} 次</span>
            <span class="mono rate" :class="{ 'c-red': (rate(r) ?? 100) < 80 }">{{ pctValue(rate(r)) }}</span>
          </span>
          <Meter :value="rate(r)" :warn="101" :danger="101" />
          <span class="s2 faint">
            <span>成功 {{ integer(r.success) }} · 失败 <b :class="{ 'c-red': r.failed }">{{ integer(r.failed) }}</b><template v-if="r.running"> · 进行中 {{ r.running }}</template></span>
            <span>平均 {{ r.averageMs !== null && r.averageMs !== undefined ? ms(r.averageMs) : '—' }}</span>
            <span>{{ integer(r.resultCount) }} 条结果</span>
            <span>{{ r.usageObserved ? `${compact(r.inputTokens + r.outputTokens)} Token · ${usdOne((r.costUsd ?? 0) * TICKS_PER_USD)}` : '按次计费' }}</span>
            <span>最近 {{ relative(r.lastAt * 1000, now) }}</span>
          </span>
        </button>
        <p class="note faint">金额只统计 OAuth 来源真实模型调用的 Token 结算；API Key 来源按次计费，不在 Token 用量中。</p>
      </div>
    </Card>

    <Card id="search-logs" title="调用记录" :subtitle="`${period === 'today' ? '今天' : '本月'} · 每次真实上游调用一行（含重试）`" flush>
      <template #icon><ScrollText /></template>
      <template #actions>
        <Select v-model="searchUi.logSource" width="150px" :options="sourceOptions" />
        <Btn v-if="searchUi.logSource" size="sm" variant="ghost" icon title="清除来源筛选" @click="searchUi.logSource = ''"><X /></Btn>
        <Btn size="sm" variant="ghost" icon title="刷新" :loading="logsLoading" @click="reload"><RefreshCw /></Btn>
      </template>
      <p v-if="logsError" class="state">{{ logsError }}</p>
      <div v-else class="logs">
        <template v-if="logsLoading && !logs.length">
          <div v-for="i in 8" :key="i" class="pad"><Skeleton h="14px" /></div>
        </template>
        <Empty v-else-if="!logs.length" title="没有调用记录" :detail="searchUi.logSource ? '这个来源在这段时间没有调用' : undefined" />
        <div v-else :class="{ dim: logsLoading }">
          <button
            v-for="l in logs"
            :key="l.id"
            type="button"
            class="lrow"
            :class="[`s-${l.status}`, { sel: selected === String(l.id) }]"
            @click="openLog(l)"
          >
            <span class="lt mono">{{ dateTime(l.startedAt) }}</span>
            <span class="ls"><i class="dot" />{{ STATUS_LABEL[l.status] || l.status }}</span>
            <span class="lsrc truncate">{{ l.sourceName }}</span>
            <span class="lq">
              <span class="op">{{ OPERATION_LABEL[l.operation] || l.operation }}</span>
              <span class="truncate" :class="{ mono: !l.query }">{{ l.query || l.url || '—' }}</span>
            </span>
            <span class="lm mono">
              {{ l.elapsedMs !== null && l.elapsedMs !== undefined ? ms(l.elapsedMs) : '—' }}
              <span class="faint">· {{ l.resultCount }} 条</span>
            </span>
            <span class="le">
              <span v-if="l.errorCode" class="c-red">{{ errorLabel(l.errorCode) }}</span>
              <span v-else class="faint">{{ ORIGIN_LABEL[l.origin] || l.origin }}</span>
            </span>
          </button>
        </div>
      </div>
      <div class="pager">
        <span class="faint">第 {{ offset / LIMIT + 1 }} 页</span>
        <Btn icon size="sm" :disabled="offset === 0 || logsLoading" @click="go(-1)"><ChevronLeft /></Btn>
        <Btn icon size="sm" :disabled="!hasNext || logsLoading" @click="go(1)"><ChevronRight /></Btn>
      </div>
    </Card>
  </div>
</template>

<style scoped>
.act-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.6fr);
  gap: 16px;
  align-items: stretch;
}
@container (max-width: 1100px) {
  .act-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
.state {
  padding: 40px 16px;
  text-align: center;
  color: var(--text-3);
}
.pad {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
}
.stats {
  display: flex;
  flex-direction: column;
  border-top: 0.5px solid var(--hairline);
}
.sum {
  display: flex;
  gap: 28px;
  padding: 12px 14px;
  border-bottom: 0.5px solid var(--hairline);
}
.k {
  font-size: 11px;
  color: var(--text-3);
}
.v {
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.srow {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 14px;
  text-align: left;
  border-bottom: 0.5px solid var(--hairline);
}
.srow:hover {
  background: var(--fill-3);
}
.srow.on {
  background: var(--accent-soft);
}
.s1 {
  display: flex;
  align-items: baseline;
  gap: 12px;
  font-size: 12.5px;
}
.sn {
  flex: 1;
  font-weight: 550;
}
.rate {
  width: 52px;
  text-align: right;
}
.s2 {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 12px;
  font-size: 11.5px;
}
.small {
  font-size: 11px;
}
.note {
  padding: 10px 14px 12px;
  font-size: 11px;
  line-height: 1.5;
}
.logs {
  container-type: inline-size;
  border-top: 0.5px solid var(--hairline);
}
.lrow {
  display: grid;
  grid-template-columns: 108px 62px minmax(80px, 0.7fr) minmax(160px, 2fr) 100px minmax(70px, 0.7fr);
  grid-template-areas: 'lt ls lsrc lq lm le';
  align-items: center;
  column-gap: 10px;
  width: 100%;
  min-height: 36px;
  padding: 0 14px;
  text-align: left;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.lrow:hover {
  background: var(--fill-3);
}
.lrow.sel {
  background: var(--accent-soft);
}
.s-success { --tone: var(--green); }
.s-error { --tone: var(--red); }
.s-running { --tone: var(--blue); }
.lt { grid-area: lt; font-size: 11.5px; color: var(--text-2); }
.ls { grid-area: ls; display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--tone); }
.lsrc { grid-area: lsrc; color: var(--text-2); }
.lq { grid-area: lq; display: flex; align-items: center; gap: 6px; min-width: 0; }
.lm { grid-area: lm; text-align: right; font-size: 11.5px; white-space: nowrap; }
.le { grid-area: le; font-size: 11.5px; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.op {
  flex-shrink: 0;
  font-size: 10.5px;
  padding: 1px 5px;
  border-radius: 4px;
  color: var(--text-2);
  background: var(--fill-2);
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--tone);
}
.dim {
  opacity: 0.55;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 8px 14px 10px;
  font-size: 12px;
}
@container (max-width: 640px) {
  .lrow {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas: 'lq ls' 'lsrc lm' 'lt le';
    row-gap: 3px;
    padding: 9px 12px;
  }
  .lt {
    font-size: 11px;
    color: var(--text-3);
  }
}
</style>
