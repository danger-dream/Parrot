<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: number | null | undefined; warn?: number; danger?: number }>()

const pct = computed(() => Math.max(0, Math.min(100, props.value ?? 0)))
const tone = computed(() => {
  if (props.value === null || props.value === undefined) return 'idle'
  if (pct.value >= (props.danger ?? 90)) return 'danger'
  if (pct.value >= (props.warn ?? 70)) return 'warn'
  return 'ok'
})
</script>

<template>
  <div class="meter" :class="tone" role="meter" :aria-valuenow="pct" aria-valuemin="0" aria-valuemax="100">
    <span :style="{ width: `${pct}%` }" />
  </div>
</template>

<style scoped>
.meter {
  height: 5px;
  border-radius: 999px;
  background: var(--fill-2);
  overflow: hidden;
}
.meter span {
  display: block;
  height: 100%;
  border-radius: inherit;
  transition: width 0.6s var(--ease-out);
}
.ok span { background: linear-gradient(90deg, var(--teal), var(--blue)); }
.warn span { background: linear-gradient(90deg, var(--yellow), var(--orange)); }
.danger span { background: linear-gradient(90deg, var(--orange), var(--red)); }
.idle span { background: transparent; }
</style>
