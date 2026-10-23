<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ outcomes?: readonly string[] | null; slots?: number; height?: number; gap?: number; width?: number }>(),
  { outcomes: () => [], slots: 10, height: 18, gap: 3, width: 5 },
)

const cells = computed(() => {
  const list = (props.outcomes ?? []).slice(-props.slots)
  const pad = props.slots - list.length
  return [...Array.from({ length: pad }, () => 'none'), ...list]
})
const title = computed(() => {
  const list = props.outcomes ?? []
  if (!list.length) return '暂无请求'
  const ok = list.filter((s) => s === 'success').length
  const err = list.filter((s) => s === 'error').length
  return `最近 ${list.length} 次：成功 ${ok} · 失败 ${err}${list.length - ok - err ? ` · 取消 ${list.length - ok - err}` : ''}`
})
</script>

<template>
  <span class="bars" :title="title" :style="{ '--h': `${height}px`, '--g': `${gap}px`, '--w': `${width}px` }">
    <i v-for="(s, i) in cells" :key="i" :class="s" />
  </span>
</template>

<style scoped>
.bars {
  display: inline-flex;
  align-items: stretch;
  gap: var(--g);
  height: var(--h);
  flex-shrink: 0;
}
i {
  width: var(--w);
  border-radius: 2px;
  background: var(--track);
}
i.success {
  background: var(--green);
}
i.error {
  background: var(--red);
}
i.cancelled {
  background: var(--text-3);
  opacity: 0.6;
}
</style>
