<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ChartLine, ExternalLink } from '@lucide/vue'
import { api } from '@/api/client'
import type { TimeRange, TimeSeries, TimeSeriesData, TimePoint } from '@/api/types'
import { clock, compact, dayLabel, integer, ms, TICKS_PER_USD, usd } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import { goLogs } from '@/links'
import { axisBase, chartColors, tooltipBase } from '@/components/charts/palette'
import Card from '@/components/ui/Card.vue'
import EChart from '@/components/charts/EChart.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Empty from '@/components/ui/Empty.vue'
import { entityName, openEntity, UNKNOWN_KEYS, type Dim, type Focus } from './metrics'

type Group = 'none' | Dim
type TMetric = 'requests' | 'errors' | 'tokens' | 'cost' | 'latency' | 'cache'
type Kind = 'line' | 'stack'

const props = defineProps<{ dim: Dim; focus: Focus }>()

const FOCUS_METRIC: Record<Focus, TMetric> = { requests: 'requests', cost: 'cost', latency: 'latency', cache: 'cache' }

const range = ref<TimeRange>('24h')
const group = ref<Group>(props.dim)
const metric = ref<TMetric>(FOCUS_METRIC[props.focus])
const kind = ref<Kind>('line')
const limit = ref(6)
const data = ref<TimeSeriesData | null>(null)
const loading = ref(false)
const error = ref('')
const hidden = reactive(new Set<string>())

watch(
  () => props.dim,
  (d) => (group.value = d),
)
watch(
  () => props.focus,
  (f) => (metric.value = FOCUS_METRIC[f]),
)

const ratio = computed(() => metric.value === 'latency' || metric.value === 'cache')
const effectiveKind = computed<Kind>(() => (ratio.value ? 'line' : kind.value))

let seq = 0
async function load(): Promise<void> {
  const my = ++seq
  loading.value = true
  try {
    const res = await api.get<TimeSeriesData>('/stats/timeseries', {
      range: range.value,
      groupBy: group.value,
      limit: group.value === 'none' ? undefined : limit.value,
    })
    if (my !== seq) return
    data.value = res.data
    hidden.clear()
    error.value = ''
  } catch (err) {
    if (my === seq) error.value = errorTitle(err)
  } finally {
    if (my === seq) loading.value = false
  }
}

let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(() => document.visibilityState === 'visible' && load(), 60_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
watch([range, group, limit], load)
defineExpose({ reload: load })

function value(p: TimePoint): number | null {
  switch (metric.value) {
    case 'requests':
      return p.total
    case 'errors':
      return p.errorCount
    case 'tokens':
      return p.inputTokens + p.outputTokens + p.cacheReadTokens + p.cacheCreationTokens
    case 'cost':
      return p.costTicks / TICKS_PER_USD
    case 'latency':
      return p.averageFirstTokenMilliseconds
    case 'cache': {
      const d = p.inputTokens + p.cacheReadTokens + p.cacheCreationTokens
      return d ? (p.cacheReadTokens / d) * 100 : null
    }
  }
}

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  if (metric.value === 'cost') return usd(v * TICKS_PER_USD)
  if (metric.value === 'latency') return ms(v)
  if (metric.value === 'cache') return `${v.toFixed(1)}%`
  if (metric.value === 'tokens') return compact(v)
  return integer(v)
}

function label(t: string): string {
  if (!data.value) return ''
  if (data.value.bucket === 'day') return dayLabel(t)
  if (range.value === '1h' || range.value === '24h') return clock(t, false)
  return `${dayLabel(t)} ${clock(t, false)}`
}

function seriesName(s: TimeSeries): string {
  if (s.key === 'total') return '全部'
  if (group.value === 'none') return s.label || s.key
  return entityName(group.value, s.key)
}

const colors = computed(() => chartColors().series)
function colorOf(i: number, key: string): string {
  return key === '__other__' ? chartColors().axis : colors.value[i % colors.value.length]
}

/** One legend entry per series with its window total (or average for ratio metrics). */
const legend = computed(() =>
  (data.value?.series || []).map((s, i) => {
    let sum = 0
    let wsum = 0
    let weight = 0
    for (const p of s.points) {
      const v = value(p)
      if (v === null) continue
      sum += v
      const w = metric.value === 'latency' ? p.successCount : p.inputTokens + p.cacheReadTokens + p.cacheCreationTokens
      wsum += v * w
      weight += w
    }
    const agg = ratio.value ? (weight ? wsum / weight : null) : sum
    return { key: s.key, name: seriesName(s), color: colorOf(i, s.key), agg, clickable: group.value !== 'none' && !UNKNOWN_KEYS.has(s.key) }
  }),
)

const option = computed(() => {
  const c = chartColors()
  const base = axisBase(c)
  const all = data.value?.series || []
  const x = (all[0]?.points || []).map((p) => label(p.t))
  const stack = effectiveKind.value === 'stack'
  const series = all
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => !hidden.has(s.key))
    .map(({ s, i }) => {
      const color = colorOf(i, s.key)
      return {
        name: seriesName(s),
        type: 'line',
        stack: stack ? 'total' : undefined,
        smooth: 0.25,
        showSymbol: false,
        symbolSize: 5,
        connectNulls: false,
        data: s.points.map((p) => value(p)),
        lineStyle: { width: stack ? 1 : 1.8, color },
        itemStyle: { color },
        areaStyle: stack ? { color, opacity: 0.38 } : all.length === 1 ? { color, opacity: 0.12 } : undefined,
        emphasis: { focus: 'series' },
      }
    })
  return {
    animationDuration: 400,
    grid: { left: 4, right: 10, top: 14, bottom: 4, containLabel: true },
    tooltip: {
      ...tooltipBase(c),
      trigger: 'axis',
      confine: true,
      order: 'valueDesc',
      valueFormatter: (v: number | null) => fmt(v),
    },
    xAxis: { type: 'category', data: x, boundaryGap: false, ...base, axisLabel: { ...base.axisLabel, hideOverlap: true } },
    yAxis: {
      type: 'value',
      ...base,
      splitNumber: 4,
      max: metric.value === 'cache' ? 100 : undefined,
      axisLabel: {
        ...base.axisLabel,
        formatter: (v: number) =>
          metric.value === 'cost' ? `$${compact(v)}` : metric.value === 'latency' ? ms(v) : metric.value === 'cache' ? `${v}%` : compact(v),
      },
    },
    series,
  }
})

const visible = computed(() => (data.value?.series || []).filter((s) => !hidden.has(s.key)))

function onPoint(p: unknown): void {
  const { dataIndex, seriesIndex } = p as { dataIndex?: number; seriesIndex?: number }
  if (dataIndex === undefined || seriesIndex === undefined || !data.value) return
  const s = visible.value[seriesIndex]
  const pt = s?.points[dataIndex]
  if (!s || !pt) return
  const from = new Date(pt.t).getTime()
  const filter: Parameters<typeof goLogs>[0] = { from, to: from + data.value.bucketSeconds * 1000 }
  if (metric.value === 'errors') filter.status = 'error'
  if (group.value !== 'none' && !UNKNOWN_KEYS.has(s.key)) {
    if (group.value === 'model') filter.model = s.key
    else if (group.value === 'channel') filter.channel = s.key
    else filter.apiKey = s.key
  }
  goLogs(filter)
}

function toggle(key: string): void {
  if (hidden.has(key)) hidden.delete(key)
  else if (hidden.size < (data.value?.series.length || 0) - 1) hidden.add(key)
}
</script>

<template>
  <Card title="分组趋势" subtitle="按时间分桶；点击图上的点可查看该时段的请求">
    <template #icon><ChartLine /></template>
    <template #actions>
      <Segmented
        v-model="range"
        size="sm"
        :options="[
          { value: '1h', label: '1 小时' },
          { value: '24h', label: '24 小时' },
          { value: '7d', label: '7 天' },
          { value: '30d', label: '30 天' },
        ]"
      />
    </template>
    <div class="controls">
      <Segmented
        v-model="group"
        size="sm"
        :options="[
          { value: 'none', label: '不分组' },
          { value: 'model', label: '按模型' },
          { value: 'channel', label: '按渠道' },
          { value: 'apiKey', label: '按 Key' },
        ]"
      />
      <Segmented
        v-model="metric"
        size="sm"
        :options="[
          { value: 'requests', label: '请求' },
          { value: 'errors', label: '失败' },
          { value: 'tokens', label: 'Token' },
          { value: 'cost', label: '费用' },
          { value: 'latency', label: '首字' },
          { value: 'cache', label: '缓存' },
        ]"
      />
      <div class="spacer" />
      <Select
        v-if="group !== 'none'"
        v-model="limit"
        width="110px"
        :options="[
          { value: 4, label: '前 4 项' },
          { value: 6, label: '前 6 项' },
          { value: 8, label: '前 8 项' },
          { value: 12, label: '前 12 项' },
        ]"
      />
      <span :title="ratio ? '比率和耗时不能叠加，只能用折线显示' : undefined">
        <Segmented
          v-model="kind"
          size="sm"
          :options="[
            { value: 'line', label: '折线' },
            { value: 'stack', label: '堆叠' },
          ]"
          :class="{ locked: ratio }"
        />
      </span>
    </div>
    <p v-if="error" class="na">{{ error }}</p>
    <template v-else>
      <Skeleton v-if="loading && !data" h="260px" r="10px" />
      <Empty v-else-if="data && !data.series.length" title="这段时间没有请求" />
      <div v-else class="chart-box" :class="{ dim: loading }">
        <EChart :option="option" height="100%" :not-merge="true" @click="onPoint" />
      </div>
      <ul v-if="legend.length > 1 || group !== 'none'" class="legend">
        <li v-for="l in legend" :key="l.key" :class="{ off: hidden.has(l.key) }">
          <button type="button" class="sw-btn" :title="hidden.has(l.key) ? '显示这条线' : '隐藏这条线'" @click="toggle(l.key)">
            <i class="sw" :style="{ background: l.color }" />
          </button>
          <button v-if="l.clickable" type="button" class="nm link truncate" :title="`查看 ${l.name}`" @click="openEntity(group as Dim, l.key)">
            {{ l.name }}
          </button>
          <span v-else class="nm truncate">{{ l.name }}</span>
          <span class="agg mono">{{ fmt(l.agg) }}</span>
          <ExternalLink v-if="l.clickable" class="ext" />
        </li>
      </ul>
    </template>
  </Card>
</template>

<style scoped>
.controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.spacer {
  flex: 1;
}
.locked {
  opacity: 0.45;
  pointer-events: none;
}
.chart-box {
  position: relative;
  height: 280px;
  transition: opacity 0.2s;
}
.chart-box.dim {
  opacity: 0.55;
}
.chart-box > :deep(.echart) {
  position: absolute;
  inset: 0;
  height: 100% !important;
}
.legend {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 2px 16px;
}
.legend li {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  height: 26px;
  font-size: 12px;
}
.legend li.off {
  opacity: 0.4;
}
.sw-btn {
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}
.sw {
  width: 9px;
  height: 9px;
  border-radius: 3px;
}
.nm {
  flex: 1;
  text-align: left;
}
.link:hover {
  color: var(--accent);
}
.agg {
  font-size: 11.5px;
  color: var(--text-2);
}
.ext {
  width: 11px;
  height: 11px;
  color: var(--text-3);
  flex-shrink: 0;
}
.na {
  display: grid;
  place-items: center;
  height: 260px;
  color: var(--text-3);
  font-size: 12.5px;
}
</style>
