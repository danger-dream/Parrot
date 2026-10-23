<script setup lang="ts">
import { Search, X } from '@lucide/vue'

defineProps<{ modelValue: string; placeholder?: string; width?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<template>
  <div class="ctl sf" :style="{ width: width || '240px' }">
    <Search class="ic" />
    <input
      :value="modelValue"
      :placeholder="placeholder || '搜索'"
      spellcheck="false"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
    <button v-if="modelValue" type="button" class="x" aria-label="清除" @click="emit('update:modelValue', '')"><X /></button>
  </div>
</template>

<style scoped>
.sf {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 6px 0 9px;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.ic {
  width: 13px;
  height: 13px;
  color: var(--text-3);
  flex-shrink: 0;
}
input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: none;
  font-size: 12.5px;
  color: inherit;
}
.x {
  display: grid;
  color: var(--text-3);
}
.x svg {
  width: 13px;
  height: 13px;
}
@media (max-width: 767px) {
  .sf {
    width: 100% !important;
  }
}
</style>
