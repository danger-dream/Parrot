<script setup lang="ts">
import { Plus, X } from '@lucide/vue'
import NumberInput from '@/components/ui/NumberInput.vue'

// Editor for a short ordered list of numbers (cooldown ladder, retry backoff).
const props = withDefaults(
  defineProps<{
    modelValue: number[]
    min?: number
    max?: number
    integer?: boolean
    unit?: string
    minItems?: number
    maxItems?: number
    /** Label in front of each item, e.g. (i) => `第 ${i + 1} 次`. */
    itemLabel?: (index: number) => string
    /** Optional caption after each item, e.g. “永久” for 0. */
    caption?: (value: number, index: number) => string
    defaultValue?: number
  }>(),
  { minItems: 1, maxItems: 20, defaultValue: 1 },
)
const emit = defineEmits<{ 'update:modelValue': [value: number[]] }>()

function set(i: number, v: number | null): void {
  if (v === null) return
  const next = [...props.modelValue]
  next[i] = v
  emit('update:modelValue', next)
}
function remove(i: number): void {
  const next = [...props.modelValue]
  next.splice(i, 1)
  emit('update:modelValue', next)
}
function add(): void {
  const last = props.modelValue[props.modelValue.length - 1]
  emit('update:modelValue', [...props.modelValue, last ?? props.defaultValue])
}
</script>

<template>
  <div class="nl">
    <div v-for="(v, i) in modelValue" :key="i" class="item">
      <span class="lab">{{ itemLabel ? itemLabel(i) : `#${i + 1}` }}</span>
      <NumberInput :model-value="v" :min="min" :max="max" :integer="integer" :unit="unit" width="120px" @update:model-value="set(i, $event)" />
      <span v-if="caption" class="cap">{{ caption(v, i) }}</span>
      <button type="button" class="rm" :disabled="modelValue.length <= minItems" aria-label="删除" @click="remove(i)"><X /></button>
    </div>
    <button v-if="modelValue.length < maxItems" type="button" class="add" @click="add"><Plus /> 添加一项</button>
  </div>
</template>

<style scoped>
.nl {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.item {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.lab {
  width: 64px;
  font-size: 12px;
  color: var(--text-2);
  white-space: nowrap;
}
.cap {
  font-size: 11.5px;
  color: var(--text-3);
  min-width: 0;
}
.rm {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  margin-left: auto;
  border-radius: 6px;
  color: var(--text-3);
}
.rm:hover:not(:disabled) {
  color: var(--red);
  background: var(--fill-2);
}
.rm svg {
  width: 14px;
  height: 14px;
}
.add {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  align-self: flex-start;
  height: 28px;
  padding: 0 8px;
  border-radius: 6px;
  font-size: 12px;
  color: var(--accent);
}
.add:hover {
  background: var(--accent-soft);
}
.add svg {
  width: 13px;
  height: 13px;
}
</style>
