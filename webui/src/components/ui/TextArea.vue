<script setup lang="ts">
defineProps<{ modelValue: string | null | undefined; rows?: number; placeholder?: string; mono?: boolean; disabled?: boolean; invalid?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<template>
  <textarea
    class="ctl area"
    :class="{ mono, invalid }"
    :value="modelValue ?? ''"
    :rows="rows || 4"
    :placeholder="placeholder"
    :disabled="disabled"
    spellcheck="false"
    @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
  />
</template>

<style scoped>
.area {
  height: auto;
  padding: 8px 10px;
  line-height: 1.5;
  resize: vertical;
}
</style>
