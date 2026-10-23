<script setup lang="ts">
import { computed } from 'vue'
import type { OutcomeItem } from '@/utils/outcomes'

/**
 * Last 24 hours, one cell per clock hour (newest on the right): grey = no requests, green = all fine,
 * orange = some failures, red = at least a fifth failed. A single failure among hundreds still shows.
 */
const props = withDefaults(defineProps<{
  item?: OutcomeItem | null
  from?: string | null
  step?: number
  count?: number
  width?: number
  gap?: number
  height?: number
}>(), { item: null, from: null, step: 3600, count: 24, width: 4, gap: 2, height: 16 })

const pad = (n: number) => String(n).padStart(2, '0')
const hm = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`

const cells = computed(() => {
  const start = props.from ? Date.parse(props.from) : 0
  return Array.from({ length: props.count }, (_, i) => {
    const b = props.item?.buckets[i]
    const total = b?.total ?? 0
    const errors = b?.errorCount ?? 0
    const tone = !total ? 'none' : !errors ? 'ok' : errors / total >= 0.2 ? 'bad' : 'warn'
    let title = ''
    if (start) {
      const a = new Date(start + i * props.step * 1000)
      const z = new Date(start + (i + 1) * props.step * 1000)
      title = `${hm(a)}–${hm(z)}  `
    }
    title += total ? `${total} 次${errors ? ` · 失败 ${errors}` : ''}${b?.cancelledCount ? ` · 取消 ${b.cancelledCount}` : ''}` : '无请求'
    return { tone, title }
  })
})
</script>

<template>
  <span class="strip" :style="{ gap: `${gap}px`, height: `${height}px` }">
    <i v-for="(c, i) in cells" :key="i" :class="c.tone" :title="c.title" :style="{ width: `${width}px` }" />
  </span>
</template>

<style scoped>
.strip {
  display: inline-flex;
  align-items: stretch;
  flex-shrink: 0;
}
i {
  border-radius: 1.5px;
  background: var(--fill-2);
}
i.ok {
  background: var(--green);
}
i.warn {
  background: var(--orange);
}
i.bad {
  background: var(--red);
}
</style>
