<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Coins, DatabaseZap, Gauge, Layers, RefreshCw, Send } from '@lucide/vue'
import { api } from '@/api/client'
import { onEvent } from '@/api/events'
import type { StatsSummaryData, TimePoint, TimeSeriesData } from '@/api/types'
import { catalog, refreshCatalog } from '@/stores/catalog'
import { live, seedQps } from '@/stores/live'
import { refreshSystem, system } from '@/stores/system'
import { compact, duration, integer, ms, percent, usd } from '@/utils/format'
import KpiCard from './KpiCard.vue'
import LiveTrafficCard from './LiveTrafficCard.vue'
import HealthCard from './HealthCard.vue'
import TrendCard from './TrendCard.vue'
import BreakdownCard from './BreakdownCard.vue'
import OAuthPoolCard from './OAuthPoolCard.vue'
import RecentErrorsCard from './RecentErrorsCard.vue'
import Btn from '@/components/ui/Btn.vue'
import StatusDot from '@/components/ui/StatusDot.vue'

const summary = ref<StatsSummaryData | null>(null)
const hourly = ref<TimePoint[]>([])
const refreshing = ref(false)
const delta = reactive({ total: 0, success: 0, error: 0, input: 0, output: 0, cost: 0 })

const trend = ref<InstanceType<typeof TrendCard> | null>(null)
const breakdown = ref<InstanceType<typeof BreakdownCard> | null>(null)
const pool = ref<InstanceType<typeof OAuthPoolCard> | null>(null)
const errors = ref<InstanceType<typeof RecentErrorsCard> | null>(null)

async function loadSummary(): Promise<void> {
  const res = await api.get<StatsSummaryData>('/stats/summary', { period: 'today' })
  summary.value = res.data
  Object.assign(delta, { total: 0, success: 0, error: 0, input: 0, output: 0, cost: 0 })
}

async function loadHourly(): Promise<void> {
  try {
    const res = await api.get<TimeSeriesData>('/stats/timeseries', { range: '7d', bucket: 'hour' })
    hourly.value = res.data.series.find((s) => s.key === 'total')?.points || res.data.series[0]?.points || []
  } catch {
    hourly.value = []
  }
}

async function seedLive(): Promise<void> {
  if (live.finished > 0) return
  try {
    const res = await api.get<TimeSeriesData>('/stats/timeseries', { range: '1h', bucket: 'minute' })
    const pts = res.data.series.find((s) => s.key === 'total')?.points || []
    seedQps(pts, res.data.bucketSeconds || 60)
  } catch {
    /* live chart fills from events */
  }
}

async function refreshAll(): Promise<void> {
  refreshing.value = true
  try {
    await Promise.allSettled([
      loadSummary(),
      loadHourly(),
      refreshSystem(),
      refreshCatalog(true),
      trend.value?.reload(),
      breakdown.value?.reload(),
      pool.value?.reload(),
      errors.value?.reload(),
    ])
  } finally {
    refreshing.value = false
  }
}

const off = onEvent('request.finished', ({ log }) => {
  if (!summary.value) return
  delta.total += 1
  if (log.status === 'success') delta.success += 1
  else if (log.status === 'error') delta.error += 1
  delta.input += log.inputTokens
  delta.output += log.outputTokens
  delta.cost += log.costTicks
})

let fast: number | undefined
let slow: number | undefined
onMounted(() => {
  loadSummary()
  loadHourly()
  seedLive()
  fast = window.setInterval(() => document.visibilityState === 'visible' && loadSummary(), 30_000)
  slow = window.setInterval(() => document.visibilityState === 'visible' && loadHourly(), 300_000)
})
onBeforeUnmount(() => {
  off()
  window.clearInterval(fast)
  window.clearInterval(slow)
})

// ---- derived KPI values ----
const m = computed(() => summary.value?.overall)
const total = computed(() => (m.value ? m.value.total + delta.total : null))
const success = computed(() => (m.value ? m.value.successCount + delta.success : 0))
const errorsToday = computed(() => (m.value ? m.value.errorCount + delta.error : 0))
const tokens = computed(() =>
  m.value ? m.value.inputTokens + m.value.outputTokens + m.value.cacheReadTokens + m.value.cacheCreationTokens + delta.input + delta.output : null,
)
const cost = computed(() => (m.value ? m.value.costTicks + delta.cost : null))
const cacheRate = computed(() => (m.value && m.value.successCount ? m.value.cacheHitRequests / m.value.successCount : null))

function bjtDayStart(ts: number): number {
  const offset = 8 * 3600_000
  return Math.floor((ts + offset) / 86400_000) * 86400_000 - offset
}

const windows = computed(() => {
  const now = Date.now()
  const todayStart = bjtDayStart(now)
  const today = hourly.value.filter((p) => new Date(p.t).getTime() >= todayStart)
  const yStart = todayStart - 86400_000
  const yEnd = now - 86400_000
  const yesterday = hourly.value.filter((p) => {
    const t = new Date(p.t).getTime()
    return t >= yStart && t < yEnd
  })
  return { today, yesterday }
})

function sumOf(points: TimePoint[], fn: (p: TimePoint) => number): number {
  return points.reduce((a, p) => a + fn(p), 0)
}

function change(fn: (p: TimePoint) => number): number | null {
  const { today, yesterday } = windows.value
  if (!yesterday.length) return null
  const before = sumOf(yesterday, fn)
  if (!before) return null
  return sumOf(today, fn) / before - 1
}

const tokenOf = (p: TimePoint) => p.inputTokens + p.outputTokens + p.cacheReadTokens + p.cacheCreationTokens

const sparks = computed(() => {
  const pts = windows.value.today
  return {
    req: pts.map((p) => p.total),
    tok: pts.map(tokenOf),
    cost: pts.map((p) => p.costTicks),
    speed: pts.map((p) => p.averageFirstTokenMilliseconds ?? 0),
  }
})

const firstTokenChange = computed(() => {
  const { today, yesterday } = windows.value
  const avg = (pts: TimePoint[]) => {
    const vals = pts.filter((p) => p.averageFirstTokenMilliseconds != null)
    if (!vals.length) return null
    const weight = vals.reduce((a, p) => a + p.successCount, 0) || vals.length
    return vals.reduce((a, p) => a + (p.averageFirstTokenMilliseconds ?? 0) * (p.successCount || 1), 0) / weight
  }
  const a = avg(today)
  const b = avg(yesterday)
  return a && b ? a / b - 1 : null
})

// ---- hero ----
const issues = computed(() => {
  const s = catalog.status
  const alerts = Object.values(system.overview?.activeAlerts || {}).reduce((n: number, v) => n + (Array.isArray(v) ? v.length : 0), 0)
  return {
    channels: s?.problemChannels.filter((c) => c.health !== 'disabled').length || 0,
    quota: s?.quotaWarnings.length || 0,
    alerts,
  }
})
const healthy = computed(() => !issues.value.channels && !issues.value.quota && !issues.value.alerts)
const headline = computed(() => {
  if (!catalog.status) return '正在检查…'
  if (healthy.value) return '运行正常'
  const parts: string[] = []
  if (issues.value.channels) parts.push(`${issues.value.channels} 个渠道异常`)
  if (issues.value.quota) parts.push(`${issues.value.quota} 个账号额度紧张`)
  if (issues.value.alerts) parts.push(`${issues.value.alerts} 条上游告警`)
  return parts.join('，')
})
</script>

<template>
  <div class="ov">
    <header class="hero">
      <div class="state">
        <StatusDot :tone="!catalog.status ? 'gray' : healthy ? 'green' : 'orange'" :size="10" :pulse="healthy" />
        <h2>{{ headline }}</h2>
      </div>
      <p class="facts">
        <template v-if="system.overview">
          <span>Parrot v{{ system.overview.version }}</span>
          <span>已运行 {{ duration(system.overview.uptimeSeconds) }}</span>
          <span v-if="system.overview.publicIp">出口 <b class="mono">{{ system.overview.publicIp }}</b></span>
          <span v-if="system.overview.telegramBot" class="tg">
            <Send /> {{ system.overview.telegramBot.configured ? 'TG Bot 已配置' : 'TG Bot 未配置' }}
          </span>
          <span v-if="system.overview.update?.available" class="upd">有新版本 {{ system.overview.update.latestVersion }}</span>
        </template>
      </p>
      <Btn class="refresh" variant="ghost" :loading="refreshing" @click="refreshAll"><RefreshCw /> 刷新</Btn>
    </header>

    <section class="kpis stagger">
      <KpiCard
        label="今日请求"
        :icon="Layers"
        tint="var(--blue)"
        :value="total === null ? null : integer(total)"
        :sub="total ? `成功率 ${percent(success, total)} · 失败 ${integer(errorsToday)}` : ''"
        :delta="change((p) => p.total)"
        neutral-delta
        :spark="sparks.req"
      />
      <KpiCard
        label="今日 Token"
        :icon="DatabaseZap"
        tint="var(--purple)"
        :value="tokens === null ? null : compact(tokens)"
        :sub="m ? `输入 ${compact(m.inputTokens + delta.input)} · 输出 ${compact(m.outputTokens + delta.output)} · 缓存读 ${compact(m.cacheReadTokens)}` : ''"
        :delta="change(tokenOf)"
        neutral-delta
        :spark="sparks.tok"
      />
      <KpiCard
        label="今日费用"
        :icon="Coins"
        tint="var(--orange)"
        :value="cost === null ? null : usd(cost, { precise: true })"
        :sub="m ? (m.estimatedCostTicks ? `其中估算 ${usd(m.estimatedCostTicks)} · 未定价 ${integer(m.unpricedSuccess)} 次` : '全部为实际计费') : ''"
        :delta="change((p) => p.costTicks)"
        neutral-delta
        :spark="sparks.cost"
      />
      <KpiCard
        label="首字耗时"
        :icon="Gauge"
        tint="var(--teal)"
        :value="m ? ms(m.averageFirstTokenMilliseconds) : null"
        :sub="m ? `平均总耗时 ${ms(m.averageTotalMilliseconds)} · 输出 ${m.averageTokensPerSecond ? m.averageTokensPerSecond.toFixed(0) : '—'} tok/s` : ''"
        :delta="firstTokenChange"
        :delta-good-when-up="false"
        :spark="sparks.speed"
      />
      <KpiCard
        label="缓存命中"
        :icon="DatabaseZap"
        tint="var(--green)"
        :value="cacheRate === null ? (m ? '—' : null) : (cacheRate * 100).toFixed(1)"
        :unit="cacheRate === null ? '' : '%'"
        :sub="m ? `${integer(m.cacheHitRequests)} 次命中 · ${integer(m.affinityHits)} 次会话亲和` : ''"
      />
    </section>

    <section class="grid">
      <LiveTrafficCard class="span-8" />
      <HealthCard class="span-4" />
      <TrendCard ref="trend" class="span-8" />
      <BreakdownCard ref="breakdown" class="span-4" />
      <OAuthPoolCard ref="pool" class="span-6" />
      <RecentErrorsCard ref="errors" class="span-6" />
    </section>
  </div>
</template>

<style scoped>
.ov {
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1680px;
  margin: 0 auto;
  padding: 22px 24px 32px;
}
.hero {
  display: grid;
  grid-template-columns: 1fr auto;
  grid-template-areas: 'state refresh' 'facts refresh';
  align-items: center;
  gap: 4px 12px;
}
.state {
  grid-area: state;
  display: flex;
  align-items: center;
  gap: 10px;
}
.state h2 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.facts {
  grid-area: facts;
  display: flex;
  flex-wrap: wrap;
  gap: 2px 14px;
  font-size: 12.5px;
  color: var(--text-2);
  padding-left: 20px;
}
.facts b {
  font-weight: 500;
  color: var(--text);
}
.tg {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.tg svg {
  width: 12px;
  height: 12px;
  color: var(--telegram);
}
.upd {
  color: var(--green);
}
.refresh {
  grid-area: refresh;
}
.kpis {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}
@container (max-width: 900px) {
  .kpis {
    grid-template-columns: repeat(6, minmax(0, 1fr));
  }
  .kpis > :nth-child(-n + 3) {
    grid-column: span 2;
  }
  .kpis > :nth-child(n + 4) {
    grid-column: span 3;
  }
}
.grid {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
}
.span-8 {
  grid-column: span 8;
}
.span-4 {
  grid-column: span 4;
}
.span-6 {
  grid-column: span 6;
}
@container (max-width: 1080px) {
  .span-8,
  .span-4 {
    grid-column: span 12;
  }
}
@container (max-width: 760px) {
  .span-6 {
    grid-column: span 12;
  }
}
@media (max-width: 767px) {
  .ov {
    padding: 14px 12px 24px;
    gap: 12px;
  }
  .state h2 {
    font-size: 19px;
  }
  .facts {
    padding-left: 0;
  }
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }
  .kpis > :nth-child(n) {
    grid-column: span 1;
  }
  .kpis > :last-child:nth-child(odd) {
    grid-column: span 2;
  }
}
@media (max-width: 340px) {
  .kpis {
    grid-template-columns: 1fr;
  }
  .kpis > :last-child:nth-child(odd) {
    grid-column: auto;
  }
}
</style>
