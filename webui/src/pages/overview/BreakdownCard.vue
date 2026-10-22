<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChartPie } from '@lucide/vue'
import { api } from '@/api/client'
import type { StatsBreakdownData, StatsPeriod } from '@/api/types'
import { channelName } from '@/stores/catalog'
import { compact, integer, percent, usd } from '@/utils/format'
import { chartColors, tooltipBase } from '@/components/charts/palette'
import Card from '@/components/ui/Card.vue'
import EChart from '@/components/charts/EChart.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

type Dim = 'model' | 'channel' | 'apiKey'
type Sort = 'total' | 'cost'

const dim = ref<Dim>('model')
const period = ref<StatsPeriod>('today')
const sort = ref<Sort>('total')
const rows = ref<StatsBreakdownData[]>([])
const loading = ref(false)

const TOP = 7

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await api.get<StatsBreakdownData[]>('/stats/breakdown', {
      dimension: dim.value,
      period: period.value,
      sort: sort.value,
      descending: true,
      pageSize: 100,
    })
    rows.value = res.data
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch([dim, period, sort], load)
defineExpose({ reload: load })

function name(key: string): string {
  return dim.value === 'channel' ? channelName(key) : key || '（未知）'
}

function valueOf(r: StatsBreakdownData): number {
  return sort.value === 'cost' ? r.metrics.costTicks : r.metrics.total
}

const sum = computed(() => rows.value.reduce((a, r) => a + valueOf(r), 0))
const top = computed(() => rows.value.slice(0, TOP))
const rest = computed(() => rows.value.slice(TOP).reduce((a, r) => a + valueOf(r), 0))

const option = computed(() => {
  const c = chartColors()
  const data = top.value.map((r, i) => ({ name: name(r.key), value: valueOf(r), itemStyle: { color: c.series[i % c.series.length] } }))
  if (rest.value > 0) data.push({ name: '其他', value: rest.value, itemStyle: { color: c.grid } })
  return {
    tooltip: {
      ...tooltipBase(c),
      trigger: 'item',
      valueFormatter: (v: number) => (sort.value === 'cost' ? usd(v) : integer(v)),
    },
    series: [
      {
        type: 'pie',
        radius: ['62%', '90%'],
        avoidLabelOverlap: true,
        padAngle: 1.5,
        itemStyle: { borderRadius: 4 },
        label: { show: false },
        data,
      },
    ],
  }
})

const colors = computed(() => chartColors().series)
</script>

<template>
  <Card title="分布">
    <template #icon><ChartPie /></template>
    <template #actions>
      <Segmented
        v-model="period"
        size="sm"
        :options="[
          { value: 'today', label: '今天' },
          { value: '7d', label: '7 天' },
          { value: 'month', label: '本月' },
        ]"
      />
    </template>
    <div class="controls">
      <Segmented
        v-model="dim"
        size="sm"
        :options="[
          { value: 'model', label: '模型' },
          { value: 'channel', label: '渠道' },
          { value: 'apiKey', label: 'Key' },
        ]"
      />
      <Segmented
        v-model="sort"
        size="sm"
        :options="[
          { value: 'total', label: '按请求' },
          { value: 'cost', label: '按费用' },
        ]"
      />
    </div>
    <div v-if="loading && !rows.length" class="sk"><Skeleton h="150px" w="150px" r="50%" /><Skeleton h="150px" /></div>
    <p v-else-if="!rows.length" class="none">这段时间没有请求</p>
    <div v-else class="body">
      <div class="donut">
        <EChart :option="option" height="150px" :not-merge="true" />
        <div class="center">
          <p class="c-v">{{ sort === 'cost' ? usd(sum) : compact(sum) }}</p>
          <p class="c-k">{{ sort === 'cost' ? '总费用' : '总请求' }}</p>
        </div>
      </div>
      <ol class="list">
        <li v-for="(r, i) in top" :key="r.key">
          <i class="sw" :style="{ background: colors[i % colors.length] }" />
          <span class="n truncate" :title="name(r.key)">{{ name(r.key) }}</span>
          <span class="share mono">{{ percent(valueOf(r), sum, 0) }}</span>
          <span class="val mono">{{ sort === 'cost' ? usd(r.metrics.costTicks) : integer(r.metrics.total) }}</span>
        </li>
        <li v-if="rest > 0" class="other">
          <i class="sw" />
          <span class="n">其他 {{ rows.length - TOP }} 项</span>
          <span class="share mono">{{ percent(rest, sum, 0) }}</span>
          <span class="val mono">{{ sort === 'cost' ? usd(rest) : integer(rest) }}</span>
        </li>
      </ol>
    </div>
  </Card>
</template>

<style scoped>
.controls {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}
.body {
  display: flex;
  align-items: center;
  gap: 16px;
}
.donut {
  position: relative;
  width: 150px;
  flex-shrink: 0;
}
.center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}
.c-v {
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.c-k {
  font-size: 10.5px;
  color: var(--text-3);
}
.list {
  flex: 1;
  min-width: 0;
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.list li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.sw {
  width: 8px;
  height: 8px;
  border-radius: 3px;
  flex-shrink: 0;
  background: var(--fill);
}
.n {
  flex: 1;
}
.share {
  width: 34px;
  text-align: right;
  color: var(--text-3);
  font-size: 11px;
}
.val {
  min-width: 52px;
  text-align: right;
  font-size: 11.5px;
}
.other .n {
  color: var(--text-3);
}
.none {
  padding: 40px 0;
  text-align: center;
  color: var(--text-3);
}
.sk {
  display: flex;
  gap: 16px;
}
@media (max-width: 420px) {
  .body {
    flex-direction: column;
    align-items: stretch;
  }
  .donut {
    align-self: center;
  }
}
</style>
