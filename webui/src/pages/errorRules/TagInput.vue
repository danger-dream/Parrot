<script setup lang="ts">
import { ref } from 'vue'
import { X } from '@lucide/vue'

const props = withDefaults(defineProps<{ modelValue: string[]; placeholder?: string; mono?: boolean; single?: boolean }>(), {
  placeholder: '',
  mono: false,
  single: false,
})
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()

const draft = ref('')

function commit(): void {
  const parts = draft.value
    .split(props.single ? /\n/ : /[\n,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!parts.length) return
  const next = props.single ? [parts[parts.length - 1]] : [...props.modelValue]
  if (!props.single) for (const p of parts) if (!next.includes(p)) next.push(p)
  emit('update:modelValue', next)
  draft.value = ''
}

function remove(i: number): void {
  const next = [...props.modelValue]
  next.splice(i, 1)
  emit('update:modelValue', next)
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Enter' || (!props.single && (e.key === ',' || e.key === '，'))) {
    e.preventDefault()
    commit()
  } else if (e.key === 'Backspace' && !draft.value && props.modelValue.length) {
    remove(props.modelValue.length - 1)
  }
}
</script>

<template>
  <label class="tags" :class="{ mono }">
    <span v-for="(v, i) in modelValue" :key="v" class="tag">
      <span class="tag-text">{{ v }}</span>
      <button type="button" :aria-label="`移除 ${v}`" @click.prevent="remove(i)"><X /></button>
    </span>
    <input
      v-model="draft"
      :placeholder="modelValue.length ? (single ? '' : '继续添加…') : placeholder"
      spellcheck="false"
      @keydown="onKey"
      @blur="commit"
    />
  </label>
</template>

<style scoped>
.tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  min-height: 32px;
  padding: 3px 6px;
  border-radius: var(--radius-sm, 8px);
  border: 0.5px solid var(--border);
  background: var(--surface-solid, var(--surface));
  cursor: text;
  min-width: 0;
}
.tags:focus-within {
  border-color: var(--accent, var(--blue));
  box-shadow: 0 0 0 3px var(--accent-soft, transparent);
}
.tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  max-width: 100%;
  height: 22px;
  padding: 0 4px 0 7px;
  border-radius: 6px;
  background: var(--fill-2);
  font-size: 12px;
}
.mono .tag,
.mono input {
  font-family: var(--font-mono);
  font-size: 11.5px;
}
.tag-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tag button {
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  border-radius: 4px;
  color: var(--text-3);
  cursor: pointer;
}
.tag button:hover {
  background: var(--fill);
  color: var(--text);
}
.tag svg {
  width: 11px;
  height: 11px;
}
input {
  flex: 1;
  min-width: 80px;
  height: 24px;
  border: 0;
  outline: 0;
  background: transparent;
  font-size: 12.5px;
  color: var(--text);
}
</style>
