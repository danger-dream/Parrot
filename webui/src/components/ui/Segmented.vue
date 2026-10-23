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
  border-radius: 9px;
  background: var(--fill-2);
  flex-shrink: 0;
}
.thumb {
  position: absolute;
  top: 2px;
  bottom: 2px;
  left: 2px;
  width: calc((100% - 4px) / var(--n));
  transform: translateX(calc(var(--i) * 100%));
  border-radius: 7px;
  background: var(--surface-raised);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12), 0 0 0 0.5px rgba(0, 0, 0, 0.06);
  transition: transform 0.24s var(--ease-out);
}
:root[data-theme='dark'] .thumb {
  background: #636366;
}
/* One line while there is room; a squeezed control (a phone-width dialog) wraps instead of
   letting the label run out of its segment. */
button {
  position: relative;
  z-index: 1;
  min-width: 0;
  min-height: 26px;
  padding: 3px 13px;
  font-size: 12.5px;
  font-weight: 500;
  line-height: 1.2;
  color: var(--text-2);
  overflow-wrap: anywhere;
  transition: color 0.15s;
}
button:hover {
  color: var(--text);
}
button.on {
  color: var(--text);
  font-weight: 600;
}
.s-sm button {
  min-height: 22px;
  padding: 2px 10px;
  font-size: 12px;
}
</style>
