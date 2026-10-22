<script setup lang="ts">
import { computed, type Component } from 'vue'
import { TrendingDown, TrendingUp } from '@lucide/vue'
import Spark from '@/components/ui/Spark.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

const props = defineProps<{
  label: string
  value: string | null
  unit?: string
  sub?: string
  delta?: number | null
  deltaGoodWhenUp?: boolean
  neutralDelta?: boolean
  spark?: number[]
  tint: string
  icon: Component
}>()

const deltaText = computed(() => {
  if (props.delta === null || props.delta === undefined || !Number.isFinite(props.delta)) return ''
  const v = props.delta * 100
  if (Math.abs(v) < 0.5) return '与昨天持平'
  return `${v > 0 ? '+' : ''}${v.toFixed(Math.abs(v) >= 10 ? 0 : 1)}% 较昨日同期`
})
const deltaTone = computed(() => {
  if (props.neutralDelta || !props.delta || Math.abs(props.delta) < 0.005) return 'flat'
  const up = props.delta > 0
  const goodUp = props.deltaGoodWhenUp ?? true
  return up === goodUp ? 'good' : 'bad'
})
</script>

<template>
  <div class="kpi" :style="{ '--tint': tint }">
    <div class="top">
      <span class="ic"><component :is="icon" /></span>
      <span class="label">{{ label }}</span>
    </div>
    <div class="value">
      <template v-if="value !== null">
        <span class="num">{{ value }}</span><span v-if="unit" class="unit">{{ unit }}</span>
      </template>
      <Skeleton v-else w="60%" h="28px" />
    </div>
    <p v-if="sub" class="sub">{{ sub }}</p>
    <p v-if="deltaText" class="delta" :class="deltaTone">
      <TrendingUp v-if="(delta ?? 0) > 0.005" />
      <TrendingDown v-else-if="(delta ?? 0) < -0.005" />
      {{ deltaText }}
    </p>
    <div v-if="spark && spark.length > 1" class="spark"><Spark :values="spark" :color="tint" :height="34" highlight-last /></div>
  </div>
</template>

<style scoped>
.kpi {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding: 14px 16px 12px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  overflow: hidden;
}
.kpi::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: radial-gradient(120% 80% at 100% 0%, color-mix(in srgb, var(--tint) 10%, transparent), transparent 60%);
}
.top {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ic {
  width: 24px;
  height: 24px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  color: var(--tint);
  background: color-mix(in srgb, var(--tint) 15%, transparent);
}
.ic svg {
  width: 14px;
  height: 14px;
}
.label {
  font-size: 12px;
  color: var(--text-2);
  font-weight: 500;
}
.value {
  display: flex;
  align-items: baseline;
  gap: 3px;
  margin-top: 6px;
  min-height: 32px;
}
.num {
  font-family: var(--font-display);
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.unit {
  font-size: 13px;
  color: var(--text-2);
  font-weight: 500;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.delta {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  color: var(--text-3);
}
.delta svg {
  width: 13px;
  height: 13px;
}
.delta.good {
  color: var(--green);
}
.delta.bad {
  color: var(--red);
}
.spark {
  margin: 6px -16px -12px;
}
</style>
