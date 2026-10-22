<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Activity, ChevronRight } from '@lucide/vue'
import { eventLink } from '@/api/events'
import { live, qps } from '@/stores/live'
import { channelName } from '@/stores/catalog'
import { clock, ms } from '@/utils/format'
import { openRequest } from '@/pages/live/openRequest'
import { axisBase, chartColors, tooltipBase } from '@/components/charts/palette'
import Card from '@/components/ui/Card.vue'
import EChart from '@/components/charts/EChart.vue'
import StatusDot from '@/components/ui/StatusDot.vue'

const STEP = 10
const now = ref(Date.now())
let t: number | undefined
onMounted(() => (t = window.setInterval(() => (now.value = Date.now()), 250)))
onBeforeUnmount(() => window.clearInterval(t))

const buckets = computed(() => {
  void qps.version
  const ok: number[] = []
  const err: number[] = []
  const labels: string[] = []
  for (let i = 0; i < qps.ok.length; i += STEP) {
    let a = 0
    let b = 0
    for (let j = i; j < i + STEP && j < qps.ok.length; j++) {
      a += qps.ok[j]
      b += qps.err[j]
    }
    ok.push(Math.round(a * 10) / 10)
    err.push(Math.round(b * 10) / 10)
    labels.push(clock((qps.origin + i) * 1000, false))
  }
  return { ok, err, labels }
})

const perMinute = computed(() => {
  const n = buckets.value.ok.length
  const last = buckets.value.ok.slice(n - 6).reduce((a, b) => a + b, 0) + buckets.value.err.slice(n - 6).reduce((a, b) => a + b, 0)
  return Math.round(last)
})

const option = computed(() => {
  const c = chartColors()
  const base = axisBase(c)
  return {
    animation: false,
    grid: { left: 4, right: 4, top: 10, bottom: 18, containLabel: true },
    tooltip: {
      ...tooltipBase(c),
      trigger: 'axis',
      axisPointer: { type: 'shadow', shadowStyle: { color: c.grid } },
      formatter: (items: { axisValue: string; value: number; seriesName: string }[]) =>
        `${items[0]?.axisValue} 起 10 秒<br/>` + items.map((i) => `${i.seriesName}：${i.value}`).join('<br/>'),
    },
    xAxis: { type: 'category', data: buckets.value.labels, ...base, axisLabel: { ...base.axisLabel, interval: 17 } },
    yAxis: { type: 'value', ...base, minInterval: 1, splitNumber: 3 },
    series: [
      { name: '成功', type: 'bar', stack: 'q', data: buckets.value.ok, barWidth: '62%', itemStyle: { color: c.teal, borderRadius: [0, 0, 0, 0] } },
      { name: '失败', type: 'bar', stack: 'q', data: buckets.value.err, itemStyle: { color: c.red, borderRadius: [2, 2, 0, 0] } },
    ],
  }
})

const inflight = computed(() => [...live.inFlight.values()].sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1)).slice(0, 6))
</script>

<template>
  <Card title="实时流量" subtitle="最近 15 分钟，每 10 秒一格">
    <template #icon><Activity /></template>
    <template #actions>
      <span class="link-state">
        <StatusDot :tone="eventLink.state === 'open' ? 'green' : 'orange'" :pulse="eventLink.state === 'open'" :size="6" />
        {{ eventLink.state === 'open' ? '实时' : '连接中' }}
      </span>
    </template>
    <div class="cq">
    <div class="wrap">
      <div class="chart-col">
        <div class="headline">
          <span class="big">{{ perMinute }}</span>
          <span class="unit">次 / 分钟</span>
          <span class="sep" />
          <span class="big small">{{ live.inFlight.size }}</span>
          <span class="unit">进行中</span>
          <template v-if="live.snapshot?.channelTotals.waiting">
            <span class="sep" />
            <span class="big small warn">{{ live.snapshot.channelTotals.waiting }}</span>
            <span class="unit">排队</span>
          </template>
        </div>
        <EChart :option="option" height="168px" />
      </div>
      <div class="inflight">
        <p class="if-title">正在处理</p>
        <p v-if="!inflight.length" class="if-empty">当前没有进行中的请求</p>
        <button v-for="r in inflight" :key="r.id" class="if-row" @click="openRequest(r.id)">
          <span class="if-dot" />
          <span class="if-main">
            <span class="mono truncate">{{ r.requestedModel || '—' }}</span>
            <span class="if-sub truncate">{{ r.apiKeyName || '—' }} · {{ r.channelId ? channelName(r.channelId) : '分配中' }}</span>
          </span>
          <span class="if-time mono">{{ ms(Math.max(0, now - new Date(r.createdAt).getTime())) }}</span>
        </button>
        <RouterLink to="/live" class="if-more">查看实时请求 <ChevronRight /></RouterLink>
      </div>
    </div>
    </div>
    <p class="foot faint">数据来自实时推送{{ live.snapshotAt ? `，${clock(live.snapshotAt)} 更新` : '' }}</p>
  </Card>
</template>

<style scoped>
.link-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-2);
}
.cq {
  container-type: inline-size;
}
.wrap {
  display: flex;
  gap: 16px;
}
.chart-col {
  flex: 1;
  min-width: 0;
}
.headline {
  display: flex;
  align-items: baseline;
  gap: 5px;
  flex-wrap: wrap;
}
.big {
  font-family: var(--font-display);
  font-size: 26px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.big.small {
  font-size: 20px;
}
.big.warn {
  color: var(--orange);
}
.unit {
  font-size: 12px;
  color: var(--text-2);
}
.sep {
  width: 0.5px;
  height: 14px;
  margin: 0 8px;
  align-self: center;
  background: var(--border-strong);
}
.inflight {
  width: 250px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-left: 16px;
  border-left: 0.5px solid var(--hairline);
}
.if-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  margin-bottom: 4px;
}
.if-empty {
  font-size: 12px;
  color: var(--text-3);
  padding: 10px 0;
}
.if-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  margin: 0 -6px;
  border-radius: 6px;
  text-align: left;
}
.if-row:hover {
  background: var(--fill-3);
}
.if-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--blue);
  flex-shrink: 0;
  animation: pulse-dot 1s ease-in-out infinite;
}
@keyframes pulse-dot {
  50% {
    opacity: 0.3;
  }
}
.if-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  font-size: 12px;
}
.if-sub {
  font-size: 11px;
  color: var(--text-3);
}
.if-time {
  font-size: 11px;
  color: var(--blue);
}
.if-more {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-top: auto;
  padding-top: 6px;
  font-size: 12px;
}
.if-more svg {
  width: 13px;
  height: 13px;
}
.foot {
  margin-top: 6px;
  font-size: 11px;
}
@container (max-width: 560px) {
  .wrap {
    flex-direction: column;
  }
  .inflight {
    width: auto;
    padding-left: 0;
    border-left: 0;
    padding-top: 10px;
    border-top: 0.5px solid var(--hairline);
  }
}
</style>
