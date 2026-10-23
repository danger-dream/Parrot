<script setup lang="ts">
import { computed, useId } from 'vue'

const props = withDefaults(
  defineProps<{ values: number[]; color?: string; height?: number; highlightLast?: boolean }>(),
  { color: 'var(--blue)', height: 32 },
)

const id = useId()
const W = 120

const geometry = computed(() => {
  const vals = props.values.length ? props.values : [0]
  const max = Math.max(...vals, 1e-9)
  const min = Math.min(...vals, 0)
  const span = max - min || 1
  const step = vals.length > 1 ? W / (vals.length - 1) : W
  const pts = vals.map((v, i) => [i * step, props.height - 2 - ((v - min) / span) * (props.height - 6)] as const)
  const line = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const area = `${line} L${W},${props.height} L0,${props.height} Z`
  const last = pts[pts.length - 1]
  return { line, area, last }
})
</script>

<template>
  <svg class="spark" :viewBox="`0 0 ${W} ${height}`" preserveAspectRatio="none" :style="{ height: `${height}px` }" aria-hidden="true">
    <defs>
      <linearGradient :id="`g${id}`" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" :style="{ stopColor: color, stopOpacity: 0.32 }" />
        <stop offset="100%" :style="{ stopColor: color, stopOpacity: 0 }" />
      </linearGradient>
    </defs>
    <path :d="geometry.area" :fill="`url(#g${id})`" />
    <path :d="geometry.line" fill="none" :style="{ stroke: color }" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round" />
    <circle v-if="highlightLast" :cx="geometry.last[0]" :cy="geometry.last[1]" r="2.4" :style="{ fill: color }" />
  </svg>
</template>

<style scoped>
.spark {
  display: block;
  width: 100%;
  overflow: visible;
}
</style>
