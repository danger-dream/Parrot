<script setup lang="ts">
import { Brackets, Plus, Trash2 } from '@lucide/vue'
import Segmented from '@/components/ui/Segmented.vue'
import ConditionRow from './ConditionRow.vue'
import { newCond, newGroup, type Group } from './model'

const props = withDefaults(defineProps<{
  group: Group
  depth?: number
  providers: { id: string; channelCount: number }[]
  channelOptions: { value: string; label: string; hint?: string }[]
  channelLabel: (id: string) => string
}>(), { depth: 0 })
const emit = defineEmits<{ remove: [] }>()

const MODES = [
  { value: 'all' as const, label: '全部满足' },
  { value: 'any' as const, label: '任一满足' },
]
const MAX_DEPTH = 3

// Stable row keys, so removing a row never hands its half-typed input to the next one.
const ids = new WeakMap<object, number>()
let nextId = 0
function keyOf(item: object): number {
  let id = ids.get(item)
  if (id === undefined) {
    id = ++nextId
    ids.set(item, id)
  }
  return id
}

function addCond(): void {
  props.group.items.push(newCond())
}

function addGroup(): void {
  props.group.items.push(newGroup(props.group.mode === 'all' ? 'any' : 'all', [newCond('message')]))
}

function removeAt(i: number): void {
  props.group.items.splice(i, 1)
  if (!props.group.items.length && props.depth > 0) emit('remove')
}
</script>

<template>
  <div class="grp" :class="[`d${depth}`, group.mode]">
    <div class="g-head">
      <span class="g-lead">{{ depth === 0 ? '满足以下' : '这一组' }}</span>
      <Segmented v-model="group.mode" :options="MODES" size="sm" />
      <span class="g-lead">条件</span>
      <button v-if="depth > 0" type="button" class="g-del" title="删除这一组" @click="emit('remove')"><Trash2 /></button>
    </div>
    <div class="items">
      <template v-for="(item, i) in group.items" :key="keyOf(item)">
        <div v-if="i > 0" class="joiner"><span>{{ group.mode === 'all' ? '且' : '或' }}</span></div>
        <ConditionGroup
          v-if="item.kind === 'group'"
          :group="item"
          :depth="depth + 1"
          :providers="providers"
          :channel-options="channelOptions"
          :channel-label="channelLabel"
          @remove="removeAt(i)"
        />
        <ConditionRow
          v-else
          :cond="item"
          :providers="providers"
          :channel-options="channelOptions"
          :channel-label="channelLabel"
          :removable="depth > 0 || group.items.length > 1"
          @remove="removeAt(i)"
        />
      </template>
    </div>
    <div class="g-foot">
      <button type="button" class="add" @click="addCond"><Plus /> 添加条件</button>
      <button v-if="depth < MAX_DEPTH - 1" type="button" class="add" @click="addGroup">
        <Brackets /> 添加{{ group.mode === 'all' ? '「或」' : '「且」' }}条件组
      </button>
    </div>
  </div>
</template>

<style scoped>
.grp {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.grp.d1,
.grp.d2 {
  padding: 10px 12px 10px 14px;
  border-radius: 11px;
  border: 0.5px dashed var(--border-strong, var(--border));
  background: color-mix(in srgb, var(--accent, var(--blue)) 4%, transparent);
}
.grp.d2 {
  background: color-mix(in srgb, var(--purple) 5%, transparent);
}
.g-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-2);
}
.g-lead {
  white-space: nowrap;
}
.g-del {
  margin-left: auto;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 7px;
  color: var(--text-3);
  cursor: pointer;
}
.g-del:hover {
  background: var(--fill-2);
  color: var(--red);
}
.g-del svg {
  width: 13px;
  height: 13px;
}
.items {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.joiner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 10px;
  height: 14px;
}
.joiner span {
  font-size: 11px;
  font-weight: 650;
  color: var(--text-3);
  letter-spacing: 0.04em;
}
.grp.any > .items > .joiner span {
  color: var(--purple);
}
.g-foot {
  display: flex;
  gap: 12px;
}
.add {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--accent, var(--blue));
  cursor: pointer;
}
.add svg {
  width: 13px;
  height: 13px;
}
</style>
