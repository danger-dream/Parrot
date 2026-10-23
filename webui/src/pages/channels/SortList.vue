<script setup lang="ts">
import { ref } from 'vue'
import { ArrowDown, ArrowDownToLine, ArrowUp, ArrowUpToLine, GripVertical } from '@lucide/vue'

// Reorderable list used by the channel and API key pages. Dragging works with mouse and touch
// (pointer events on the handle); the arrow buttons cover keyboard and precise moves.

const props = defineProps<{
  modelValue: string[]
  labels: Record<string, { title: string; sub?: string }>
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string[]]; commit: [value: string[]] }>()

const listEl = ref<HTMLElement | null>(null)
const dragging = ref('')

function move(id: string, to: number, commit = true): void {
  const arr = props.modelValue.filter((x) => x !== id)
  arr.splice(Math.max(0, Math.min(arr.length, to)), 0, id)
  emit('update:modelValue', arr)
  if (commit) emit('commit', arr)
}
function step(id: string, delta: number): void {
  const i = props.modelValue.indexOf(id)
  move(id, i + delta)
}

function onDown(e: PointerEvent, id: string): void {
  if (e.button !== 0) return
  e.preventDefault()
  dragging.value = id
  const before = props.modelValue.join('\n')
  const onMove = (ev: PointerEvent) => {
    const rows = [...(listEl.value?.querySelectorAll<HTMLElement>('[data-sort-id]') || [])].filter((r) => r.dataset.sortId !== id)
    let to = rows.length
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i].getBoundingClientRect()
      if (ev.clientY < r.top + r.height / 2) {
        to = i
        break
      }
    }
    if (to !== props.modelValue.indexOf(id)) move(id, to, false)
  }
  const onUp = () => {
    dragging.value = ''
    if (props.modelValue.join('\n') !== before) emit('commit', [...props.modelValue])
    document.body.style.userSelect = prevSelect
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
  }
  // Window listeners instead of pointer capture: the row re-renders while it moves.
  const prevSelect = document.body.style.userSelect
  document.body.style.userSelect = 'none'
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  window.addEventListener('pointercancel', onUp)
}
</script>

<template>
  <ol ref="listEl" class="sl">
    <li v-for="(id, i) in modelValue" :key="id" :data-sort-id="id" :class="{ dragging: dragging === id }">
      <button type="button" class="handle" aria-label="拖动排序" @pointerdown="onDown($event, id)"><GripVertical /></button>
      <span class="idx mono">{{ i + 1 }}</span>
      <span class="text">
        <span class="title truncate">{{ labels[id]?.title || id }}</span>
        <span v-if="labels[id]?.sub" class="sub truncate">{{ labels[id].sub }}</span>
      </span>
      <slot name="extra" :id="id" />
      <span class="moves">
        <button type="button" title="置顶" :disabled="i === 0" @click="move(id, 0)"><ArrowUpToLine /></button>
        <button type="button" title="上移" :disabled="i === 0" @click="step(id, -1)"><ArrowUp /></button>
        <button type="button" title="下移" :disabled="i === modelValue.length - 1" @click="step(id, 1)"><ArrowDown /></button>
        <button type="button" title="置底" :disabled="i === modelValue.length - 1" @click="move(id, modelValue.length)"><ArrowDownToLine /></button>
      </span>
    </li>
  </ol>
</template>

<style scoped>
.sl {
  container: sortlist / inline-size;
  list-style: none;
  margin: 0;
  padding: 0;
}
li {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 46px;
  padding: 5px 6px 5px 2px;
  border-bottom: 0.5px solid var(--hairline);
  border-radius: 0;
  transition: background 0.15s, box-shadow 0.15s, border-radius 0.15s;
}
li:last-child {
  border-bottom-color: transparent;
}
li:hover {
  background: var(--fill-3);
}
li.dragging {
  position: relative;
  z-index: 1;
  border-radius: var(--radius-md);
  border-bottom-color: transparent;
  background: var(--surface-raised);
  box-shadow: var(--shadow-md);
}
.handle {
  display: grid;
  place-items: center;
  width: 32px;
  height: 36px;
  border-radius: 6px;
  color: var(--text-3);
  cursor: grab;
  touch-action: none;
  flex-shrink: 0;
}
.handle:hover {
  background: var(--fill-2);
  color: var(--text);
}
.dragging .handle {
  cursor: grabbing;
}
.handle svg {
  width: 16px;
  height: 16px;
}
.idx {
  width: 22px;
  text-align: right;
  font-size: 11.5px;
  color: var(--text-3);
  flex-shrink: 0;
}
.text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.title {
  font-weight: 550;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.moves {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}
.moves button {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 6px;
  color: var(--text-2);
}
.moves button:hover:not(:disabled) {
  background: var(--fill-2);
  color: var(--text);
}
.moves button:disabled {
  opacity: 0.3;
}
.moves svg {
  width: 14px;
  height: 14px;
}
@container sortlist (max-width: 480px) {
  .moves button:nth-child(1),
  .moves button:nth-child(4) {
    display: none;
  }
}
</style>
