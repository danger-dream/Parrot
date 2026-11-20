<script setup lang="ts">
import { computed, ref } from 'vue'
import { CircleCheck, CircleMinus, CircleX, ChevronDown, ChevronRight } from '@lucide/vue'
import Badge from '@/components/ui/Badge.vue'
import {
  CATEGORY_LOOK,
  PHASE_OPTIONS,
  STEP_META,
  THRESHOLD_SCOPE_OPTIONS,
  categoryLabel,
  describeStep,
  finalLabel,
  providerLabel,
  untilRelative,
  type CategoryInfo,
  type TestResult,
} from './model'

const props = defineProps<{ result: TestResult; categories: CategoryInfo[]; compact?: boolean }>()

const LAYER_LABEL: Record<string, string> = { draft: '正在编辑', user: '我的规则', preset: '预置规则' }
const SOURCE_LABEL: Record<string, string> = {
  message_time: '从错误文本里读到的时间',
  retry_after: '上游的 Retry-After',
  usage_window: '额度窗口结束时间',
  boundary: '下一个周期边界',
  fixed: '固定时长',
  fallback: '没读到时间，用了保底时长',
}

type PlanStep = NonNullable<TestResult['plan']>['steps'][number]

const showAll = ref(false)
const matched = computed(() => props.result.matched)
const plan = computed(() => props.result.plan)
const obs = computed(() => props.result.observation)

function stepText(s: PlanStep): string {
  if (s.type === 'pause') {
    const what = s.scope === 'account' ? '暂停整个账号' : '暂停这个渠道的该模型'
    if (s.until) return `${what}到 ${s.untilText}（${untilRelative(s.until)}，${SOURCE_LABEL[s.source || ''] || s.source}）`
    return `${what}，时长按冷却阶梯逐级延长`
  }
  if (s.type === 'retry') return `${describeStep(s)}（重试成功就结束；还是这个错误才继续）`
  return describeStep(s)
}

function thresholdText(t: NonNullable<TestResult['plan']>['thresholds'][number]): string {
  const scope = THRESHOLD_SCOPE_OPTIONS.find((o) => o.value === t.scope)?.label || ''
  const when = t.mode === 'consecutive' ? `连续 ${t.count} 次` : `${t.windowMinutes} 分钟内 ${t.count} 次`
  return `${scope}${when}时：${t.steps.map(stepText).join('、')}`
}

const visibleTrace = computed(() => {
  const all = props.result.trace
  if (showAll.value || all.length <= 6) return all
  const idx = all.findIndex((t) => t.status === 'matched')
  const keep = new Set<number>()
  all.forEach((t, i) => {
    if (t.layer === 'draft' || t.status === 'matched' || i < 3 || (idx >= 0 && Math.abs(i - idx) <= 1)) keep.add(i)
  })
  return all.filter((_, i) => keep.has(i))
})

const phaseLabel = computed(() => PHASE_OPTIONS.find((p) => p.value === obs.value.phase)?.label || obs.value.phase)
</script>

<template>
  <div class="trace" :class="{ compact }">
    <div class="verdict" :style="matched ? { '--c': CATEGORY_LOOK[matched.category].color } : undefined" :class="{ none: !matched }">
      <template v-if="matched">
        <component :is="CATEGORY_LOOK[matched.category].icon" class="v-ico" />
        <div class="v-main">
          <p class="v-title">由{{ LAYER_LABEL[matched.layer] }}「{{ matched.name }}」处理</p>
          <p class="v-sub">判定为 <b>{{ categoryLabel(categories, matched.category) }}</b></p>
        </div>
      </template>
      <template v-else>
        <CircleMinus class="v-ico" />
        <div class="v-main">
          <p class="v-title">没有规则命中</p>
          <p class="v-sub">交给内置处理和默认策略：换下一个渠道，并按「错误冷却」阶梯暂停这个模型。</p>
        </div>
      </template>
    </div>

    <div v-if="!compact" class="obs">
      <span class="k">识别到</span>
      <span>{{ phaseLabel }}</span>
      <Badge v-if="obs.status" tone="neutral" mono>HTTP {{ obs.status }}</Badge>
      <span v-if="obs.provider">· {{ providerLabel(obs.provider) }}</span>
      <span v-if="obs.model" class="mono">· {{ obs.model }}</span>
      <template v-if="obs.codes.length">
        <span>· 错误码</span>
        <Badge v-for="c in obs.codes.slice(0, 4)" :key="c" tone="neutral" mono>{{ c }}</Badge>
      </template>
      <p v-if="obs.message" class="obs-msg">{{ obs.message }}</p>
    </div>

    <div v-if="plan" class="block">
      <p class="block-title">每次遇到时</p>
      <ol class="steps">
        <li v-for="(s, i) in plan.steps" :key="i" :style="{ '--t': STEP_META[s.type].tone }">
          <component :is="STEP_META[s.type].icon" class="st-ico" />
          <span>{{ stepText(s) }}</span>
        </li>
        <li class="final">最后：{{ finalLabel(plan.final) }}</li>
      </ol>
      <template v-if="plan.thresholds.length">
        <p class="block-title th-title">累计触发</p>
        <ul class="ths">
          <li v-for="(t, i) in plan.thresholds" :key="i">
            {{ thresholdText(t) }}
            <span v-if="t.current !== null && t.current !== undefined" class="cur">（这个渠道现在已累计 {{ t.current }} 次）</span>
          </li>
        </ul>
      </template>
      <div v-if="plan.response" class="reply">
        <p class="reply-head">{{ plan.final === 'respond' ? '客户端收到' : '所有渠道都不可用时，客户端收到' }}</p>
        <div class="reply-box mono">
          <span class="reply-status">HTTP {{ plan.response.status }}</span>
          <span v-if="plan.response.retryAfter" class="reply-hdr">Retry-After: {{ plan.response.retryAfter }}</span>
          <p class="reply-msg">{{ plan.response.message }}</p>
        </div>
      </div>
    </div>

    <div class="block">
      <p class="block-title">逐条匹配过程</p>
      <ul class="rows">
        <li v-for="t in visibleTrace" :key="`${t.layer}:${t.id}`" :class="t.status">
          <CircleCheck v-if="t.status === 'matched'" class="r-ico ok" />
          <CircleX v-else-if="t.status === 'missed'" class="r-ico" />
          <CircleMinus v-else class="r-ico" />
          <span class="r-layer">{{ LAYER_LABEL[t.layer] }}</span>
          <span class="r-name" :title="t.name">{{ t.name }}</span>
          <span v-if="t.reason" class="r-reason" :title="t.reason">{{ t.reason }}</span>
          <span v-else class="r-reason ok">命中</span>
        </li>
      </ul>
      <button v-if="result.trace.length > visibleTrace.length || showAll" type="button" class="more" @click="showAll = !showAll">
        <component :is="showAll ? ChevronDown : ChevronRight" class="m-ico" />
        {{ showAll ? '收起' : `展开全部 ${result.trace.length} 条` }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.trace {
  display: flex;
  flex-direction: column;
  gap: 14px;
  font-size: 12.5px;
}
.verdict {
  --c: var(--text-3);
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 12px 14px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--c) 11%, transparent);
  border: 0.5px solid color-mix(in srgb, var(--c) 32%, transparent);
}
.verdict.none {
  background: var(--fill-2);
  border-color: var(--hairline);
}
.v-ico {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  margin-top: 1px;
  color: var(--c);
}
.v-title {
  font-size: 13.5px;
  font-weight: 650;
  line-height: 1.4;
}
.v-sub {
  margin-top: 2px;
  color: var(--text-2);
  line-height: 1.5;
}
.obs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  color: var(--text-2);
}
.obs .k {
  color: var(--text-3);
}
.obs-msg {
  flex-basis: 100%;
  margin-top: 2px;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--fill-2);
  color: var(--text);
  word-break: break-word;
}
.block-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  margin-bottom: 6px;
  letter-spacing: 0.02em;
}
.steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  line-height: 1.5;
}
.steps li {
  --t: var(--text-3);
  display: flex;
  gap: 6px;
  align-items: flex-start;
}
.st-ico {
  width: 14px;
  height: 14px;
  margin-top: 3px;
  flex-shrink: 0;
  color: var(--t);
}
.steps li.final {
  font-weight: 600;
  padding-left: 20px;
}
.th-title {
  margin-top: 10px;
}
.ths {
  margin: 0;
  padding-left: 18px;
  color: var(--text-2);
  line-height: 1.5;
}
.cur {
  color: var(--text-3);
}
.reply {
  margin-top: 10px;
}
.reply-head {
  color: var(--text-3);
  margin-bottom: 4px;
}
.reply-box {
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--surface-sunken, var(--fill-2));
  border: 0.5px solid var(--hairline);
  font-size: 11.5px;
}
.reply-status {
  font-weight: 650;
}
.reply-hdr {
  margin-left: 10px;
  color: var(--text-2);
}
.reply-msg {
  margin-top: 4px;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
}
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rows li {
  display: grid;
  grid-template-columns: 16px 64px minmax(0, auto) minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border-radius: 6px;
  color: var(--text-3);
}
.rows li.matched {
  background: color-mix(in srgb, var(--green) 10%, transparent);
  color: var(--text);
}
.rows li.skipped {
  opacity: 0.7;
}
.r-ico {
  width: 14px;
  height: 14px;
}
.r-ico.ok {
  color: var(--green);
}
.r-layer {
  font-size: 11px;
}
.r-name {
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rows li.matched .r-name {
  color: var(--text);
  font-weight: 600;
}
.r-reason {
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.r-reason.ok {
  color: var(--text-green, var(--green));
}
.more {
  margin-top: 4px;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 12px;
  color: var(--accent, var(--blue));
  cursor: pointer;
}
.m-ico {
  width: 14px;
  height: 14px;
}
.compact .rows li {
  grid-template-columns: 16px 56px minmax(0, auto) minmax(0, 1fr);
}
@media (max-width: 720px) {
  .rows li {
    grid-template-columns: 16px minmax(0, 1fr);
  }
  .r-layer,
  .r-reason {
    grid-column: 2;
  }
}
</style>
