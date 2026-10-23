<script setup lang="ts">
import type { Component } from 'vue'
import Spark from './Spark.vue'

export type Tone = 'blue' | 'green' | 'orange' | 'red' | 'purple' | 'teal' | 'cyan' | 'indigo' | 'pink'

withDefaults(
  defineProps<{
    label: string
    value?: string | number | null
    unit?: string
    sub?: string
    icon?: Component
    tone?: Tone
    valueTone?: 'green' | 'red' | 'orange' | ''
    spark?: number[]
    /** Change against the previous period, shown top right (see utils/compare). */
    delta?: { text: string; tone: 'flat' | 'up' | 'down' | 'good' | 'bad'; title?: string } | null
    loading?: boolean
    dense?: boolean
  }>(),
  { value: null, unit: '', sub: '', icon: undefined, tone: 'blue', valueTone: '', spark: undefined, delta: null, loading: false, dense: false },
)
defineEmits<{ click: [] }>()
</script>

<template>
  <div class="tile" :class="[`t-${tone}`, { dense }]" @click="$emit('click')">
    <div class="head">
      <span v-if="icon" class="ic"><component :is="icon" /></span>
      <span class="label">{{ label }}</span>
      <span v-if="delta" class="delta" :class="delta.tone" :title="delta.title">{{ delta.text }}</span>
      <slot name="extra" />
    </div>
    <div class="value num" :class="valueTone ? `v-${valueTone}` : ''">
      <span v-if="loading && (value === null || value === undefined)" class="ph" />
      <template v-else>{{ value ?? '—' }}<small v-if="unit">{{ unit }}</small></template>
    </div>
    <p class="sub"><slot name="sub">{{ sub || '\u00a0' }}</slot></p>
    <Spark v-if="spark && spark.length > 1" class="trend" :values="spark" color="var(--c)" :height="dense ? 22 : 28" />
  </div>
</template>

<style scoped>
.tile {
  --c: var(--blue);
  position: relative;
  min-width: 0;
  min-height: 112px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 18px 14px;
  overflow: hidden;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
}
.t-green { --c: var(--green); }
.t-orange { --c: var(--orange); }
.t-red { --c: var(--red); }
.t-purple { --c: var(--purple); }
.t-teal { --c: var(--teal); }
.t-cyan { --c: var(--cyan); }
.t-indigo { --c: var(--indigo); }
.t-pink { --c: var(--pink); }
/* The label is never squeezed by the change: on a narrow tile the change wraps to its own line. */
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 8px;
  min-width: 0;
}
.ic {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  border-radius: 8px;
  background: color-mix(in srgb, var(--c) 13%, transparent);
  color: var(--c);
}
.ic :deep(svg) {
  width: 16px;
  height: 16px;
}
.label {
  flex: 1 0 auto;
  max-width: calc(100% - 36px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--text-2);
}
.delta {
  flex-shrink: 0;
  margin-left: auto;
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--text-3);
  cursor: default;
}
.delta.good {
  color: var(--green);
}
.delta.bad {
  color: var(--red);
}
.value {
  position: relative;
  z-index: 1;
  font-size: 26px;
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.value small {
  margin-left: 2px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-3);
}
.v-green { color: var(--green); }
.v-red { color: var(--red); }
.v-orange { color: var(--orange); }
.ph {
  display: inline-block;
  width: 72px;
  height: 22px;
  border-radius: 6px;
  background: var(--fill-2);
  vertical-align: middle;
}
.sub {
  position: relative;
  z-index: 1;
  font-size: 12px;
  line-height: 1.45;
  color: var(--text-3);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}
.trend {
  margin-top: auto;
  padding-top: 2px;
}
.dense {
  min-height: 94px;
  gap: 6px;
  padding: 12px 14px 10px;
}
.dense .ic {
  width: 24px;
  height: 24px;
  border-radius: 7px;
}
.dense .ic :deep(svg) {
  width: 14px;
  height: 14px;
}
.dense .label {
  font-size: 12.5px;
}
.dense .value {
  font-size: 21px;
}
</style>
