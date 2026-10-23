<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, Brain, DatabaseZap, Globe, Repeat, ShieldAlert, Star, TriangleAlert, Zap } from '@lucide/vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import StatusBars from '@/components/ui/StatusBars.vue'
import { clock, dateTime, ms, usd } from '@/utils/format'
import { statusLabel, statusTone } from './status'
import {
  cacheRatio,
  effortText,
  hasBilling,
  ingressTag,
  modelChanged,
  pct,
  promptTokens,
  rerouteLabel,
  rowChannelName,
  rowError,
  rowProvider,
  rowTps,
  speedBadge,
  tok,
  tps,
  wsTag,
  type LogRow,
} from './logView'

// One request in the live / history list, laid out like a monitoring table row: every fact of the
// Telegram list card (`ui.fmt_log_entry_headline/body`) is on the row; the rest is in the detail.
const props = defineProps<{
  row: LogRow
  now?: number
  fresh?: boolean
  selected?: boolean
  withDate?: boolean
  recent?: readonly string[]
}>()

const r = computed(() => props.row)
const tone = computed(() => statusTone(r.value.status))
const pending = computed(() => r.value.status === 'pending')
const failed = computed(() => r.value.status === 'error' || r.value.status === 'cancelled')
const elapsed = computed(() => {
  if (!pending.value || !r.value.createdAt || !props.now) return null
  return Math.max(0, props.now - new Date(r.value.createdAt).getTime())
})
const prompt = computed(() => promptTokens(r.value))
const ratio = computed(() => cacheRatio(r.value))
const speed = computed(() => rowTps(r.value))
const ing = computed(() => ingressTag(r.value))
const ws = computed(() => wsTag(r.value))
const fast = computed(() => speedBadge(r.value))
const showTokens = computed(() => prompt.value > 0 || r.value.outputTokens > 0)
const showCost = computed(() => !pending.value && hasBilling(r.value))
const firstToken = computed(() => (r.value.isStream === false ? null : r.value.firstTokenMilliseconds ?? null))
const total = computed(() => (pending.value ? elapsed.value : r.value.durationMilliseconds ?? null))

function grade(v: number | null | undefined, warn: number, bad: number): 'g' | 'o' | 'r' | '' {
  if (v == null) return ''
  return v >= bad ? 'r' : v >= warn ? 'o' : 'g'
}
const firstGrade = computed(() => grade(firstToken.value, 5_000, 15_000))
const totalGrade = computed(() => grade(total.value, 30_000, 90_000))
const barGrade = computed(() => {
  if (pending.value) return 'b'
  if (failed.value) return 'r'
  const order = { r: 3, o: 2, g: 1, '': 0 }
  return order[firstGrade.value] >= order[totalGrade.value] ? firstGrade.value || 'g' : totalGrade.value
})
const billing = computed(() => r.value.billing)
const costKind = computed(() => {
  const b = billing.value
  if (!b) return ''
  if (b.actualCostTicks && !b.estimatedCostTicks) return '实际'
  if (b.estimatedCostTicks && !b.actualCostTicks) return '估算'
  if (b.actualCostTicks && b.estimatedCostTicks) return '实际+估算'
  return ''
})
</script>

<template>
  <button class="row" :class="[`t-${tone}`, { fresh, selected, pending, failed }]" type="button" :data-id="row.id" :data-status="row.status">
    <span class="c-time">
      <span class="main num">{{ clock(row.createdAt) }}</span>
      <span class="sub num">{{ withDate ? dateTime(row.createdAt).slice(0, 5) : '' }}</span>
    </span>

    <span class="c-key">
      <span class="main truncate strong">{{ row.apiKeyName || '—' }}</span>
      <span class="sub mono truncate">{{ row.clientIp || '' }}</span>
    </span>

    <span class="c-channel">
      <span class="main ch">
        <template v-if="row.channelId">
          <ProviderIcon :provider="rowProvider(row)" :size="18" />
          <span class="truncate">{{ rowChannelName(row) }}</span>
        </template>
        <span v-else class="faint">{{ pending ? '分配中…' : '未分配渠道' }}</span>
      </span>
      <span class="sub meta">
        <span v-if="row.proxyName && row.proxyName !== 'direct'" class="truncate" :title="`出站代理 ${row.proxyName}`">代理 {{ row.proxyName }}</span>
        <span v-if="row.retryCount" class="warn"><Repeat />重试 {{ row.retryCount }}</span>
        <span v-if="row.affinityHit" class="aff"><Star />亲和</span>
      </span>
    </span>

    <span class="c-model">
      <span class="main model">
        <span class="mono truncate strong">{{ row.requestedModel || '—' }}</span>
        <template v-if="modelChanged(row)">
          <ArrowRight class="arrow" />
          <span class="mono truncate">{{ row.finalModel }}</span>
        </template>
      </span>
      <span class="sub tags">
        <span v-if="ing" class="tag">{{ ing }}</span>
        <span class="tag" :class="row.isStream ? 't-stream' : ''">{{ row.isStream ? '流式' : '同步' }}</span>
        <span v-if="ws" class="tag t-stream">{{ ws }}</span>
        <span v-if="row.reasoningEffort" class="tag t-effort" :title="`思考强度 ${row.reasoningEffort}`"><Brain />{{ effortText(row.reasoningEffort) }}</span>
        <span v-if="fast" class="tag t-fast"><Zap />{{ fast }}</span>
        <span v-if="row.localSearchCount" class="tag t-search" :title="`本地搜索 ${row.localSearchCount} 次`"><Globe />{{ row.localSearchCount }}</span>
        <span v-if="row.modelSignalConflict" class="tag t-warn" title="上游模型信号冲突"><TriangleAlert />模型信息不一致</span>
        <span v-else-if="row.upstreamActualModel" class="tag t-warn" :title="rerouteLabel(row.channelType)"><TriangleAlert />{{ row.upstreamActualModel }}</span>
        <span v-if="row.safetyReview" class="tag t-warn"><ShieldAlert />安全审查</span>
      </span>
    </span>

    <span class="c-recent"><StatusBars :outcomes="recent" :slots="10" :width="4" :gap="2" :height="18" /></span>

    <span class="c-status">
      <span class="pill" :class="`p-${tone}`"><i />{{ statusLabel(row.status) }}</span>
      <span class="sub num">{{ row.httpStatus ? `HTTP ${row.httpStatus}` : pending ? '处理中' : '' }}</span>
    </span>

    <span class="c-tok">
      <template v-if="showTokens">
        <span class="main num"><em class="up">↑</em>{{ tok(prompt) }} <em class="down">↓</em>{{ tok(row.outputTokens) }}</span>
        <span class="sub num cache" :class="{ hit: row.cacheReadTokens > 0 }"><DatabaseZap />{{ row.cacheReadTokens > 0 ? `${tok(row.cacheReadTokens)} · ${pct(ratio)}` : '无缓存' }}</span>
        <span class="tip" role="tooltip">
          <b>Token 明细</b>
          <span>输入（未缓存）<i>{{ row.inputTokens.toLocaleString() }}</i></span>
          <span>缓存读取<i>{{ row.cacheReadTokens.toLocaleString() }}</i></span>
          <span>缓存写入<i>{{ row.cacheCreationTokens.toLocaleString() }}</i></span>
          <span>输出<i>{{ row.outputTokens.toLocaleString() }}</i></span>
          <span class="sum">合计<i>{{ (prompt + row.outputTokens).toLocaleString() }}</i></span>
        </span>
      </template>
      <span v-else class="main faint">—</span>
    </span>

    <span class="c-cost">
      <span class="main num" :class="{ cost: showCost }">{{ showCost ? usd(billing?.costTicks ?? row.costTicks, { precise: true }) : '—' }}</span>
      <span class="sub">{{ showCost ? costKind : '' }}</span>
    </span>

    <span class="c-lat" :class="`l-${barGrade}`">
      <span class="ln"><em>首字</em><b class="num" :class="firstGrade">{{ firstToken != null ? ms(firstToken) : '—' }}</b></span>
      <span class="ln"><em>{{ pending ? '已进行' : '总耗时' }}</em><b class="num" :class="pending ? '' : totalGrade">{{ ms(total) }}</b></span>
      <span class="ln"><em>TPS</em><b class="num">{{ speed != null ? tps(speed) : '—' }}</b></span>
    </span>

    <span v-if="failed && (row.error || row.errorSummary)" class="c-err">
      <TriangleAlert />
      <span class="truncate">{{ row.status === 'cancelled' ? '已取消 · ' : '' }}{{ rowError(row) }}</span>
    </span>
  </button>
</template>

<style scoped>
/* Columns come from the parent table (--cols / --areas) so the header stays aligned. */
.row {
  position: relative;
  display: grid;
  grid-template-columns: var(--cols);
  grid-template-areas: var(--areas);
  align-items: center;
  column-gap: 12px;
  row-gap: 6px;
  width: 100%;
  padding: 9px 16px;
  text-align: left;
  font-size: 13px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  transition: border-color 0.15s, background 0.15s, box-shadow 0.15s;
}
.row:hover {
  box-shadow: var(--shadow-md);
}
.row.selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 4%, var(--surface));
}
.row.fresh {
  animation: fresh 1.6s var(--ease-out);
}
@keyframes fresh {
  from {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface));
  }
}
.row.failed {
  border-color: color-mix(in srgb, var(--red) 28%, var(--border));
}
.row > span {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.main {
  min-width: 0;
  line-height: 1.35;
}
.sub {
  font-size: 11.5px;
  line-height: 16px;
  color: var(--text-3);
}
.sub:empty {
  display: none;
}
.strong {
  font-weight: 600;
}
.faint {
  color: var(--text-3);
}
.c-time { grid-area: time; }
.c-key { grid-area: key; }
.c-channel { grid-area: channel; }
.c-model { grid-area: model; }
.c-recent { grid-area: recent; }
.c-status { grid-area: status; }
.c-tok { grid-area: tok; position: relative; }
.c-cost { grid-area: cost; }
.c-lat { grid-area: lat; }
.c-err { grid-area: err; }
.ch,
.model {
  display: flex;
  align-items: center;
  gap: 6px;
}
.arrow {
  width: 12px;
  height: 12px;
  flex-shrink: 0;
  color: var(--text-3);
}
.meta,
.tags {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 4px 8px;
  overflow: hidden;
}
.meta span,
.tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
}
.meta svg,
.tag svg {
  width: 11px;
  height: 11px;
}
.meta .warn {
  color: var(--text-orange);
}
.meta .aff {
  color: var(--yellow);
}
.tags {
  gap: 4px;
}
.tag {
  height: 18px;
  padding: 0 6px;
  border-radius: 5px;
  font-size: 11px;
  color: var(--text-2);
  background: var(--fill-2);
}
.t-stream {
  color: var(--accent);
  background: var(--accent-soft);
}
.t-effort {
  color: var(--purple);
  background: color-mix(in srgb, var(--purple) 12%, transparent);
}
.t-fast {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.t-search {
  color: var(--teal);
  background: color-mix(in srgb, var(--teal) 12%, transparent);
}
.t-warn {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  align-self: flex-start;
  height: 22px;
  padding: 0 9px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.pill i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.p-green {
  color: var(--text-green);
  background: color-mix(in srgb, var(--green) 12%, transparent);
}
.p-red {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 11%, transparent);
}
.p-orange,
.p-neutral {
  color: var(--text-2);
  background: var(--fill-2);
}
.p-blue {
  color: var(--accent);
  background: var(--accent-soft);
}
.p-blue i {
  animation: blink 2.4s ease-in-out infinite;
}
@keyframes blink {
  50% {
    opacity: 0.35;
  }
}
.c-tok .main em {
  font-style: normal;
  margin-right: 2px;
}
.up {
  color: var(--green);
}
.down {
  color: var(--purple);
}
.cache {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.cache svg {
  width: 12px;
  height: 12px;
}
.cache.hit {
  color: var(--accent);
}
.tip {
  position: absolute;
  z-index: 20;
  left: 50%;
  bottom: calc(100% + 8px);
  transform: translateX(-50%);
  display: none !important;
  flex-direction: column;
  gap: 4px;
  width: 210px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #111827;
  color: #e5e7eb;
  font-size: 12px;
  box-shadow: var(--shadow-lg);
  pointer-events: none;
}
.c-tok:hover .tip {
  display: flex !important;
}
.tip b {
  margin-bottom: 2px;
  color: #fff;
}
.tip span {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  white-space: nowrap;
}
.tip i {
  font-style: normal;
  font-variant-numeric: tabular-nums;
  color: #fff;
}
.tip .sum {
  margin-top: 4px;
  padding-top: 6px;
  border-top: 1px solid rgba(255, 255, 255, 0.15);
}
.tip .sum i {
  color: #60a5fa;
  font-weight: 700;
}
.cost {
  color: var(--text-green);
  font-weight: 650;
}
.c-lat {
  position: relative;
  padding-left: 10px;
  gap: 1px !important;
}
.c-lat::before {
  content: '';
  position: absolute;
  left: 0;
  top: 2px;
  bottom: 2px;
  width: 3px;
  border-radius: 2px;
  background: var(--track);
}
.l-g::before { background: var(--green); }
.l-o::before { background: var(--orange); }
.l-r::before { background: var(--red); }
.l-b::before { background: var(--accent); }
.ln {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  line-height: 17px;
}
.ln em {
  width: 38px;
  flex-shrink: 0;
  font-style: normal;
  color: var(--text-3);
}
.ln b {
  font-weight: 600;
}
.ln .g { color: var(--text-green); }
.ln .o { color: var(--text-orange); }
.ln .r { color: var(--text-red); }
.c-err {
  flex-direction: row !important;
  align-items: center;
  gap: 6px !important;
  padding: 6px 10px;
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 7%, transparent);
}
.c-err svg {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
}
</style>
