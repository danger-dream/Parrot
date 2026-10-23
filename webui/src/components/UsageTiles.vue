<script setup lang="ts">
import type { Delta } from '@/utils/compare'

export interface UsageTile {
  k: string
  v: string
  /** Secondary line under the value. */
  s?: string
  bad?: boolean
  /** Hover text for the whole tile. */
  t?: string
  delta?: Delta | null
}

defineProps<{ tiles: UsageTile[] }>()
</script>

<template>
  <div class="cells">
    <!-- The last tile fills its row, so a short last row never leaves an empty gap. -->
    <div v-for="(x, i) in tiles" :key="x.k" :title="x.t" :style="i === tiles.length - 1 ? { gridColumn: `span ${3 - (i % 3)}` } : undefined">
      <p class="ck">
        <span>{{ x.k }}</span>
        <span v-if="x.delta" class="delta" :class="x.delta.tone" :title="x.delta.title">{{ x.delta.text }}</span>
      </p>
      <p class="cv">{{ x.v }}</p>
      <p v-if="x.s" class="cs" :class="{ 'c-red': x.bad }">{{ x.s }}</p>
    </div>
  </div>
</template>

<style scoped>
/* Every tile draws its own top/left line; the container clips the outer ones. */
.cells {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  overflow: hidden;
}
.cells > div {
  margin: -0.5px 0 0 -0.5px;
  padding: 8px 11px;
  min-width: 0;
  border-top: 0.5px solid var(--hairline);
  border-left: 0.5px solid var(--hairline);
}
.ck {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0 6px;
  font-size: 11px;
  color: var(--text-3);
}
.ck > span:first-child {
  white-space: nowrap;
}
.delta {
  flex-shrink: 0;
  margin-left: auto;
  font-size: 10.5px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.delta.good {
  color: var(--green);
}
.delta.bad {
  color: var(--red);
}
.cv {
  margin-top: 2px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cs {
  margin-top: 1px;
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
