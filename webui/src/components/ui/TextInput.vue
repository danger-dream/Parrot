<script setup lang="ts">
import { computed, ref } from 'vue'
import { Eye, EyeOff, X } from '@lucide/vue'

const props = defineProps<{
  modelValue: string | null | undefined
  placeholder?: string
  type?: 'text' | 'password' | 'url' | 'email'
  mono?: boolean
  disabled?: boolean
  invalid?: boolean
  clearable?: boolean
  autofocus?: boolean
  width?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; enter: [] }>()

const reveal = ref(false)
const inputType = computed(() => (props.type === 'password' && !reveal.value ? 'password' : props.type === 'password' ? 'text' : props.type || 'text'))
</script>

<template>
  <div class="ctl wrap" :class="{ mono, disabled, invalid }" :style="{ width }">
    <input
      :value="modelValue ?? ''"
      :type="inputType"
      :placeholder="placeholder"
      :disabled="disabled"
      :autofocus="autofocus"
      spellcheck="false"
      autocomplete="off"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      @keydown.enter="emit('enter')"
    />
    <button v-if="clearable && modelValue" type="button" class="icon" aria-label="清除" @click="emit('update:modelValue', '')"><X /></button>
    <button v-if="type === 'password'" type="button" class="icon" :aria-label="reveal ? '隐藏' : '显示'" @click="reveal = !reveal">
      <EyeOff v-if="reveal" /><Eye v-else />
    </button>
  </div>
</template>

<style scoped>
.wrap {
  display: flex;
  align-items: center;
  gap: 4px;
  padding-right: 4px;
}
input {
  flex: 1;
  min-width: 0;
  height: 100%;
  border: 0;
  outline: 0;
  background: none;
  font: inherit;
  color: inherit;
}
input::placeholder {
  color: var(--text-3);
}
.icon {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 5px;
  color: var(--text-3);
}
.icon:hover {
  color: var(--text);
  background: var(--fill-2);
}
.icon svg {
  width: 13px;
  height: 13px;
}
</style>
