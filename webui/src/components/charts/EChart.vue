<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'
import { ui } from '@/stores/ui'

echarts.use([BarChart, LineChart, PieChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer])

const props = defineProps<{ option: EChartsCoreOption; height?: string; notMerge?: boolean }>()
const emit = defineEmits<{ click: [params: unknown] }>()

const el = ref<HTMLDivElement | null>(null)
const chart = shallowRef<echarts.ECharts | null>(null)
let observer: ResizeObserver | null = null

function render(replace = false): void {
  if (!chart.value) return
  chart.value.setOption(props.option, { notMerge: replace || props.notMerge, lazyUpdate: true })
}

onMounted(() => {
  if (!el.value) return
  chart.value = echarts.init(el.value, undefined, { renderer: 'canvas' })
  chart.value.on('click', (p) => emit('click', p))
  render(true)
  observer = new ResizeObserver(() => chart.value?.resize())
  observer.observe(el.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  chart.value?.dispose()
})

watch(() => props.option, () => render(), { deep: false })
watch(
  () => ui.dark,
  () => render(true),
)
</script>

<template>
  <div ref="el" class="echart" :style="{ height: height || '220px' }" />
</template>

<style scoped>
.echart {
  width: 100%;
  min-width: 0;
}
</style>
