<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, Search } from '@lucide/vue'

const props = withDefaults(
  defineProps<{
    modelValue: string[]
    options: { value: string; label?: string; hint?: string; disabled?: boolean }[]
    searchable?: boolean
    maxHeight?: string
    emptyText?: string
  }>(),
  { searchable: true, maxHeight: '320px', emptyText: '没有可选项' },
)
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()

const q = ref('')
const selected = computed(() => new Set(props.modelValue))
const visible = computed(() => {
  const s = q.value.trim().toLowerCase()
  if (!s) return props.options
  return props.options.filter((o) => (o.label || o.value).toLowerCase().includes(s) || o.value.toLowerCase().includes(s))
})

function toggle(value: string): void {
  const next = new Set(selected.value)
  if (next.has(value)) next.delete(value)
  else next.add(value)
  emit('update:modelValue', props.options.map((o) => o.value).filter((v) => next.has(v)))
}
function selectVisible(on: boolean): void {
  const next = new Set(selected.value)
  for (const o of visible.value) if (!o.disabled) on ? next.add(o.value) : next.delete(o.value)
  emit('update:modelValue', props.options.map((o) => o.value).filter((v) => next.has(v)))
}
function invertVisible(): void {
  const next = new Set(selected.value)
  for (const o of visible.value) if (!o.disabled) next.has(o.value) ? next.delete(o.value) : next.add(o.value)
  emit('update:modelValue', props.options.map((o) => o.value).filter((v) => next.has(v)))
}
</script>

<template>
  <div class="cl">
    <div class="bar">
      <div v-if="searchable" class="ctl search">
        <Search />
        <input v-model="q" placeholder="搜索" />
      </div>
      <span class="count num">{{ modelValue.length }}/{{ options.length }}</span>
      <button type="button" @click="selectVisible(true)">全选</button>
      <button type="button" @click="selectVisible(false)">清空</button>
      <button type="button" @click="invertVisible">反选</button>
    </div>
    <div class="list" :style="{ maxHeight }">
      <p v-if="!visible.length" class="none">{{ emptyText }}</p>
      <button
        v-for="o in visible"
        :key="o.value"
        type="button"
        class="item"
        :class="{ on: selected.has(o.value) }"
        :disabled="o.disabled"
        @click="toggle(o.value)"
      >
        <span class="box"><Check v-if="selected.has(o.value)" /></span>
        <span class="text">
          <span class="label">{{ o.label || o.value }}</span>
          <span v-if="o.hint" class="hint">{{ o.hint }}</span>
        </span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.cl {
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border-strong);
  overflow: hidden;
}
.bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
  border-bottom: 0.5px solid var(--hairline);
}
.search {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  background: var(--bg-elev);
}
.search svg {
  width: 13px;
  height: 13px;
  color: var(--text-3);
}
.search input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: none;
  font-size: 12.5px;
}
.count {
  font-size: 11px;
  color: var(--text-3);
  margin-left: auto;
}
.bar button {
  font-size: 12px;
  color: var(--accent);
  white-space: nowrap;
}
.list {
  overflow-y: auto;
  padding: 4px;
}
.item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  min-height: 30px;
  padding: 4px 8px;
  border-radius: 6px;
  text-align: left;
}
.item:hover:not(:disabled) {
  background: var(--fill-3);
}
.item:disabled {
  opacity: 0.45;
}
.box {
  width: 16px;
  height: 16px;
  border-radius: 4px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  box-shadow: inset 0 0 0 1px var(--border-strong);
  color: #fff;
}
.on .box {
  background: var(--accent);
  box-shadow: none;
}
.box svg {
  width: 12px;
  height: 12px;
  stroke-width: 3;
}
.text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.label {
  font-size: 12.5px;
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hint {
  font-size: 11px;
  color: var(--text-3);
}
.none {
  padding: 16px;
  text-align: center;
  font-size: 12px;
  color: var(--text-3);
}
</style>
