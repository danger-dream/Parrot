<script setup lang="ts">
defineProps<{ modelValue: boolean | null | undefined; disabled?: boolean; loading?: boolean; label?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
</script>

<template>
  <button
    type="button"
    role="switch"
    class="switch"
    :class="{ on: modelValue, loading }"
    :aria-checked="Boolean(modelValue)"
    :aria-label="label"
    :disabled="disabled || loading"
    @click="emit('update:modelValue', !modelValue)"
  >
    <span class="knob" />
  </button>
</template>

<style scoped>
.switch {
  position: relative;
  width: 36px;
  height: 21px;
  flex-shrink: 0;
  border-radius: 999px;
  background: var(--fill);
  box-shadow: inset 0 0 0 0.5px var(--border);
  transition: background 0.2s;
}
.switch.on {
  background: var(--green);
}
.switch:disabled {
  opacity: 0.5;
}
.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  transition: transform 0.25s var(--ease-spring);
}
.on .knob {
  transform: translateX(15px);
}
.loading .knob {
  animation: pulse 0.9s ease-in-out infinite;
}
@keyframes pulse {
  50% {
    opacity: 0.6;
  }
}
</style>
