<script setup lang="ts">
import { computed } from 'vue'
import { EyeOff, Minimize2 } from '@lucide/vue'
import { compact, integer, pct, tps } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { displayStatus, groupSources, priceSummary, rateSummary, sourceProvider, sourceRate, tokens, type MetadataValues, type ModelData, type ModelMetadata, type ModelSource, type ModelUsage } from './shared'

const props = defineProps<{
  model: ModelData
  meta?: ModelMetadata
  /** Account/channel-scoped records of this model. */
  sourceMeta?: ModelMetadata[]
  /** When the list is filtered to one source, its state is shown instead of the aggregate. */
  source?: ModelSource
  /** listModels `usage`; with a source filter the page passes that source's projection. */
  usage?: ModelUsage | null
  usageLoading?: boolean
  compression?: boolean
  checked: boolean
  selectable: boolean
  selected: boolean
}>()
const emit = defineEmits<{ toggle: []; open: [] }>()

const groups = computed(() => groupSources(props.model))
const live = computed(() => groups.value.filter((g) => g.live).length)
const total = computed(() => groups.value.length)
/** One icon per provider that can serve the model now; unavailable sources only count in live/total. */
const icons = computed(() => {
  const seen = new Set<string>()
  for (const g of groups.value) if (g.live) seen.add(sourceProvider(g.items[0]))
  return [...seen]
})
/** The metadata record this model is bound to, e.g. "anthropic/claude-fable-5". */
const bound = computed(() => {
  const m = props.meta
  if (!m) return ''
  return m.target || (m.providerId && m.catalogModelId ? `${m.providerId}/${m.catalogModelId}` : '')
})
const status = computed(() => displayStatus(props.model, props.source))
/** TG's head block: values shared by the scheduled sources; the common record fills gaps. */
const shared = computed(() => (props.model.sharedMetadata || {}) as Partial<MetadataValues>)
const eff = computed<Partial<MetadataValues> | undefined>(() => {
  const base = props.meta?.effective
  return { ...(base || {}), ...shared.value, cost: shared.value.cost ?? base?.cost } as Partial<MetadataValues>
})
const priceText = computed(() => priceSummary(eff.value as MetadataValues))

/** Values TG shows per model that differ by source: only noted, the common value stays primary. */
function differs(key: 'maxOutputTokens' | 'compactTriggerTokens' | 'contextWindow'): number[] {
  const base = eff.value?.[key]
  return [...new Set((props.sourceMeta || []).map((m) => m.effective[key]).filter((v): v is number => typeof v === 'number' && v !== base))]
}
const outDiff = computed(() => differs('maxOutputTokens'))
const compactDiff = computed(() => differs('compactTriggerTokens'))
const ctxDiff = computed(() => differs('contextWindow'))
const rates = computed(() => (props.source ? [sourceRate(props.source)].filter(Boolean) : rateSummary(props.model.creditRates)))

const u = computed(() => props.usage)
const prompt = computed(() => u.value?.promptTokens ?? 0)
const cacheText = computed(() => {
  const r = u.value?.cacheReadRatio
  if (r == null) return ''
  return `缓存占比 ${pct(r)}`
})
const usageTitle = computed(() => {
  const m = u.value
  if (!m) return ''
  const parts = [
    `累计请求 ${integer(m.requestCount)} 次，失败 ${integer(m.errorCount)} 次（当前保留日志范围）`,
    `读入 ${compact(prompt.value)}（缓存占比 ${pct(m.cacheReadRatio)}） · 输出 ${compact(m.outputTokens)}`,
  ]
  if (m.averageTokensPerSecond != null)
    parts.push(`输出速度 平均 ${tps(m.averageTokensPerSecond)} · 峰值 ${tps(m.maximumTokensPerSecond)} · 最低 ${tps(m.minimumTokensPerSecond)}`)
  return parts.join('\n')
})
</script>

<template>
  <div
    class="row"
    :class="{ selected, checked, off: status.tone === 'neutral' }"
    role="button"
    tabindex="0"
    @click="emit('open')"
    @keydown.enter.prevent="emit('open')"
  >
    <span class="c-check" @click.stop>
      <input type="checkbox" :checked="checked" :disabled="!selectable" :aria-label="`选择 ${model.modelId}`" @change="emit('toggle')" />
    </span>
    <span class="c-model">
      <span class="name-line">
        <span class="id mono truncate">{{ model.modelId }}</span>
        <span v-if="compression" class="tag comp" title="当前压缩模型：用于内部上下文压缩"><Minimize2 />压缩</span>
        <EyeOff v-if="model.visible === false" class="hid" aria-label="已对下游隐藏" />
      </span>
      <span v-if="bound || model.aliases?.length || rates.length" class="sub-line">
        <span v-if="bound" class="bound mono truncate" title="绑定的元数据">{{ bound }}</span>
        <span v-if="model.aliases?.length" class="alias truncate">别名 <span class="mono">{{ model.aliases.join('、') }}</span></span>
        <span v-for="r in rates" :key="r" class="rate">{{ r }}</span>
      </span>
    </span>
    <span class="c-src" :title="`${live} 个可用 / 共 ${total} 个账户或渠道`">
      <span class="icons">
        <ProviderIcon v-for="p in icons.slice(0, 3)" :key="p" :provider="p" :size="18" />
        <span v-if="!icons.length" class="none">无可用</span>
      </span>
      <span class="mono n" :class="{ bad: total && !live }">{{ live }}/{{ total }}</span>
    </span>
    <span class="c-ctx mono" :title="[ctxDiff.length ? `上下文部分来源不同：${ctxDiff.map(tokens).join('、')}` : '', compactDiff.length ? `压缩阈值部分来源不同：${compactDiff.map(tokens).join('、')}` : ''].filter(Boolean).join('\n') || '上下文窗口 / 压缩阈值（模型通用值）'">
      <span>{{ eff?.contextWindow ? tokens(eff.contextWindow) : '—' }}<sup v-if="ctxDiff.length" class="diff">*</sup></span>
      <span class="faint cmp">{{ eff?.compactTriggerTokens ? `压缩 ${tokens(eff.compactTriggerTokens)}` : '不压缩' }}<sup v-if="compactDiff.length" class="diff">*</sup></span>
    </span>
    <span class="c-out mono" :title="outDiff.length ? `部分来源不同：${outDiff.map(tokens).join('、')}` : '最大输出（模型通用值）'">
      {{ eff?.maxOutputTokens ? tokens(eff.maxOutputTokens) : '—' }}<sup v-if="outDiff.length" class="diff">*</sup>
    </span>
    <span class="c-price mono">{{ priceText || '—' }}</span>
    <span class="c-use" :title="usageTitle">
      <template v-if="u && u.requestCount">
        <span class="mono">↑{{ compact(prompt) }} ↓{{ compact(u.outputTokens) }}</span>
        <span class="mono faint use2">{{ [cacheText, u.averageTokensPerSecond != null ? tps(u.averageTokensPerSecond) : ''].filter(Boolean).join(' · ') || '—' }}</span>
      </template>
      <span v-else class="faint">{{ usageLoading ? '统计中' : '—' }}</span>
    </span>
    <span class="c-state"><Badge :tone="status.tone" dot>{{ status.label }}</Badge></span>
    <span class="c-meta faint">
      <span class="mono">{{ live }}/{{ total }} 来源</span>
      <span v-if="eff?.maxOutputTokens" class="mono">输出 {{ tokens(eff.maxOutputTokens) }}</span>
      <span v-if="eff?.contextWindow" class="mono">上下文 {{ tokens(eff.contextWindow) }}</span>
      <span v-if="eff?.compactTriggerTokens" class="mono">压缩 {{ tokens(eff.compactTriggerTokens) }}</span>
      <span v-if="priceText" class="mono">{{ priceText }}</span>
      <span v-if="u && u.requestCount" class="mono">↑{{ compact(prompt) }} ↓{{ compact(u.outputTokens) }}</span>
      <span v-else-if="usageLoading" class="mono">统计中</span>
    </span>
  </div>
</template>

<style scoped>
.row {
  display: grid;
  grid-template-columns: var(--cols);
  grid-template-areas: var(--areas);
  align-items: center;
  column-gap: 12px;
  min-height: 46px;
  padding: 5px 14px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
  cursor: pointer;
  transition: background 0.15s;
  outline: 0;
}
.row:hover,
.row:focus-visible {
  background: var(--fill-3);
}
.row.checked {
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}
.row.selected {
  background: var(--accent-soft);
}
.c-check {
  grid-area: check;
  display: flex;
  align-items: center;
  align-self: stretch;
}
.c-check input {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  cursor: pointer;
}
.c-check input:disabled {
  cursor: not-allowed;
  opacity: 0.35;
}
.c-model {
  grid-area: model;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.name-line,
.sub-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.sub-line {
  flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--text-3);
  row-gap: 0;
}
.id {
  font-weight: 600;
  min-width: 0;
}
.off .id {
  color: var(--text-2);
}
.tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  height: 17px;
  padding: 0 5px;
  border-radius: 5px;
  font-size: 10.5px;
  font-weight: 600;
  flex-shrink: 0;
}
.tag svg {
  width: 10px;
  height: 10px;
}
.comp {
  color: var(--teal);
  background: color-mix(in srgb, var(--teal) 15%, transparent);
}
.hid {
  width: 12px;
  height: 12px;
  color: var(--orange);
  flex-shrink: 0;
}
.alias {
  min-width: 0;
}
.alias .mono {
  color: var(--text-2);
}
.rate {
  color: var(--purple);
  white-space: nowrap;
}
.c-src {
  grid-area: src;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.icons {
  display: flex;
  align-items: center;
  gap: 3px;
}
.none {
  font-size: 11px;
  color: var(--text-3);
}
.n {
  font-size: 11.5px;
  color: var(--text-2);
}
.n.bad {
  color: var(--red);
}
.c-out {
  grid-area: out;
  text-align: right;
  font-size: 12px;
}
.c-ctx {
  grid-area: ctx;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-size: 12px;
  white-space: nowrap;
}
.c-ctx .cmp {
  font-size: 10.5px;
}
.bound {
  max-width: 260px;
  color: var(--text-3);
}
.diff {
  margin-left: 1px;
  color: var(--orange);
  font-size: 10px;
}
.c-price {
  grid-area: price;
  text-align: right;
  font-size: 12px;
  white-space: nowrap;
}
.c-use {
  grid-area: use;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-size: 12px;
  min-width: 0;
  white-space: nowrap;
}
.use2 {
  font-size: 10.5px;
}
.warn {
  color: var(--red);
}
.c-state {
  grid-area: state;
  display: flex;
  justify-content: flex-end;
  min-width: 0;
}
.c-meta {
  grid-area: meta;
  display: none;
  flex-wrap: wrap;
  gap: 2px 10px;
  font-size: 11.5px;
  min-width: 0;
}
</style>
