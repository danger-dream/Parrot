<script setup lang="ts">
import { computed } from 'vue'
import type { TimePoint } from '@/api/types'
import { chartColors, tooltipBase } from '@/components/charts/palette'
import EChart from '@/components/charts/EChart.vue'
import Card from '@/components/ui/Card.vue'
import { axisNumber, compact, integer, usd } from '@/utils/format'

const props = defineProps<{ points: TimePoint[]; ready: boolean }>()

const option = computed(() => {
  const c = chartColors()
  const pts = props.points
  const labels = pts.map((p) => {
    const d = new Date(p.t)
    return `${String(d.getHours()).padStart(2, '0')}:00`
  })
  const tokens = pts.map((p) => p.inputTokens + p.cacheReadTokens + p.cacheCreationTokens + p.outputTokens)
  return {
    animation: false,
    grid: { left: 46, right: 40, top: 16, bottom: 26 },
    tooltip: {
      ...tooltipBase(c),
      trigger: 'axis',
      formatter: (items: { dataIndex: number }[]) => {
        const p = pts[items[0]?.dataIndex ?? 0]
        if (!p) return ''
        return [
          `<b>${labels[items[0].dataIndex]}</b>`,
          `请求 ${integer(p.total)}（失败 ${integer(p.errorCount)}）`,
          `Token ${compact(tokens[items[0].dataIndex])}`,
          `金额 ${usd(p.costTicks)}`,
        ].join('<br>')
      },
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: c.grid } },
      axisTick: { show: false },
      axisLabel: { color: c.axis, fontSize: 11, interval: 3 },
    },
    yAxis: [
      {
        type: 'value',
        splitLine: { lineStyle: { color: c.grid, type: 'dashed' } },
        axisLabel: { color: c.axis, fontSize: 11, formatter: (v: number) => axisNumber(v) },
      },
      {
        type: 'value',
        splitLine: { show: false },
        axisLabel: { color: c.axis, fontSize: 11, formatter: (v: number) => axisNumber(v) },
      },
    ],
    series: [
      {
        name: 'Token',
        type: 'bar',
        data: tokens,
        barMaxWidth: 16,
        itemStyle: { color: '#10b981', borderRadius: [3, 3, 0, 0] },
      },
      {
        name: '请求',
        type: 'line',
        yAxisIndex: 1,
        data: pts.map((p) => p.total),
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { width: 2, color: c.blue },
        itemStyle: { color: c.blue },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: `${c.blue}33` }, { offset: 1, color: `${c.blue}00` }] } },
      },
    ],
  }
})
</script>

<template>
  <Card title="流量趋势（近 24 小时）">
    <template #actions>
      <span class="legend"><i class="lb" />请求</span>
      <span class="legend"><i class="lg" />Token</span>
    </template>
    <div class="box">
      <EChart v-if="ready && points.length" :option="option" height="262px" />
    </div>
  </Card>
</template>

<style scoped>
.box {
  height: 262px;
}
.legend {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-2);
}
.legend i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.lb {
  background: var(--blue);
}
.lg {
  background: #10b981;
}
</style>
