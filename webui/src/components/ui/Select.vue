<script setup lang="ts" generic="T extends string | number | null">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Check, ChevronDown, Search } from '@lucide/vue'

const props = defineProps<{
  modelValue: T
  options: { value: T; label: string; disabled?: boolean; hint?: string }[]
  placeholder?: string
  disabled?: boolean
  width?: string
  searchable?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: T] }>()

const open = ref(false)
const query = ref('')
const active = ref(-1)
const trigger = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)
const searchInput = ref<HTMLInputElement | null>(null)
const pos = ref({ left: 0, top: 0, width: 0, up: false })

const current = computed(() => props.options.find((o) => o.value === props.modelValue))
const withSearch = computed(() => props.searchable ?? props.options.length > 10)
const visible = computed(() => {
  const q = query.value.trim().toLowerCase()
  return q ? props.options.filter((o) => o.label.toLowerCase().includes(q) || String(o.value ?? '').toLowerCase().includes(q)) : props.options
})

function place(): void {
  const el = trigger.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const below = window.innerHeight - r.bottom
  const up = below < 280 && r.top > below
  pos.value = { left: r.left, top: up ? r.top - 6 : r.bottom + 6, width: Math.max(r.width, 180), up }
}

function show(): void {
  if (props.disabled) return
  place()
  query.value = ''
  active.value = Math.max(0, visible.value.findIndex((o) => o.value === props.modelValue))
  open.value = true
  nextTick(() => {
    if (withSearch.value) searchInput.value?.focus()
    panel.value?.querySelector('.opt.on')?.scrollIntoView({ block: 'nearest' })
  })
}

function close(): void {
  open.value = false
}

function pick(o: { value: T; disabled?: boolean }): void {
  if (o.disabled) return
  emit('update:modelValue', o.value)
  close()
  trigger.value?.focus()
}

function onKey(e: KeyboardEvent): void {
  if (!open.value) {
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
      e.preventDefault()
      show()
    }
    return
  }
  if (e.key === 'Escape') {
    e.preventDefault()
    close()
    trigger.value?.focus()
  } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault()
    const n = visible.value.length
    if (!n) return
    active.value = (active.value + (e.key === 'ArrowDown' ? 1 : -1) + n) % n
    nextTick(() => panel.value?.querySelectorAll('.opt')[active.value]?.scrollIntoView({ block: 'nearest' }))
  } else if (e.key === 'Enter') {
    e.preventDefault()
    const o = visible.value[active.value]
    if (o) pick(o)
  }
}

function onDoc(e: MouseEvent): void {
  const t = e.target as Node
  if (trigger.value?.contains(t) || panel.value?.contains(t)) return
  close()
}

watch(open, (v) => {
  if (v) {
    document.addEventListener('mousedown', onDoc, true)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', place, true)
  } else {
    document.removeEventListener('mousedown', onDoc, true)
    window.removeEventListener('resize', close)
    window.removeEventListener('scroll', place, true)
  }
})
watch(query, () => (active.value = 0))
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDoc, true)
  window.removeEventListener('resize', close)
  window.removeEventListener('scroll', place, true)
})
</script>

<template>
  <button
    ref="trigger"
    type="button"
    class="ctl trig"
    :class="{ disabled, open }"
    :style="{ width }"
    :disabled="disabled"
    aria-haspopup="listbox"
    :aria-expanded="open"
    @click="open ? close() : show()"
    @keydown="onKey"
  >
    <span class="val" :class="{ ph: !current }">{{ current?.label ?? placeholder ?? '请选择' }}</span>
    <ChevronDown class="chev" />
  </button>
  <Teleport to="body">
    <div
      v-if="open"
      ref="panel"
      class="sel-pop"
      :class="{ up: pos.up }"
      :style="{ left: `${pos.left}px`, top: `${pos.top}px`, minWidth: `${pos.width}px` }"
      role="listbox"
      @keydown="onKey"
    >
      <label v-if="withSearch" class="sel-search">
        <Search />
        <input ref="searchInput" v-model="query" placeholder="搜索…" @keydown="onKey" />
      </label>
      <div class="sel-list">
        <button
          v-for="(o, i) in visible"
          :key="String(o.value)"
          type="button"
          class="opt"
          :class="{ on: o.value === modelValue, act: i === active }"
          :disabled="o.disabled"
          role="option"
          :aria-selected="o.value === modelValue"
          @mouseenter="active = i"
          @click="pick(o)"
        >
          <span class="ol">{{ o.label }}</span>
          <span v-if="o.hint" class="oh">{{ o.hint }}</span>
          <Check v-if="o.value === modelValue" class="ok" />
        </button>
        <p v-if="!visible.length" class="none">没有匹配的选项</p>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.trig {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px 0 12px;
  text-align: left;
  cursor: pointer;
}
.val {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.val.ph {
  color: var(--text-3);
}
.chev {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  color: var(--text-3);
  transition: transform 0.15s;
}
.trig.open .chev {
  transform: rotate(180deg);
}
</style>

<style>
.sel-pop {
  position: fixed;
  z-index: 3000;
  max-width: min(420px, calc(100vw - 16px));
  display: flex;
  flex-direction: column;
  padding: 6px;
  border-radius: var(--radius-lg);
  background: var(--popover);
  border: 0.5px solid var(--border);
  box-shadow: var(--shadow-lg);
  color: var(--text);
  font-size: 13px;
}
.sel-pop.up {
  transform: translateY(-100%);
}
.sel-search {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  margin-bottom: 4px;
  padding: 0 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 1px var(--border);
  color: var(--text-3);
}
.sel-search svg {
  width: 14px;
  height: 14px;
}
.sel-search input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: none;
  color: var(--text);
}
.sel-list {
  max-height: 300px;
  overflow-y: auto;
}
.sel-pop .opt {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  text-align: left;
  color: var(--text);
}
.sel-pop .opt.act {
  background: var(--accent-soft);
}
.sel-pop .opt.on {
  color: var(--accent);
  font-weight: 600;
}
.sel-pop .opt:disabled {
  opacity: 0.45;
}
.sel-pop .ol {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sel-pop .oh {
  font-size: 11.5px;
  color: var(--text-3);
}
.sel-pop .ok {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
.sel-pop .none {
  padding: 10px;
  color: var(--text-3);
  text-align: center;
}
</style>
