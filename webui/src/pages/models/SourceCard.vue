<script setup lang="ts">
import { computed } from 'vue'
import { GripVertical, PencilLine, TriangleAlert } from '@lucide/vue'
import { openAccount, openChannel } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import Switch from '@/components/ui/Switch.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import {
  META_FIELDS,
  effortLabel,
  price,
  rawMetaValue,
  metaValue,
  sourceName,
  sourceProvider,
  sourceRate,
  tokens,
  unavailableReason,
  type MetaField,
  type MetadataValues,
  type ModelData,
  type SourceGroup,
} from './shared'

const props = defineProps<{
  group: SourceGroup
  model: ModelData
  /** Model-common metadata; only values that differ from it are listed per source. */
  common?: MetadataValues | null
  editable: boolean
  busy: boolean
  editing: boolean
  /** Priority scheduling: the card can be dragged to reorder the scheduled sources. */
  draggable?: boolean
  dragging?: boolean
}>()
const emit = defineEmits<{ toggle: [enabled: boolean]; edit: []; dragstart: [e: PointerEvent] }>()

function onPointerDown(e: PointerEvent): void {
  if (!props.draggable) return
  const t = e.target as HTMLElement
  // Touch drags only from the grip so the drawer still scrolls; controls keep their own clicks.
  if (e.pointerType === 'touch' ? !t.closest('.grip') : t.closest('button, a, input, label, [role="switch"]')) return
  emit('dragstart', e)
}

const first = computed(() => props.group.items[0])
const name = computed(() => sourceName(first.value))
const provider = computed(() => sourceProvider(first.value))
const wires = computed(() => [...new Set(props.group.items.map((s) => s.outboundModel))])
const rate = computed(() => [...new Set(props.group.items.map(sourceRate).filter(Boolean))].join(' / '))
const reason = computed(() => {
  if (props.group.live) return ''
  const r = unavailableReason(props.model, first.value)
  return props.group.containerEnabled ? r : `${r}，请到${props.group.type === 'oauth' ? '账户' : '渠道'}管理里处理`
})

function open(): void {
  if (props.group.type === 'oauth') openAccount(props.group.id, name.value)
  else openChannel(props.group.id)
}

// ---- metadata that differs from the common value ----
function short(f: MetaField, v: unknown): string {
  if (v === null || v === undefined) return '未提供'
  if (f.kind === 'tokens' && typeof v === 'number') return tokens(v)
  if (f.kind === 'bool') return v ? '支持' : '不支持'
  if (f.kind === 'price' && typeof v === 'number') return price(v, true)
  if (Array.isArray(v)) return v.length ? v.map((x) => (f.key === 'reasoningEfforts' ? effortLabel(String(x)) : String(x))).join('/') : '无'
  return String(v)
}
const diffs = computed(() => {
  const raw = first.value.effectiveMetadata as Record<string, unknown>
  const out: { key: string; label: string; value: string }[] = []
  for (const f of META_FIELDS) {
    const own = rawMetaValue(raw, f.key)
    if (own === undefined || own === null || own === '' || (Array.isArray(own) && !own.length)) continue
    const base = props.common ? metaValue(props.common, f.key) : undefined
    if (JSON.stringify(own) === JSON.stringify(base)) continue
    out.push({ key: f.key, label: f.label, value: short(f, own) })
  }
  return out
})
</script>

<template>
  <article class="src" :class="{ off: !group.live, movable: draggable, dragging }" @pointerdown="onPointerDown">
    <header class="top">
      <span v-if="draggable" class="grip" title="拖动调整顺序"><GripVertical /></span>
      <span v-if="group.rank" class="rank mono" title="调度位次">{{ group.rank }}</span>
      <ProviderIcon :provider="provider" :size="22" />
      <button class="name truncate" :title="group.id" @click="open">{{ name }}</button>
      <Badge v-if="model.globalEnabled === false && group.enabledOverride && group.sourceEnabled" tone="blue">单独启用</Badge>
      <Switch
        v-if="editable && group.containerEnabled"
        :model-value="group.sourceEnabled"
        :loading="busy"
        :label="`启用来源 ${name}`"
        @update:model-value="(v: boolean) => emit('toggle', v)"
      />
    </header>

    <p v-if="reason" class="why"><TriangleAlert /><span>{{ reason }}</span></p>
    <p v-else-if="model.globalEnabled === false && group.enabledOverride" class="line faint">本来源单独启用（覆盖全局停用）</p>

    <p class="line">
      <span class="k">上游名</span>
      <span class="wires"><span v-for="w in wires" :key="w" class="mono wire">{{ w }}</span></span>
    </p>
    <p v-if="rate" class="line rate">{{ rate }}</p>

    <p v-if="diffs.length" class="diffs">
      <span class="k">与上方不同</span>
      <span v-for="d in diffs" :key="d.key" class="chip">{{ d.label }} <b class="mono">{{ d.value }}</b></span>
    </p>

    <div class="acts">
      <button class="mini" :class="{ on: editing }" @click="emit('edit')"><PencilLine /> {{ editing ? '收起' : '编辑元数据' }}</button>
    </div>
  </article>
</template>

<style scoped>
.src {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.src.movable {
  cursor: grab;
}
.src.dragging {
  cursor: grabbing;
  background: var(--surface-raised);
  box-shadow: var(--shadow-md);
  position: relative;
  z-index: 1;
}
.grip {
  display: grid;
  place-items: center;
  width: 18px;
  margin-left: -6px;
  color: var(--text-3);
  touch-action: none;
}
.grip svg {
  width: 14px;
  height: 14px;
}
.src.off {
  background: transparent;
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.top {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 26px;
}
.off .top :deep(.pi) {
  filter: grayscale(1);
  opacity: 0.55;
}
.rank {
  min-width: 20px;
  height: 20px;
  padding: 0 4px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
}
.name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  text-align: left;
}
.name:hover {
  color: var(--accent);
  text-decoration: underline;
}
.off .name {
  color: var(--text-2);
}
.why {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  font-size: 12px;
  color: var(--orange);
}
.why svg {
  width: 13px;
  height: 13px;
  margin-top: 2px;
  flex-shrink: 0;
}
.line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  min-width: 0;
}
.k {
  flex-shrink: 0;
  font-size: 11.5px;
  color: var(--text-3);
}
.wires {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}
.wire {
  font-size: 11.5px;
  word-break: break-all;
}
.wire + .wire::before {
  content: '/ ';
  color: var(--text-3);
}
.rate {
  color: var(--purple);
}
.diffs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  font-size: 11.5px;
}
.chip {
  padding: 1px 6px;
  border-radius: 5px;
  color: var(--text-2);
  background: var(--fill-2);
}
.chip b {
  font-weight: 600;
  color: var(--text);
}
.acts {
  display: flex;
}
.mini {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 6px;
  margin-left: -6px;
  border-radius: 6px;
  font-size: 11.5px;
  color: var(--text-2);
}
.mini:hover,
.mini.on {
  background: var(--fill-2);
  color: var(--accent);
}
.mini svg {
  width: 12px;
  height: 12px;
}
</style>
