<script setup lang="ts" generic="T extends string">
import { computed } from 'vue'

const props = defineProps<{
  modelValue: T
  options: { value: T; label: string }[]
  size?: 'sm' | 'md'
}>()
const emit = defineEmits<{ 'update:modelValue': [value: T] }>()

const index = computed(() => Math.max(0, props.options.findIndex((o) => o.value === props.modelValue)))
</script>

<template>
  <div class="seg" :class="size === 'sm' ? 's-sm' : ''" role="tablist" :style="{ '--n': options.length, '--i': index }">
    <span class="thumb" aria-hidden="true" />
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      role="tab"
      :aria-selected="opt.value === modelValue"
      :class="{ on: opt.value === modelValue }"
      @click="emit('update:modelValue', opt.value)"
    >
      {{ opt.label }}
    </button>
  </div>
</template>

<style scoped>
.seg {
  position: relative;
  display: inline-grid;
  grid-template-columns: repeat(var(--n), minmax(0, 1fr));
  padding: 2px;
  border-radius: 8px;
  background: var(--fill-2);
  box-shadow: inset 0 0 0 0.5px var(--border);
  flex-shrink: 0;
}
.thumb {
  position: absolute;
  top: 2px;
  bottom: 2px;
  left: 2px;
  width: calc((100% - 4px) / var(--n));
  transform: translateX(calc(var(--i) * 100%));
  border-radius: 6px;
  background: var(--surface-raised);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18), 0 0 0 0.5px var(--border);
  transition: transform 0.28s var(--ease-out);
}
button {
  position: relative;
  z-index: 1;
  height: 24px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-2);
  white-space: nowrap;
  transition: color 0.15s;
}
button.on {
  color: var(--text);
}
.s-sm button {
  height: 20px;
  padding: 0 9px;
  font-size: 11.5px;
}
</style>
