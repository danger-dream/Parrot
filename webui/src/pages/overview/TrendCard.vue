<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChartColumn } from '@lucide/vue'
import { api, ApiError } from '@/api/client'
import type { TimeRange, TimeSeriesData } from '@/api/types'
import { clock, compact, dayLabel, integer, TICKS_PER_USD, usd } from '@/utils/format'
import { axisBase, chartColors, tooltipBase } from '@/components/charts/palette'
import Card from '@/components/ui/Card.vue'
import EChart from '@/components/charts/EChart.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

type Metric = 'requests' | 'tokens' | 'cost'

const range = ref<TimeRange>('24h')
const metric = ref<Metric>('requests')
const data = ref<TimeSeriesData | null>(null)
const loading = ref(false)
const unavailable = ref('')

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await api.get<TimeSeriesData>('/stats/timeseries', { range: range.value, bucket: range.value === '30d' ? 'day' : 'hour' })
    data.value = res.data
    unavailable.value = ''
  } catch (err) {
    unavailable.value = err instanceof ApiError && (err.status === 404 || err.status === 405) ? '趋势统计接口尚未启用' : (err as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(range, load)
defineExpose({ reload: load })

const points = computed(() => data.value?.series.find((s) => s.key === 'total')?.points || data.value?.series[0]?.points || [])

const totals = computed(() => {
  let req = 0
  let err = 0
  let tok = 0
  let cost = 0
  for (const p of points.value) {
    req += p.total
    err += p.errorCount
    tok += p.inputTokens + p.outputTokens + p.cacheReadTokens + p.cacheCreationTokens
    cost += p.costTicks
  }
  return { req, err, tok, cost }
})

function label(t: string): string {
  if (data.value?.bucket === 'day') return dayLabel(t)
  if (range.value === '24h') return clock(t, false)
  return `${dayLabel(t)} ${clock(t, false)}`
}

const option = computed(() => {
  const c = chartColors()
  const base = axisBase(c)
  const x = points.value.map((p) => label(p.t))
  const bar = (name: string, values: number[], color: string, top = false) => ({
    name,
    type: 'bar',
    stack: 'a',
    data: values,
    barMaxWidth: 18,
    itemStyle: { color, borderRadius: top ? [3, 3, 0, 0] : 0 },
    emphasis: { focus: 'series' },
  })
  let series: unknown[] = []
  let fmt = (v: number) => compact(v)
  if (metric.value === 'requests') {
    series = [
      bar('成功', points.value.map((p) => p.total - p.errorCount), c.blue),
      bar('失败', points.value.map((p) => p.errorCount), c.red, true),
    ]
    fmt = (v) => integer(v)
  } else if (metric.value === 'tokens') {
    series = [
      bar('缓存读取', points.value.map((p) => p.cacheReadTokens), c.teal),
      bar('缓存写入', points.value.map((p) => p.cacheCreationTokens), c.indigo),
      bar('输入', points.value.map((p) => p.inputTokens), c.blue),
      bar('输出', points.value.map((p) => p.outputTokens), c.purple, true),
    ]
  } else {
    series = [bar('费用', points.value.map((p) => p.costTicks / TICKS_PER_USD), c.orange, true)]
    fmt = (v) => usd(v * TICKS_PER_USD)
  }
  return {
    animationDuration: 500,
    grid: { left: 4, right: 8, top: 12, bottom: 4, containLabel: true },
    tooltip: {
      ...tooltipBase(c),
      trigger: 'axis',
      axisPointer: { type: 'shadow', shadowStyle: { color: c.grid } },
      valueFormatter: (v: number) => fmt(v),
    },
    xAxis: { type: 'category', data: x, ...base, axisLabel: { ...base.axisLabel, hideOverlap: true } },
    yAxis: { type: 'value', ...base, splitNumber: 4, axisLabel: { ...base.axisLabel, formatter: (v: number) => (metric.value === 'cost' ? `$${compact(v)}` : compact(v)) } },
    series,
  }
})
</script>

<template>
  <Card title="趋势">
    <template #icon><ChartColumn /></template>
    <template #actions>
      <Segmented
        v-model="metric"
        size="sm"
        :options="[
          { value: 'requests', label: '请求' },
          { value: 'tokens', label: 'Token' },
          { value: 'cost', label: '费用' },
        ]"
      />
      <Segmented
        v-model="range"
        size="sm"
        :options="[
          { value: '24h', label: '24 小时' },
          { value: '7d', label: '7 天' },
          { value: '30d', label: '30 天' },
        ]"
      />
    </template>
    <div v-if="unavailable" class="na">{{ unavailable }}</div>
    <template v-else>
      <div class="totals">
        <div><p class="k">请求</p><p class="v">{{ integer(totals.req) }}</p></div>
        <div><p class="k">失败</p><p class="v" :class="{ red: totals.err }">{{ integer(totals.err) }}</p></div>
        <div><p class="k">Token</p><p class="v">{{ compact(totals.tok) }}</p></div>
        <div><p class="k">费用</p><p class="v">{{ usd(totals.cost) }}</p></div>
      </div>
      <Skeleton v-if="loading && !data" h="220px" r="10px" />
      <EChart v-else :option="option" height="230px" :not-merge="true" />
    </template>
  </Card>
</template>

<style scoped>
.totals {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 28px;
  margin-bottom: 8px;
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
.v.red {
  color: var(--red);
}
.na {
  display: grid;
  place-items: center;
  height: 240px;
  color: var(--text-3);
  font-size: 12.5px;
}
</style>
