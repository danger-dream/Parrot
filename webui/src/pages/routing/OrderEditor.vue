<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, ArrowDownToLine, ArrowUp, ArrowUpToLine, GripVertical, RotateCcw, Save } from '@lucide/vue'
import { openChannel } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { channelProvider } from '@/utils/providers'
import { channelLabel, channelState } from '@/pages/models/shared'
import { useCardDrag } from '@/pages/channels/useCardDrag'

// Drag ordering (mouse and touch, from the grip) of routing channels with explicit 保存 / 还原.
// The move buttons stay for precise single steps.

const props = withDefaults(
  defineProps<{ order: string[]; saving?: boolean; emptyText?: string; compact?: boolean; hideFooter?: boolean }>(),
  { emptyText: '当前没有可排序的账户或渠道。' },
)
const emit = defineEmits<{ save: [order: string[]]; change: [order: string[]] }>()

const draft = ref<string[]>([...props.order])
watch(
  () => props.order,
  (o) => (draft.value = [...o]),
)
watch(draft, (d) => emit('change', d), { deep: true })

const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(props.order))
const moved = computed(() => {
  const orig = new Map(props.order.map((k, i) => [k, i]))
  return new Set(draft.value.filter((k, i) => orig.get(k) !== i))
})

function move(from: number, to: number): void {
  if (to < 0 || to >= draft.value.length || from === to) return
  const next = [...draft.value]
  const [item] = next.splice(from, 1)
  next.splice(to, 0, item)
  draft.value = next
}

// ---- drag: reorders the draft live; saving stays explicit ----
const drag = useCardDrag(draft, () => undefined)

function reset(): void {
  draft.value = [...props.order]
}
defineExpose({ reset, dirty, draft })
</script>

<template>
  <div class="oe" :class="{ compact }">
    <p v-if="!draft.length" class="empty faint">{{ emptyText }}</p>
    <ol v-else class="list" data-drag-grid>
      <li v-for="(key, i) in draft" :key="key" class="item" :class="{ dragging: drag.dragging.value === key, moved: moved.has(key) }" :data-drag-id="key">
        <button type="button" class="grip" aria-label="拖动排序" title="按住拖动排序" @pointerdown="drag.start($event, key)"><GripVertical /></button>
        <span class="rank mono">{{ i + 1 }}</span>
        <ProviderIcon :provider="channelProvider(key)" :size="20" />
        <Badge v-if="key.startsWith('api:')" class="type" tone="teal">API</Badge>
        <button class="name truncate" :title="key" @click="openChannel(key)">{{ channelLabel(key) }}</button>
        <Badge :tone="channelState(key).tone" dot class="st" :title="channelState(key).title">{{ channelState(key).label }}</Badge>
        <span class="mv">
          <button title="置顶" :disabled="i === 0" @click="move(i, 0)"><ArrowUpToLine /></button>
          <button title="上移" :disabled="i === 0" @click="move(i, i - 1)"><ArrowUp /></button>
          <button title="下移" :disabled="i === draft.length - 1" @click="move(i, i + 1)"><ArrowDown /></button>
          <button title="置底" :disabled="i === draft.length - 1" @click="move(i, draft.length - 1)"><ArrowDownToLine /></button>
        </span>
      </li>
    </ol>
    <Transition name="bar">
      <footer v-if="dirty && !hideFooter" class="foot">
        <span class="note">顺序已调整，保存后生效</span>
        <Btn variant="ghost" size="sm" :disabled="saving" @click="reset"><RotateCcw /> 还原</Btn>
        <Btn variant="primary" size="sm" :loading="saving" @click="emit('save', [...draft])"><Save /> 保存顺序</Btn>
      </footer>
    </Transition>
  </div>
</template>

<style scoped>
.oe {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.empty {
  font-size: 12.5px;
  padding: 8px 0;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 40px;
  padding: 4px 6px 4px 4px;
  border-bottom: 0.5px solid var(--hairline);
  border-radius: 0;
}
.item:last-child {
  border-bottom-color: transparent;
}
.item:hover {
  background: var(--fill-3);
}
.item.dragging {
  opacity: 0.4;
}
.item.moved .rank {
  color: var(--accent);
  background: var(--accent-soft);
}
.grip {
  display: grid;
  place-items: center;
  width: 22px;
  height: 28px;
  flex-shrink: 0;
  color: var(--text-3);
  cursor: grab;
  /* The browser must not take a touch on the grip as a scroll. */
  touch-action: none;
}
.grip:hover {
  color: var(--accent);
}
.grip svg {
  width: 14px;
  height: 14px;
}
.rank {
  min-width: 22px;
  height: 20px;
  padding: 0 4px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  font-size: 11px;
  color: var(--text-2);
  background: var(--fill-2);
  flex-shrink: 0;
}
.type {
  flex-shrink: 0;
}
.name {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  text-align: left;
}
.name:hover {
  color: var(--accent);
  text-decoration: underline;
}
.st {
  flex-shrink: 0;
}
.mv {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}
.mv button {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  color: var(--text-2);
}
.mv button:hover:not(:disabled) {
  background: var(--fill-2);
  color: var(--accent);
}
.mv button:disabled {
  opacity: 0.3;
}
.mv svg {
  width: 14px;
  height: 14px;
}
.foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.note {
  flex: 1;
  font-size: 12px;
  color: var(--text-2);
}
.bar-enter-active,
.bar-leave-active {
  transition: opacity 0.18s;
}
.bar-enter-from,
.bar-leave-to {
  opacity: 0;
}
@container (max-width: 520px) {
  .mv button:nth-child(1),
  .mv button:nth-child(4) {
    display: none;
  }
  .type {
    display: none;
  }
}
@media (max-width: 767px) {
  .type,
  .mv button:nth-child(1),
  .mv button:nth-child(4) {
    display: none;
  }
  .mv button {
    width: 32px;
    height: 32px;
  }
}
</style>
