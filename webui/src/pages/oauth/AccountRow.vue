<script setup lang="ts">
import { computed } from 'vue'
import { ChevronRight, GripVertical, RefreshCw } from '@lucide/vue'
import { compact, dateTime, pctValue, usd } from '@/utils/format'
import ProviderIcon from '@/components/ProviderIcon.vue'
import OutcomeStrip from '@/components/ui/OutcomeStrip.vue'
import type { OutcomeItem } from '@/utils/outcomes'
import Switch from '@/components/ui/Switch.vue'
import { cacheShare, localUsageLine, promptTokens, tps } from './channelStats'
import { accountAlerts, hitWindowKeys, planFacts, planText } from './facts'
import { accountState, countdown, noteLabel, num, providerMeta, quotaLabel, remainingAmount, usageValue, type AccountSummary } from './meta'

const props = defineProps<{
  account: AccountSummary
  priority: number
  outcome?: OutcomeItem | null
  outcomeFrom?: string | null
  lastAt?: string | null
  now: number
  selected?: boolean
  dragging?: boolean
  busy?: string
}>()
const emit = defineEmits<{ toggle: [on: boolean]; refresh: []; open: [] }>()

const a = computed(() => props.account)
const card = computed(() => a.value.card || null)
const state = computed(() => accountState(a.value))
const plan = computed(() => planText(a.value, card.value?.plan, card.value?.usageSections))
const alerts = computed(() => accountAlerts(a.value, card.value, { now: props.now }))
const quotaPaused = computed(() => a.value.disabledReason === 'quota' || a.value.quotaLimited || card.value?.statusTag === 'quotaDisabled')
const hitKeys = computed(() => hitWindowKeys(card.value?.usageSections, { quotaPaused: quotaPaused.value, disabledUntil: a.value.disabledUntil }))
const QUOTA_FACT_LABELS = new Set(['积分', '官方重置', '重置卡'])
const quotaFacts = computed(() =>
  planFacts(a.value, card.value?.plan, card.value?.usageSections).filter((f) => QUOTA_FACT_LABELS.has(f.label)),
)
const period = computed(() => card.value?.localPeriod || null)
const stats = computed(() => period.value?.stats || null)
const share = computed(() => cacheShare(stats.value))
const projection = computed(() => card.value?.weeklyProjection || null)

type Item = NonNullable<AccountSummary['card']>['usageSections'][number]['items'][number]
function remaining(i: Item): number | null {
  if (i.remainingPercent != null) return i.remainingPercent
  const used = num(i.usedPercent)
  return used == null ? null : 100 - used
}
function isExpiry(it: Item): boolean {
  return it.label === '套餐' || it.note === 'resetsAt=最近到期'
}
function itemReset(it: Item): string {
  if (!it.resetsAt) return ''
  if (isExpiry(it)) return it.resetsAt.startsWith('9999') ? '' : `${dateTime(it.resetsAt)} 到期`
  return countdown(it.resetsAt, props.now)
}
function itemNote(it: Item): string {
  if (it.note === 'resetsAt=最近到期') return '最近到期'
  return noteLabel(it.note)
}
const quotas = computed(() => {
  const items = (card.value?.usageSections || [])
    .filter((s) => s.id !== 'credits' && s.id !== 'antigravityCredits' && s.id !== 'claudeReset')
    .map((s) => (s.id === 'qoderCredits' ? { ...s, items: s.items.filter((i) => i.label !== '套餐') } : s))
    .flatMap((s) => s.items.map((it) => ({ ...it, label: quotaLabel(s, it), hit: hitKeys.value.has(`${s.id}:${it.label}`) })))
  const bars = items.filter((i) => remaining(i) != null).slice(0, 6)
  const values = items.filter((i) => remaining(i) == null && (usageValue(i) || i.note)).slice(0, Math.max(1, 5 - bars.length))
  return { bars, values }
})
function tone(left: number | null): string {
  if (left == null) return ''
  return left < 15 ? 'r' : left < 40 ? 'o' : 'g'
}
</script>

<template>
  <div
    class="arow"
    :class="{ selected, dragging, dim: state.key === 'invalid' || state.key === 'disabled' }"
    tabindex="0"
    @click="emit('open')"
  >
    <span class="grip" title="拖动调整调度顺序"><GripVertical /></span>

    <div class="c-who">
      <ProviderIcon :provider="account.provider" :size="34" />
      <div class="who">
        <p class="name truncate" :title="account.displayName">{{ account.displayName }}</p>
        <p class="sub truncate who-sub" data-private>{{ account.identity !== account.displayName ? account.identity : providerMeta(account.provider).label }}</p>
        <p class="sub plan-inline" :title="plan">{{ plan || providerMeta(account.provider).label }}</p>
      </div>
    </div>

    <div class="c-plan">
      <p class="strong plan" :title="plan">{{ plan || '—' }}</p>
      <p class="sub">{{ providerMeta(account.provider).label }}</p>
    </div>

    <div class="c-state">
      <span class="badge" :class="`b-${state.tone}`">{{ state.label }}</span>
      <p class="sub">优先级 #{{ priority }}</p>
    </div>

    <div class="c-recent">
      <p class="num t">{{ lastAt ? dateTime(lastAt).slice(0, 11) : '24 小时内无请求' }}</p>
      <OutcomeStrip :item="outcome" :from="outcomeFrom" :width="3" :gap="1.5" :height="18" />
    </div>

    <div class="c-use">
      <dl v-if="stats && stats.total" class="use">
        <dt>Token</dt>
        <dd title="读入（含缓存）/ 写出">↑{{ compact(promptTokens(stats)) }} ↓{{ compact(stats.outputTokens) }}</dd>
        <dt>缓存占比</dt>
        <dd title="缓存读取占读入">{{ pctValue(share) }}</dd>
        <dt>速度</dt>
        <dd title="平均输出速度">{{ tps(stats.averageTokensPerSecond) }}</dd>
        <dt>金额</dt>
        <dd>{{ stats.costTicks || !stats.unpricedSuccess ? usd(stats.costTicks) : '未计价' }}</dd>
        <template v-if="projection">
          <dt>7 天预测</dt>
          <dd :title="`7 天额度预测：${compact(projection.tokens)} tokens${projection.costTicks ? ` · ${usd(projection.costTicks)}` : ''}`">{{ compact(projection.tokens) }}<template v-if="projection.costTicks"> · {{ usd(projection.costTicks) }}</template></dd>
        </template>
      </dl>
      <p v-else class="sub">{{ period?.statsLoading ? '统计初始化中…' : '本期暂无请求' }}</p>
    </div>

    <div class="c-quota">
      <div v-if="alerts.length" class="alerts">
        <p v-for="(al, i) in alerts" :key="i" class="alert" :class="`t-${al.tone}`">{{ al.text }}</p>
      </div>
      <div v-for="(q, i) in quotas.bars" :key="i" class="q">
        <div class="qh">
          <span class="ql-wrap">
            <span class="ql" :class="{ hit: q.hit }">{{ q.label }}</span>
            <span v-if="q.hit" class="hit-tag">已撞限</span>
          </span>
          <span class="qv">剩余 <b class="num" :class="tone(remaining(q))">{{ pctValue(remaining(q) ?? 0) }}</b><template v-if="remainingAmount(q)"> · {{ remainingAmount(q) }}</template><template v-if="itemReset(q)"> · {{ itemReset(q) }}</template></span>
        </div>
        <div class="bar"><span :class="tone(remaining(q))" :style="{ width: `${Math.max(0, Math.min(100, remaining(q) ?? 0))}%` }" /></div>
        <p v-if="localUsageLine(q.local, q)" class="qf num">{{ localUsageLine(q.local, q) }}</p>
      </div>
      <p v-for="(v, i) in quotas.values" :key="`v${i}`" class="val">
        <span>{{ v.label }}</span>
        <template v-if="usageValue(v)"> <b class="num">{{ usageValue(v) }}</b></template>
        <span v-if="v.note" class="vn">{{ usageValue(v) ? ' · ' : ' ' }}{{ itemNote(v) }}</span>
        <span v-if="itemReset(v)" class="vn"> · {{ itemReset(v) }}</span>
      </p>
      <dl v-if="quotaFacts.length" class="qfacts">
        <div v-for="f in quotaFacts" :key="f.label" class="qfact">
          <dt>{{ f.label }}</dt>
          <dd :class="[f.tone ? `c-${f.tone === 'neutral' ? 'muted' : f.tone}` : '', { mono: f.mono }]">{{ f.value }}</dd>
        </div>
      </dl>
      <p v-if="!quotas.bars.length && !quotas.values.length && !quotaFacts.length && !alerts.length" class="sub">尚未获取额度数据</p>
    </div>

    <div class="c-act" @click.stop @pointerdown.stop>
      <div class="sw" :title="account.enabled ? '停用后不再调度到这个账号' : '启用后重新参与调度'">
        <span>{{ account.enabled ? '启用' : '停用' }}</span>
        <Switch :model-value="account.enabled" :loading="busy === 'toggle'" label="启用账号" @update:model-value="emit('toggle', $event)" />
      </div>
      <button class="act" title="从官方拉取最新额度" :disabled="busy === 'refresh'" @click="emit('refresh')"><RefreshCw :class="{ spin: busy === 'refresh' }" />刷新额度</button>
      <button class="act primary" @click="emit('open')">详情<ChevronRight /></button>
    </div>
  </div>
</template>

<style scoped>
.arow {
  display: grid;
  grid-template-columns: var(--cols);
  align-items: center;
  column-gap: 12px;
  padding: 12px 16px 12px 8px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
}
.arow:hover {
  box-shadow: var(--shadow-md);
}
.arow.selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 4%, var(--surface));
}
.arow.dragging {
  opacity: 0.55;
  box-shadow: var(--shadow-lg);
}
.arow.dim .c-who,
.arow.dim .c-plan,
.arow.dim .c-use {
  opacity: 0.6;
}
.arow > * {
  min-width: 0;
}
.grip {
  display: grid;
  place-items: center;
  color: var(--text-3);
  cursor: grab;
}
.grip svg {
  width: 15px;
  height: 15px;
}
.c-who {
  display: flex;
  align-items: center;
  gap: 10px;
}
.who {
  min-width: 0;
}
.name {
  font-size: 13.5px;
  font-weight: 650;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.strong {
  font-weight: 600;
  font-size: 13px;
}
.plan-inline {
  display: none;
  color: var(--text-2);
  overflow-wrap: anywhere;
}
.plan {
  line-height: 1.35;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}
.c-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.badge {
  padding: 2px 9px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid transparent;
}
.b-green {
  color: var(--text-green);
  background: color-mix(in srgb, var(--green) 10%, transparent);
  border-color: color-mix(in srgb, var(--green) 28%, transparent);
}
.b-orange {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 10%, transparent);
  border-color: color-mix(in srgb, var(--orange) 30%, transparent);
}
.b-red {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 9%, transparent);
  border-color: color-mix(in srgb, var(--red) 26%, transparent);
}
.b-neutral {
  color: var(--text-2);
  background: var(--fill-2);
}
.c-recent {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.c-recent .t {
  font-size: 12.5px;
  color: var(--text-2);
}
/* Labelled values in a fixed order, so the same figure lines up from row to row. */
.c-use {
  min-width: 0;
}
.use {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 3px 10px;
  margin: 0;
  font-size: 12px;
  line-height: 1.35;
}
.use dt {
  color: var(--text-3);
}
.use dd {
  margin: 0;
  font-weight: 550;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.c-quota {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.alerts {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.q {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.qh {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 2px 8px;
  font-size: 11.5px;
}
.ql-wrap {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  flex-shrink: 0;
}
.ql {
  padding: 0 6px;
  border-radius: 4px;
  color: var(--text-2);
  background: var(--fill-2);
  white-space: nowrap;
}
.ql.hit {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 12%, transparent);
  font-weight: 600;
}
.hit-tag {
  display: inline-block;
  padding: 0 5px;
  border-radius: 4px;
  font-size: 10.5px;
  font-weight: 650;
  line-height: 16px;
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 12%, transparent);
  white-space: nowrap;
  flex-shrink: 0;
}
.qv {
  margin-left: auto;
  color: var(--text-3);
  text-align: right;
  min-width: 0;
}
.qv b {
  font-size: 12.5px;
  color: var(--text);
}
.qv b.o {
  color: var(--text-orange);
}
.qv b.r {
  color: var(--text-red);
}
.qf {
  font-size: 11px;
  color: var(--text-3);
}
.bar {
  height: 6px;
  border-radius: 3px;
  background: var(--track);
  overflow: hidden;
}
.bar span {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--accent);
}
.bar .g { background: var(--green); }
.bar .o { background: var(--orange); }
.bar .r { background: var(--red); }
.val {
  font-size: 11.5px;
  color: var(--text-2);
}
.vn {
  color: var(--text-3);
}
.qfacts {
  display: flex;
  flex-wrap: wrap;
  gap: 3px 12px;
  margin: 0;
  font-size: 11.5px;
}
.qfact {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
}
.qfact dt {
  color: var(--text-3);
}
.qfact dd {
  margin: 0;
  font-weight: 550;
}
.c-muted {
  color: var(--text-2);
}
.alert {
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  line-height: 1.4;
}
.t-red {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 8%, transparent);
}
.t-orange {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 9%, transparent);
}
.t-neutral,
.t-blue {
  color: var(--text-2);
  background: var(--fill-2);
}
.c-act {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  cursor: default;
}
.sw {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  height: 26px;
  padding-left: 2px;
  font-size: 12px;
  color: var(--text-2);
}
.act {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 26px;
  border-radius: 7px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  color: var(--text-2);
  background: var(--fill-2);
}
.act:hover:not(:disabled) {
  background: var(--fill);
}
.act:disabled {
  opacity: 0.6;
}
.act.primary {
  color: var(--accent);
  background: var(--accent-soft);
}
.act.primary:hover {
  background: color-mix(in srgb, var(--accent) 18%, transparent);
}
.act svg {
  width: 13px;
  height: 13px;
}
.spin {
  animation: spin 0.9s linear infinite;
}
</style>
