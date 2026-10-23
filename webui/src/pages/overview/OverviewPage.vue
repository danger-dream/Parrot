<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Activity, CircleCheck, Coins, DatabaseZap, Inbox, Layers, RefreshCw, Timer, Zap } from '@lucide/vue'
import { onEvent } from '@/api/events'
import type { RequestLogData } from '@/api/types'
import { catalog } from '@/stores/catalog'
import { live, qps, seedQps } from '@/stores/live'
import { api } from '@/api/client'
import type { TimeSeriesData } from '@/api/types'
import { goPage } from '@/links'
import { clock, compact, integer, ms, usd } from '@/utils/format'
import StatTile from '@/components/ui/StatTile.vue'
import Btn from '@/components/ui/Btn.vue'
import TrafficTrend from './TrafficTrend.vue'
import HealthTimeline from './HealthTimeline.vue'
import TokenMix from './TokenMix.vue'
import ModelRanking from './ModelRanking.vue'
import ChannelHealth from './ChannelHealth.vue'
import AccountQuota from './AccountQuota.vue'
import FamilyTable from './FamilyTable.vue'
import { loadOverview, ov } from './data'
import { cacheShare, outcomeLine, pctText, promptOf, successRateOf, tpsText, type Metrics } from './metrics'
import { deltas, sparks as compareSparks } from '@/utils/compare'

// Requests finished since the last summary snapshot (the summary is cached ~15 s server-side).
const delta = reactive({ total: 0, success: 0, error: 0, prompt: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 })
const now = ref(Date.now())

async function refresh(): Promise<void> {
  await loadOverview()
  Object.assign(delta, { total: 0, success: 0, error: 0, prompt: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 })
}

const off = onEvent('request.finished', ({ log }: { log: RequestLogData }) => {
  if (!ov.summary) return
  delta.total += 1
  if (log.status === 'success') delta.success += 1
  else if (log.status === 'error') delta.error += 1
  delta.prompt += log.promptTokens
  delta.output += log.outputTokens
  delta.cacheRead += log.cacheReadTokens
  delta.cacheWrite += log.cacheCreationTokens
  delta.cost += log.billing?.costTicks ?? log.costTicks
})

let poll: number | undefined
let tick: number | undefined
onMounted(() => {
  refresh()
  if (!live.finished)
    api
      .get<TimeSeriesData>('/stats/timeseries', { range: '1h', bucket: 'minute' })
      .then((res) => seedQps(res.data.series.find((s) => s.key === 'total')?.points || [], res.data.bucketSeconds || 60))
      .catch(() => undefined)
  poll = window.setInterval(() => document.visibilityState === 'visible' && refresh(), 30_000)
  tick = window.setInterval(() => (now.value = Date.now()), 1000)
})
onBeforeUnmount(() => {
  off()
  window.clearInterval(poll)
  window.clearInterval(tick)
})

const m = computed<Metrics | null>(() => {
  const o = ov.summary?.overall
  if (!o) return null
  if (!delta.total) return o
  const total = o.total + delta.total
  const successCount = o.successCount + delta.success
  const promptTokens = promptOf(o) + delta.prompt
  const cacheReadTokens = o.cacheReadTokens + delta.cacheRead
  return {
    ...o,
    total,
    successCount,
    errorCount: o.errorCount + delta.error,
    outputTokens: o.outputTokens + delta.output,
    cacheReadTokens,
    cacheCreationTokens: o.cacheCreationTokens + delta.cacheWrite,
    costTicks: o.costTicks + delta.cost,
    promptTokens,
    cacheReadRatio: promptTokens ? cacheReadTokens / promptTokens : null,
    successRate: total ? successCount / total : null,
  }
})

const success = computed(() => successRateOf(m.value))
const successTone = computed(() => {
  const v = success.value
  if (v == null) return ''
  return v >= 0.99 ? 'green' : v >= 0.95 ? 'orange' : 'red'
})

/** Today's sparklines and the change against yesterday up to the same time. */
const sparks = computed(() => compareSparks(ov.compare))
const change = computed(() => deltas(ov.compare, 'today'))

const perMinute = computed(() => {
  void qps.version
  const n = qps.ok.length
  let s = 0
  for (let i = n - 60; i < n; i++) s += (qps.ok[i] || 0) + (qps.err[i] || 0)
  return s
})
const minuteSpark = computed(() => {
  void qps.version
  const out: number[] = []
  const n = qps.ok.length
  for (let i = n - 900; i < n; i += 60) {
    let s = 0
    for (let j = i; j < i + 60; j++) s += (qps.ok[j] || 0) + (qps.err[j] || 0)
    out.push(s)
  }
  return out
})
const waiting = computed(() => live.snapshot?.channelTotals.waiting ?? catalog.status?.concurrency.channelTotals.waiting ?? 0)

</script>

<template>
  <div class="ov">
    <div class="sec-row">
      <h3 class="sec">今日概览</h3>
      <span class="clock num">{{ clock(now) }}</span>
      <Btn size="sm" variant="secondary" :loading="ov.loading && ov.ready" @click="refresh"><RefreshCw /> 刷新</Btn>
    </div>
    <section class="kpis">
      <StatTile label="今日请求" :icon="Inbox" tone="blue" :loading="!ov.ready" :value="m ? integer(m.total) : null"
        :sub="m ? outcomeLine(m) : ''" :spark="sparks.requests" :delta="change.requests" />
      <StatTile label="成功率" :icon="CircleCheck" tone="green" :loading="!ov.ready" :value="m ? pctText(success) : null"
        :value-tone="successTone" :sub="m ? `${integer(m.successCount)} / ${integer(m.total)}` : ''" :spark="sparks.success" :delta="change.success" />
      <StatTile label="Token" :icon="Layers" tone="purple" :loading="!ov.ready" :value="m ? compact(promptOf(m) + m.outputTokens) : null"
        :sub="m ? `读入 ${compact(promptOf(m))} · 写出 ${compact(m.outputTokens)}` : ''" :spark="sparks.tokens" :delta="change.tokens" />
      <StatTile label="缓存占比" :icon="DatabaseZap" tone="teal" :loading="!ov.ready" :value="m ? pctText(cacheShare(m)) : null"
        :sub="m ? `缓存读 ${compact(m.cacheReadTokens)} · 命中 ${integer(m.cacheHitRequests)} 次` : ''" :spark="sparks.cache" :delta="change.cache" />
      <StatTile label="今日金额" :icon="Coins" tone="orange" :loading="!ov.ready" :value="m ? usd(m.costTicks) : null"
        :sub="m ? (m.unpricedSuccess ? `未计价 ${integer(m.unpricedSuccess)} 次` : `实际 ${usd(m.actualCostTicks)} · 估算 ${usd(m.estimatedCostTicks)}`) : ''" :spark="sparks.cost" :delta="change.cost" />
      <StatTile label="首字" :icon="Timer" tone="cyan" :loading="!ov.ready" :value="m ? ms(m.averageFirstTokenMilliseconds) : null"
        :sub="m ? `总耗时 ${ms(m.averageTotalMilliseconds)} · 连接 ${ms(m.averageConnectMilliseconds)}` : ''" :spark="sparks.firstToken" :delta="change.firstToken" />
      <StatTile label="生成速度" :icon="Zap" tone="indigo" :loading="!ov.ready" :value="m ? tpsText(m.averageTokensPerSecond) : null"
        :sub="m ? `峰值 ${tpsText(m.maximumTokensPerSecond)} · 最低 ${tpsText(m.minimumTokensPerSecond)}` : ''" :spark="sparks.tps" :delta="change.tps" />
      <StatTile label="当前负载" :icon="Activity" tone="pink" :value="integer(perMinute)" unit="次/分"
        :sub="`进行中 ${live.inFlight.size} · 排队 ${waiting}`" :spark="minuteSpark" @click="goPage('/live')" />
    </section>

    <section class="row a">
      <TrafficTrend :points="ov.hourly" :ready="ov.ready" />
      <HealthTimeline :points="ov.tenMinutes" :ready="ov.ready" />
      <TokenMix :m="m" :ready="ov.ready" />
    </section>

    <section class="row b">
      <ModelRanking :rows="ov.models" :ready="ov.ready" />
      <ChannelHealth :ready="ov.ready" />
      <AccountQuota :ready="ov.ready" />
    </section>

    <FamilyTable :ready="ov.ready" />
  </div>
</template>

<style scoped>
.ov {
  max-width: 1720px;
  margin: 0 auto;
  padding: 20px 24px 40px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.sec-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 4px;
}
.sec-row .sec {
  flex: 1;
  margin-top: 0;
}
.clock {
  font-size: 15px;
  font-weight: 600;
}
.sec {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 16px;
  font-weight: 650;
}
.sec::before {
  content: '';
  width: 3px;
  height: 16px;
  border-radius: 2px;
  background: var(--accent);
}
.kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.kpis > :last-child {
  cursor: pointer;
}
.row {
  display: grid;
  gap: 16px;
}
.row.a {
  grid-template-columns: minmax(0, 1.25fr) minmax(0, 1.25fr) minmax(0, 0.8fr);
}
.row.b {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
@media (max-width: 1279px) {
  .row.a,
  .row.b {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
  .row.a > :first-child {
    grid-column: 1 / -1;
  }
  .row.b > :last-child {
    grid-column: 1 / -1;
  }
}
@media (max-width: 767px) {
  .ov {
    padding: 12px 10px 28px;
    gap: 12px;
  }
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }
  .row.a,
  .row.b {
    grid-template-columns: minmax(0, 1fr);
  }
  .st-right {
    width: 100%;
    justify-content: space-between;
  }
}
</style>
