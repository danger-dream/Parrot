<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{
  modelValue: number | null | undefined
  min?: number
  max?: number
  step?: number
  unit?: string
  nullable?: boolean
  placeholder?: string
  disabled?: boolean
  integer?: boolean
  width?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: number | null] }>()

const text = ref(props.modelValue === null || props.modelValue === undefined ? '' : String(props.modelValue))
const invalid = ref(false)

watch(
  () => props.modelValue,
  (v) => {
    const cur = text.value.trim() === '' ? null : Number(text.value)
    if (cur !== v) text.value = v === null || v === undefined ? '' : String(v)
  },
)

function commit(raw: string): void {
  text.value = raw
  const s = raw.trim()
  if (s === '') {
    invalid.value = !props.nullable
    if (props.nullable) emit('update:modelValue', null)
    return
  }
  const n = Number(s)
  const bad =
    !Number.isFinite(n) ||
    (props.integer && !Number.isInteger(n)) ||
    (props.min !== undefined && n < props.min) ||
    (props.max !== undefined && n > props.max)
  invalid.value = bad
  if (!bad) emit('update:modelValue', n)
}
</script>

<template>
  <div class="ctl wrap" :class="{ invalid, disabled }" :style="{ width: width || '140px' }">
    <input
      :value="text"
      inputmode="decimal"
      :placeholder="placeholder ?? (nullable ? '不限' : '')"
      :disabled="disabled"
      @input="commit(($event.target as HTMLInputElement).value)"
    />
    <span v-if="unit" class="unit">{{ unit }}</span>
  </div>
  <p v-if="invalid" class="err">
    {{ integer ? '需要整数' : '需要数字' }}<template v-if="min !== undefined || max !== undefined">，范围 {{ min ?? '-∞' }} ~ {{ max ?? '∞' }}</template>
  </p>
</template>

<style scoped>
.wrap {
  display: flex;
  align-items: center;
  gap: 6px;
}
input {
  flex: 1;
  min-width: 0;
  height: 100%;
  border: 0;
  outline: 0;
  background: none;
  font-family: var(--font-mono);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  color: inherit;
  text-align: right;
}
.unit {
  font-size: 12px;
  color: var(--text-3);
  white-space: nowrap;
}
.err {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--red);
}
</style>
