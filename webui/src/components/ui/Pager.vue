<script setup lang="ts">
import { computed } from 'vue'
import { ChevronLeft, ChevronRight } from '@lucide/vue'

const props = defineProps<{ page: number; pageSize: number; total: number; disabled?: boolean }>()
const emit = defineEmits<{ 'update:page': [page: number] }>()
const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
</script>

<template>
  <div v-if="pages > 1" class="pager">
    <span class="info">共 {{ total }} 条 · 第 {{ page }}/{{ pages }} 页</span>
    <button type="button" :disabled="disabled || page <= 1" aria-label="上一页" @click="emit('update:page', page - 1)"><ChevronLeft /></button>
    <button type="button" :disabled="disabled || page >= pages" aria-label="下一页" @click="emit('update:page', page + 1)"><ChevronRight /></button>
  </div>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  padding: 8px 0 0;
}
.info {
  margin-right: auto;
  font-size: 12px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
button {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  background: var(--fill-2);
  color: var(--text-2);
}
button:hover:not(:disabled) {
  background: var(--fill);
  color: var(--text);
}
button svg {
  width: 15px;
  height: 15px;
}
</style>
