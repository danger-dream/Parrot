<script setup lang="ts">
import { computed } from 'vue'
import { ChevronDown, ChevronUp, GripVertical } from '@lucide/vue'
import { compact, pctValue, usd } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import UsageView from './UsageView.vue'
import { cacheShare, promptTokens, tps } from './channelStats'
import { accountAlerts, planFacts, planText } from './facts'
import { accountState, countdown, num, providerMeta, usageValue, type AccountSummary } from './meta'

/** Everything shown comes from `account.card` (server-built, TG _format_account_block). */
const props = defineProps<{
  account: AccountSummary
  selected?: boolean
  dragging?: boolean
  now: number
  /** Index / count in the visible order, for the touch-device move buttons. */
  index: number
  count: number
}>()
const emit = defineEmits<{ move: [delta: number] }>()

const a = computed(() => props.account)
const card = computed(() => a.value.card || null)
const state = computed(() => accountState(a.value))
const plan = computed(() => planText(a.value, card.value?.plan, card.value?.usageSections))
const facts = computed(() => planFacts(a.value, card.value?.plan, card.value?.usageSections))
/** OpenAI credits / reset cards appear as facts; the Qoder 套餐 row is the subtitle. */
const sections = computed(() =>
  (card.value?.usageSections || [])
    .filter((s) => s.id !== 'credits')
    .map((s) => (s.id === 'qoderCredits' ? { ...s, items: s.items.filter((i) => i.label !== '套餐') } : s))
    .filter((s) => s.items.length),
)
const hasUsage = computed(() => sections.value.some((s) => s.items.some((it) => num(it.usedPercent) !== null || usageValue(it) || it.note)))

const alerts = computed(() => {
  const list = accountAlerts(a.value, card.value)
  const until = a.value.disabledUntil
  // Add the countdown TG shows next to 「配额禁用 · 预计 …」.
  return list.map((al) => (al.text.startsWith('额度暂停，预计') && until ? { ...al, text: `${al.text}（${countdown(until, props.now).replace('后重置', '后')}）` } : al))
})

const period = computed(() => card.value?.localPeriod || null)
const stats = computed(() => period.value?.stats || null)
const cost = computed(() => {
  const m = stats.value
  if (!m) return ''
  if (m.costTicks || !m.unpricedSuccess) return usd(m.costTicks)
  return '未计价'
})
const projection = computed(() => card.value?.weeklyProjection || null)
</script>

<template>
  <article class="ac" :class="{ selected, dragging, dim: state.key === 'invalid' || state.key === 'disabled' }" tabindex="0">
    <header class="top">
      <ProviderIcon :provider="account.provider" :size="32" />
      <div class="who">
        <p class="name truncate" :title="account.displayName">{{ account.displayName }}</p>
        <p class="sub truncate">
          {{ providerMeta(account.provider).label }}<template v-if="plan"> · <b>{{ plan }}</b></template><template v-if="account.identity !== account.displayName"> · <span data-private>{{ account.identity }}</span></template>
        </p>
      </div>
      <Badge :tone="state.tone" dot>{{ state.label }}</Badge>
    </header>

    <ul v-if="alerts.length" class="alerts">
      <li v-for="(al, i) in alerts" :key="i" :class="`t-${al.tone}`">{{ al.text }}</li>
    </ul>

    <dl v-if="facts.length" class="facts">
      <div v-for="f in facts" :key="f.label" class="fact">
        <dt>{{ f.label }}</dt>
        <dd :class="[f.tone ? `c-${f.tone === 'neutral' ? 'muted' : f.tone}` : '', { mono: f.mono }]">{{ f.value }}</dd>
      </div>
    </dl>

    <div class="usage">
      <UsageView v-if="hasUsage" :sections="sections" compact :limit="5" :now="now" />
      <p v-else class="none">尚未获取额度数据</p>
    </div>

    <div v-if="period" class="local">
      <p class="lrow">
        <span class="lk">本期</span>
        <template v-if="stats && stats.total">
          <span class="mono">↑{{ compact(promptTokens(stats)) }}</span>
          <span class="mono">↓{{ compact(stats.outputTokens) }}</span>
          <span v-if="cacheShare(stats) !== null" class="u">缓存占比 <span class="mono">{{ pctValue(cacheShare(stats)) }}</span></span>
          <span v-if="stats.averageTokensPerSecond" class="u">均 <span class="mono">{{ tps(stats.averageTokensPerSecond) }}</span></span>
          <span class="cost mono">{{ cost }}</span>
        </template>
        <span v-else-if="period.statsLoading" class="u">统计初始化中…</span>
        <span v-else class="u">暂无本地请求</span>
      </p>
      <p v-if="projection" class="lrow small">
        <span class="u">7 天额度预测</span><span class="mono">{{ compact(projection.tokens) }}</span><template v-if="projection.costTicks"><span class="u">·</span><span class="mono">{{ usd(projection.costTicks) }}</span></template>
      </p>
    </div>

    <footer class="foot">
      <span :title="`共 ${account.modelCount} 个模型，停用 ${account.disabledModelCount} 个`">模型 <b class="mono">{{ account.modelCount - account.disabledModelCount }}/{{ account.modelCount }}</b></span>
      <span>并发 <b class="mono">{{ account.maxConcurrent || '不限' }}</b></span>
      <span class="grip" title="拖动卡片调整调度顺序"><GripVertical /></span>
      <span class="moves" @click.stop @pointerdown.stop>
        <button type="button" :disabled="index <= 0" aria-label="上移" @click="emit('move', -1)"><ChevronUp /></button>
        <button type="button" :disabled="index >= count - 1" aria-label="下移" @click="emit('move', 1)"><ChevronDown /></button>
      </span>
    </footer>
  </article>
</template>

<style scoped>
.ac {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 9px;
  min-width: 0;
  padding: 13px 14px 10px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  border: 0.5px solid var(--border);
  box-shadow: var(--card-highlight), var(--shadow-sm);
  cursor: pointer;
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
  transition: background 0.2s, border-color 0.2s, box-shadow 0.2s, opacity 0.2s;
}
.ac:hover {
  background: var(--surface-hover);
  border-color: var(--border-strong);
}
.ac.selected {
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
  box-shadow: var(--card-highlight), 0 0 0 1px color-mix(in srgb, var(--accent) 40%, transparent);
}
.ac.dragging {
  opacity: 0.55;
  border-style: dashed;
  border-color: var(--accent);
}
.ac.dim .top,
.ac.dim .usage,
.ac.dim .facts {
  opacity: 0.72;
}
.top {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.who {
  flex: 1;
  min-width: 0;
}
.name {
  font-size: 13px;
  font-weight: 600;
}
.sub {
  margin-top: 1px;
  font-size: 11.5px;
  color: var(--text-3);
}
.sub b {
  font-weight: 600;
  color: var(--text-2);
}
.alerts {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.alerts li {
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  line-height: 1.45;
}
.t-red {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
}
.t-orange {
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 11%, transparent);
}
.t-blue {
  color: var(--blue);
  background: color-mix(in srgb, var(--blue) 10%, transparent);
}
.facts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 0;
  min-width: 0;
}
.fact {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  min-width: 0;
  max-width: 100%;
  font-size: 11.5px;
}
.fact dt {
  color: var(--text-3);
  white-space: nowrap;
}
.fact dd {
  margin: 0;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.c-muted {
  color: var(--text-2);
}
.usage {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 7px;
  min-height: 34px;
}
.none {
  font-size: 11.5px;
  color: var(--text-3);
  padding: 4px 0;
}
.local {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
}
.lrow {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 2px 6px;
  font-size: 12px;
  min-width: 0;
}
.lrow.small {
  font-size: 11px;
  color: var(--text-2);
}
.lk {
  font-size: 11px;
  color: var(--text-3);
}
.u {
  color: var(--text-3);
  font-size: 11px;
}
.cost {
  margin-left: auto;
  font-weight: 600;
}
.foot {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 11px;
  color: var(--text-3);
}
.foot b {
  font-weight: 500;
  color: var(--text-2);
}
.grip {
  margin-left: auto;
  display: grid;
  place-items: center;
  color: var(--text-3);
  cursor: grab;
}
.grip svg {
  width: 14px;
  height: 14px;
}
.moves {
  display: none;
  gap: 4px;
}
.moves button {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  color: var(--text-2);
  background: var(--fill-2);
}
.moves button:disabled {
  opacity: 0.35;
}
.moves svg {
  width: 15px;
  height: 15px;
}
/* Touch devices: long-press drags too, but explicit buttons are the reliable path. */
@media (hover: none) {
  .grip {
    display: none;
  }
  .moves {
    display: flex;
    margin-left: auto;
  }
}
</style>
