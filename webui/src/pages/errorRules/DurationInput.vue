<script setup lang="ts">
import { ref, watch } from 'vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Select from '@/components/ui/Select.vue'

const props = withDefaults(defineProps<{ modelValue: number | null | undefined; allowZero?: boolean; max?: number }>(), {
  allowZero: false,
  max: 30 * 86400,
})
const emit = defineEmits<{ 'update:modelValue': [value: number] }>()

const UNITS = [
  { value: 1, label: '秒' },
  { value: 60, label: '分钟' },
  { value: 3600, label: '小时' },
  { value: 86400, label: '天' },
]

function bestUnit(seconds: number): number {
  for (const u of [86400, 3600, 60]) if (seconds >= u && seconds % u === 0) return u
  return seconds && seconds < 60 ? 1 : 60
}

const unit = ref(bestUnit(props.modelValue ?? 0))
const amount = ref<number | null>(props.modelValue ? props.modelValue / unit.value : 0)

watch(
  () => props.modelValue,
  (v) => {
    if (amount.value !== null && Math.round(amount.value * unit.value) === (v ?? 0)) return
    unit.value = bestUnit(v ?? 0)
    amount.value = v ? v / unit.value : 0
  },
)

function commit(): void {
  const n = amount.value ?? 0
  const seconds = Math.min(props.max, Math.max(props.allowZero ? 0 : 1, Math.round(n * unit.value)))
  emit('update:modelValue', seconds)
}

function setAmount(v: number | null): void {
  amount.value = v
  commit()
}

function setUnit(v: number): void {
  unit.value = v
  commit()
}
</script>

<template>
  <div class="dur">
    <NumberInput :model-value="amount" :min="allowZero ? 0 : 1" :step="1" width="110px" @update:model-value="setAmount" />
    <Select :model-value="unit" :options="UNITS" width="92px" @update:model-value="setUnit" />
  </div>
</template>

<style scoped>
.dur {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>
