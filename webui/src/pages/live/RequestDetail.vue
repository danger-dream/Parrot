<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowRight, Brain, Globe, ShieldAlert, Star, TriangleAlert, Zap } from '@lucide/vue'
import { api, ApiError, enc } from '@/api/client'
import { onEvent } from '@/api/events'
import type { RequestLogDetailData } from '@/api/types'
import { channelName } from '@/stores/catalog'
import { goLogs, openApiKey, openChannel, openModel, openProxy } from '@/links'
import { channelProvider } from '@/utils/providers'
import { compact, ms, relative } from '@/utils/format'
import { protocolLabel, statusLabel, statusTone } from './status'
import {
  ATTEMPT_KEYS,
  SAFETY_REVIEW_TEXT,
  cacheRatio,
  chainLabel,
  chainMark,
  conflictLines,
  costFormula,
  effortText,
  errorSummary,
  hasBilling,
  modelChanged,
  outboundModel,
  pct,
  pricedAttempts,
  promptTokens,
  requestStageMetrics,
  rerouteLabel,
  rowChannelName,
  rowProvider,
  rowTps,
  speedBadge,
  stageMetrics,
  tgBytes,
  timingMetrics,
  tps,
  usdExact,
  type Json,
  type LogRow,
  type Metric,
} from './logView'
import Badge from '@/components/ui/Badge.vue'
import CopyText from '@/components/ui/CopyText.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import BodyViewer from './BodyViewer.vue'

// Request detail — everything Telegram's single-log page shows (`logs_menu._render_detail`):
// identity, upstream observations, Tokens, billing (with per-attempt formulas), request timing and
// reliable stages, the execution chain with nested upstream rounds, local search log, final error,
// then the request / response bodies.
const props = defineProps<{ id: string }>()

const detail = ref<RequestLogDetailData | null>(null)
const error = ref('')
const bodyTab = ref<'request' | 'response'>('request')
let pollTimer: number | undefined

async function load(): Promise<void> {
  try {
    const res = await api.get<RequestLogDetailData>(`/logs/${enc(props.id)}`)
    detail.value = res.data
    error.value = ''
    window.clearTimeout(pollTimer)
    if (res.data.log.status === 'pending') pollTimer = window.setTimeout(load, 2500)
  } catch (err) {
    error.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND' ? '找不到这条请求，可能已被清理。' : (err as Error).message
  }
}

const off = onEvent('request.finished', ({ log }) => {
  if (log.id === props.id) load()
})

onMounted(load)
onBeforeUnmount(() => {
  off()
  window.clearTimeout(pollTimer)
})
watch(
  () => props.id,
  () => {
    detail.value = null
    load()
  },
)

const log = computed(() => detail.value?.log as LogRow | undefined)
const attempts = computed(() => (detail.value?.attempts || []) as Json[])
const rounds = computed(() => (detail.value?.stages || []) as Json[])
const searches = computed(() => (detail.value?.localWebRounds || []) as Json[])
const billed = computed(() => pricedAttempts((detail.value?.billingAttempts || []) as Json[]))

const str = (v: unknown): string => (v === null || v === undefined ? '' : String(v))
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

function fullStamp(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const p: Record<string, string> = {}
  for (const x of new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(d))
    p[x.type] = x.value
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`
}

// ---- identity ----
const finalAttempt = computed(() => attempts.value[attempts.value.length - 1])
const upstreamProtocol = computed(() => log.value?.upstreamProtocol || str(finalAttempt.value?.upstreamProtocol) || '')
const ingress = computed(() => log.value?.ingressProtocol || log.value?.protocol || '')
const proxyName = computed(() => log.value?.proxyName || str(finalAttempt.value?.proxyName) || '')
const fast = computed(() => (log.value ? speedBadge(log.value) : ''))
const conflict = computed(() => conflictLines(log.value?.modelSignalConflict ?? null))
const isCursor = computed(() => channelProvider(log.value?.channelId) === 'cursor')

// ---- tokens / billing ----
const prompt = computed(() => (log.value ? promptTokens(log.value) : 0))
const ratio = computed(() => (log.value ? cacheRatio(log.value) : null))
const showBilling = computed(() => (log.value ? hasBilling(log.value) : false))
const totalCost = computed(() => log.value?.billing?.costTicks ?? log.value?.costTicks ?? 0)
const singleFormula = computed(() => (billed.value.length === 1 ? costFormula(billed.value[0]) : ''))

// ---- request overview ----
const requestFirstApplicable = computed(() => Boolean(log.value?.isStream || log.value?.transport === 'ws'))
const requestTiming = computed<Metric[]>(() => {
  const l = log.value
  if (!l) return []
  const row: Json = {
    connect: l.connectMilliseconds,
    first: l.firstTokenMilliseconds,
    idle: l.idleMilliseconds,
    total: l.durationMilliseconds,
  }
  return timingMetrics(row, { connect: 'connect', first: 'first', idle: 'idle', total: 'total' }, requestFirstApplicable.value)
})
const requestStages = computed(() => requestStageMetrics(log.value?.stageMilliseconds))
const speed = computed(() => (log.value ? rowTps(log.value) : null))

// ---- execution chain: rounds nest under their channel attempt (retryAttemptId) ----
const chain = computed(() => {
  const byAttempt = new Map<string, Json[]>()
  const loose: Json[] = []
  for (const r of rounds.value) {
    if (r.retryAttemptId === null || r.retryAttemptId === undefined) loose.push(r)
    else {
      const k = String(r.retryAttemptId)
      byAttempt.set(k, [...(byAttempt.get(k) || []), r])
    }
  }
  const items = attempts.value.map((a) => {
    const children = byAttempt.get(String(a.id)) || []
    byAttempt.delete(String(a.id))
    const finalMode = children.length ? str(children[children.length - 1].requestMode) : ''
    const model = str(a.model)
    const outbound = outboundModel(a)
    return {
      a,
      children,
      model,
      outbound: outbound && outbound !== model ? outbound : '',
      timing: timingMetrics(a, ATTEMPT_KEYS, finalMode !== 'http_non_stream'),
      stages: stageMetrics(a),
    }
  })
  for (const rest of byAttempt.values()) loose.push(...rest)
  return { items, loose }
})

function roundView(p: Json) {
  return {
    ok: ['success', 'connected'].includes(str(p.outcome)),
    timing: timingMetrics(p, ATTEMPT_KEYS, str(p.requestMode) !== 'http_non_stream'),
    stages: stageMetrics(p),
    bytes: (num(p.bytesUp) || 0) + (num(p.bytesDown) || 0),
  }
}

function metricText(list: Metric[]): string {
  return list.map((m) => `${m.label} ${ms(m.value)}`).join(' · ')
}

// ---- local search log ----
const searchTotals = computed(() => {
  let results = 0
  let bytesTotal = 0
  for (const s of searches.value) {
    results += num(s.resultCount) || 0
    bytesTotal += num(s.contentBytes) || 0
  }
  return { results, bytes: bytesTotal }
})
function searchMark(s: Json): 'ok' | 'wait' | 'fail' {
  const v = str(s.status)
  return v === 'success' ? 'ok' : v === 'running' ? 'wait' : 'fail'
}
function searchSeconds(s: Json): number | null {
  const a = num(s.startedAt)
  const b = num(s.endedAt)
  return a !== null && b !== null ? (b - a) * 1000 : null
}

const MARK_TONE: Record<string, string> = { ok: 'green', search: 'blue', budget: 'orange', guard: 'orange', wait: 'blue', fail: 'red' }

function bodyPurgedText(at: string | null | undefined): string {
  return at ? `正文已按保留策略于 ${fullStamp(at)} 清理；统计、执行链与计费仍保留。` : ''
}
</script>

<template>
  <div class="detail">
    <p v-if="error" class="err"><TriangleAlert /> {{ error }}</p>
    <template v-else-if="!log">
      <div class="pad"><Skeleton h="60px" r="12px" /><Skeleton h="120px" r="12px" /><Skeleton h="200px" r="12px" /></div>
    </template>
    <template v-else>
      <!-- identity -->
      <section class="hero">
        <div class="hero-top">
          <Badge :tone="statusTone(log.status)" dot>{{ statusLabel(log.status) }}<template v-if="log.httpStatus"> · {{ log.httpStatus }}</template></Badge>
          <span class="when">{{ fullStamp(log.createdAt) }} · {{ relative(log.createdAt) }}</span>
        </div>
        <div class="model">
          <button class="mono lnk" :disabled="!log.requestedModel" @click="openModel(log.requestedModel)">{{ log.requestedModel || '—' }}</button>
          <template v-if="modelChanged(log)">
            <ArrowRight class="arrow" />
            <button class="mono final lnk" @click="openModel(log.finalModel)">{{ log.finalModel }}</button>
          </template>
        </div>
        <div class="flags">
          <span class="flag" :title="`入口 ${protocolLabel(ingress)}${upstreamProtocol ? ` → 上游 ${protocolLabel(upstreamProtocol)}` : ''}`">入口 <b class="mono">{{ ingress || '?' }}</b><template v-if="upstreamProtocol"> → 上游 <b class="mono">{{ upstreamProtocol }}</b></template><template v-if="log.transport"> / <b class="mono">{{ log.transport }}</b></template></span>
          <span v-if="log.isStream" class="flag">stream</span>
          <span v-if="log.affinityHit" class="flag f-yellow"><Star />亲和命中</span>
          <span v-if="log.retryCount" class="flag f-orange">重试 {{ log.retryCount }} 次</span>
          <span v-if="log.reasoningEffort" class="flag f-purple"><Brain />思考强度 {{ effortText(log.reasoningEffort) }}<span v-if="effortText(log.reasoningEffort) !== log.reasoningEffort" class="faint"> ({{ log.reasoningEffort }})</span></span>
          <span v-if="fast" class="flag f-yellow"><Zap />{{ fast }}</span>
          <span v-if="log.localSearchCount || searches.length" class="flag f-blue"><Globe />搜索 {{ log.localSearchCount || searches.length }} 次</span>
        </div>
        <div v-if="conflict.length || log.upstreamActualModel || log.safetyReview" class="observe">
          <template v-if="conflict.length">
            <p class="o-title"><TriangleAlert />模型信息不一致：上游模型信号冲突，不能据此确定实际模型或是否降级</p>
            <p v-for="line in conflict" :key="line" class="o-line mono">{{ line }}</p>
          </template>
          <p v-else-if="log.upstreamActualModel" class="o-title"><TriangleAlert />{{ rerouteLabel(log.channelType) }}：<span class="mono">{{ log.upstreamActualModel }}</span></p>
          <template v-if="log.safetyReview">
            <p class="o-title"><ShieldAlert />安全审查：{{ SAFETY_REVIEW_TEXT }}</p>
            <p v-if="log.safetyReview.reason" class="o-line">原因：{{ log.safetyReview.reason }}</p>
          </template>
        </div>
      </section>

      <dl class="facts">
        <div class="wide">
          <dt>最终渠道 / 模型</dt>
          <dd class="ch">
            <template v-if="log.channelId">
              <ProviderIcon :provider="rowProvider(log)" :size="16" />
              <button class="lnk truncate" @click="openChannel(log.channelId)">{{ rowChannelName(log) }}</button>
              <span class="faint">/</span>
              <span class="mono truncate">{{ log.finalModel || '—' }}</span>
            </template>
            <span v-else class="faint">未分配渠道</span>
          </dd>
        </div>
        <div>
          <dt>API Key</dt>
          <dd><button class="lnk" :disabled="!log.apiKeyName" @click="openApiKey(log.apiKeyName)">{{ log.apiKeyName || '—' }}</button></dd>
        </div>
        <div>
          <dt>客户端</dt>
          <dd class="mono">{{ log.clientIp || '—' }}</dd>
        </div>
        <div>
          <dt>出站代理</dt>
          <dd><button v-if="proxyName && proxyName !== 'direct'" class="lnk mono" @click="openProxy('proxy', proxyName)">{{ proxyName }}</button><span v-else>{{ proxyName === 'direct' ? '直连' : '—' }}</span></dd>
        </div>
        <div>
          <dt>请求 ID</dt>
          <dd class="idc"><CopyText :text="id" :display="id.slice(0, 8) + '…'" /></dd>
        </div>
      </dl>

      <!-- tokens + billing -->
      <section v-if="log.status === 'success' || showBilling" class="metrics">
        <template v-if="log.status === 'success'">
          <div class="m"><p class="k">读入 ↑</p><p class="v">{{ compact(prompt) }}</p><p class="s mono">{{ prompt.toLocaleString('en-US') }}</p></div>
          <div class="m"><p class="k">写出 ↓</p><p class="v">{{ compact(log.outputTokens) }}</p><p class="s mono">{{ log.outputTokens.toLocaleString('en-US') }}</p></div>
          <div class="m">
            <p class="k">缓存读</p>
            <p class="v" :class="{ hit: (log.cacheReadTokens || 0) > 0 }">{{ compact(log.cacheReadTokens || 0) }}</p>
            <p class="s mono">占读入 {{ (log.cacheReadTokens || 0) > 0 ? pct(ratio) : '0%' }}</p>
          </div>
          <div class="m">
            <p class="k">缓存写</p>
            <p class="v">{{ compact(log.cacheCreationTokens || 0) }}</p>
            <p class="s mono">{{ (log.cacheCreationTokens || 0).toLocaleString('en-US') }}</p>
          </div>
        </template>
        <div class="m" :class="{ wide: log.status !== 'success' }">
          <p class="k">金额</p>
          <p class="v">{{ usdExact(totalCost) }}</p>
          <p v-if="log.billing?.unpricedSuccess && !totalCost" class="s">未定价</p>
        </div>
      </section>

      <section v-if="showBilling && (singleFormula || billed.length > 1 || isCursor)" class="sec">
        <p class="sec-title">计费</p>
        <div class="box">
          <p v-if="singleFormula" class="line mono">{{ usdExact(totalCost) }} = {{ singleFormula }}</p>
          <template v-else-if="billed.length > 1">
            <p class="line mono">{{ usdExact(totalCost) }}</p>
            <p v-for="(b, i) in billed" :key="i" class="line mono sub-line">
              尝试 {{ b.attemptOrder ?? i + 1 }}: {{ usdExact(num(b.costTicks)) }}<template v-if="costFormula(b)"> = {{ costFormula(b) }}</template>
            </p>
          </template>
          <p v-if="isCursor" class="line faint">准确计价以官方为准</p>
        </div>
      </section>

      <!-- request overview -->
      <section class="sec">
        <p class="sec-title">请求总览</p>
        <div class="box kvs">
          <div v-if="log.finalRoundId" class="kv"><span class="k">最终轮次</span><span class="v mono">{{ log.finalRoundId }}</span></div>
          <div class="kv">
            <span class="k">业务计时</span>
            <span class="v">
              <template v-if="requestTiming.length"><span v-for="m in requestTiming" :key="m.label" class="chip">{{ m.label }} <b class="mono">{{ ms(m.value) }}</b></span></template>
              <span v-else class="faint">无可靠样本</span>
            </span>
          </div>
          <div v-if="log.requestElapsedMilliseconds != null" class="kv"><span class="k">请求全程（外层）</span><span class="v mono">{{ ms(log.requestElapsedMilliseconds) }}</span></div>
          <div v-if="requestStages.length" class="kv">
            <span class="k">可靠阶段<small>可能重叠，不相加</small></span>
            <span class="v"><span v-for="m in requestStages" :key="m.label" class="chip">{{ m.label }} <b class="mono">{{ ms(m.value) }}</b></span></span>
          </div>
          <div v-if="speed != null" class="kv"><span class="k">生成速度</span><span class="v mono">{{ tps(speed) }}</span></div>
        </div>
      </section>

      <!-- execution chain -->
      <section class="sec">
        <p class="sec-title">执行链 <span class="faint">{{ attempts.length }} 次渠道尝试 / {{ rounds.length }} 个上游轮次</span></p>
        <ol class="timeline">
          <li v-for="(c, i) in chain.items" :key="i" :class="MARK_TONE[chainMark(str(c.a.outcome))]">
            <span class="node" />
            <div class="t-body">
              <div class="t-head">
                <span class="t-order">尝试 {{ c.a.attemptOrder ?? i + 1 }}</span>
                <ProviderIcon :provider="channelProvider(str(c.a.channelKey))" :size="15" />
                <button class="t-name truncate lnk" @click="openChannel(str(c.a.channelKey))">{{ channelName(str(c.a.channelKey)) }}</button>
                <Badge :tone="(MARK_TONE[chainMark(str(c.a.outcome))] as 'green')">{{ chainLabel(str(c.a.outcome)) }}</Badge>
              </div>
              <p class="t-meta">
                <span class="mono">{{ c.model || '—' }}</span>
                <template v-if="c.outbound"> <span class="faint">→</span> <span class="mono">{{ c.outbound }}</span></template>
                <template v-if="c.a.proxyName"> · 代理 {{ c.a.proxyName }}</template>
              </p>
              <p v-if="c.a.finalRoundId" class="t-line">终止轮次 <span class="mono">{{ c.a.finalRoundId }}</span></p>
              <p v-if="c.timing.length" class="t-line">终止轮摘要：{{ metricText(c.timing) }}</p>
              <p v-if="num(c.a.attemptElapsedMs) !== null" class="t-line">渠道尝试全程（外层）：{{ ms(num(c.a.attemptElapsedMs)) }}</p>
              <p v-if="c.stages.length" class="t-line">可靠阶段（可能重叠，不相加）：{{ metricText(c.stages) }}</p>
              <p v-if="c.a.errorDetail" class="t-err">{{ errorSummary(str(c.a.errorDetail)) }}</p>
              <ul v-if="c.children.length" class="rounds">
                <li v-for="(p, j) in c.children" :key="j" :class="{ bad: !roundView(p).ok }">
                  <p class="r-head">
                    <span class="r-mark">{{ roundView(p).ok ? '✓' : '✕' }}</span>
                    轮次 {{ p.attemptOrder ?? '?' }} · <span class="mono">{{ str(p.proxyName) || 'direct' }}</span> — {{ str(p.outcome) || '?' }}
                  </p>
                  <p class="r-line">
                    <template v-if="p.roundId">ID <span class="mono">{{ p.roundId }}</span></template>
                    <template v-if="p.transport"> · 传输 <span class="mono">{{ p.transport }}</span></template>
                    <template v-if="p.requestMode"> · 模式 <span class="mono">{{ p.requestMode }}</span></template>
                  </p>
                  <p v-if="roundView(p).timing.length" class="r-line">业务计时：{{ metricText(roundView(p).timing) }}</p>
                  <p v-if="roundView(p).stages.length" class="r-line">可靠阶段（可能重叠，不相加）：{{ metricText(roundView(p).stages) }}</p>
                  <p v-if="roundView(p).bytes" class="r-line">流量 {{ tgBytes(roundView(p).bytes) }}</p>
                  <p v-if="p.errorDetail" class="t-err">{{ errorSummary(str(p.errorDetail)) }}</p>
                </li>
              </ul>
            </div>
          </li>
          <li v-if="!attempts.length" class="neutral">
            <span class="node" />
            <div class="t-body"><p class="t-meta">无渠道尝试记录</p></div>
          </li>
        </ol>
        <template v-if="chain.loose.length">
          <p class="sec-title sub-title">未关联渠道尝试的上游轮次 <span class="faint">{{ chain.loose.length }} 个</span></p>
          <ul class="rounds flat">
            <li v-for="(p, j) in chain.loose" :key="j" :class="{ bad: !roundView(p).ok }">
              <p class="r-head">
                <span class="r-mark">{{ roundView(p).ok ? '✓' : '✕' }}</span>
                轮次 {{ p.attemptOrder ?? '?' }} · <span class="mono">{{ str(p.proxyName) || 'direct' }}</span> — {{ str(p.outcome) || '?' }}
              </p>
              <p class="r-line">
                <template v-if="p.roundId">ID <span class="mono">{{ p.roundId }}</span></template>
                <template v-if="p.transport"> · 传输 <span class="mono">{{ p.transport }}</span></template>
                <template v-if="p.requestMode"> · 模式 <span class="mono">{{ p.requestMode }}</span></template>
              </p>
              <p v-if="roundView(p).timing.length" class="r-line">业务计时：{{ metricText(roundView(p).timing) }}</p>
              <p v-if="roundView(p).stages.length" class="r-line">可靠阶段（可能重叠，不相加）：{{ metricText(roundView(p).stages) }}</p>
              <p v-if="roundView(p).bytes" class="r-line">流量 {{ tgBytes(roundView(p).bytes) }}</p>
              <p v-if="p.errorDetail" class="t-err">{{ errorSummary(str(p.errorDetail)) }}</p>
            </li>
          </ul>
        </template>
      </section>

      <!-- local search log -->
      <section v-if="searches.length" class="sec">
        <p class="sec-title">搜索日志 <span class="faint">{{ searches.length }} 次<template v-if="searchTotals.results"> · 共 {{ searchTotals.results }} 条</template><template v-if="searchTotals.bytes"> · 共 {{ tgBytes(searchTotals.bytes) }}</template></span></p>
        <ul class="searches">
          <li v-for="(s, i) in searches" :key="i" :class="searchMark(s)">
            <p class="s-head">
              <span class="s-mark">{{ searchMark(s) === 'ok' ? '✓' : searchMark(s) === 'wait' ? '…' : '✕' }}</span>
              <b>{{ i + 1 }}.</b> <span class="mono">{{ s.toolName || '?' }}</span>
              <span class="faint">· round {{ s.roundNo ?? '?' }}</span>
              <span v-if="num(s.resultCount)" class="faint">· 返回 {{ s.resultCount }} 条</span>
              <span v-if="num(s.contentBytes)" class="faint">· {{ tgBytes(num(s.contentBytes)) }}</span>
            </p>
            <p v-if="s.query" class="s-line"><span class="faint">查：</span><i>{{ s.query }}</i></p>
            <p v-if="s.url" class="s-line url"><span class="faint">URL：</span><a :href="str(s.url)" target="_blank" rel="noreferrer noopener">{{ s.url }}</a></p>
            <p v-if="searchSeconds(s) !== null || num(s.contentChars)" class="s-line faint">
              <template v-if="searchSeconds(s) !== null">耗时 {{ ms(searchSeconds(s)) }}</template>
              <template v-if="num(s.contentChars)"> · 字符 {{ compact(num(s.contentChars)) }}</template>
            </p>
            <p v-if="s.errorMessage" class="t-err">{{ s.errorMessage }}</p>
          </li>
        </ul>
      </section>

      <!-- final error -->
      <section v-if="(log.status === 'error' || log.status === 'cancelled') && log.error" class="error-box" :class="{ cancelled: log.status === 'cancelled' }">
        <p class="sec-title">{{ log.status === 'cancelled' ? '客户端取消' : '最终错误' }}</p>
        <p class="e-sum">{{ log.errorSummary || errorSummary(log.error) }}</p>
        <details v-if="(log.errorSummary || errorSummary(log.error)) !== log.error">
          <summary>原始错误</summary>
          <pre>{{ log.error }}</pre>
        </details>
      </section>

      <!-- bodies -->
      <section class="sec">
        <div class="sec-row">
          <p class="sec-title">正文</p>
          <Segmented
            v-model="bodyTab"
            size="sm"
            :options="[
              { value: 'request', label: '请求' },
              { value: 'response', label: '响应' },
            ]"
          />
        </div>
        <p v-if="detail?.bodyPurgedAt" class="faint small">{{ bodyPurgedText(detail.bodyPurgedAt) }}</p>
        <p v-else-if="!detail?.requestBodyAvailable && bodyTab === 'request'" class="faint small">这条请求没有保存请求正文。</p>
        <p v-else-if="!detail?.responseBodyAvailable && bodyTab === 'response'" class="faint small">这条请求没有保存响应正文。</p>
        <BodyViewer v-else :id="id" :key="bodyTab" :kind="bodyTab" />
      </section>

      <div class="related">
        <button v-if="log.requestedModel" class="lnk" @click="goLogs({ model: log.requestedModel })">同模型的请求</button>
        <button v-if="log.channelId" class="lnk" @click="goLogs({ channel: log.channelId })">同渠道的请求</button>
        <button v-if="log.apiKeyName" class="lnk" @click="goLogs({ apiKey: log.apiKeyName })">同 Key 的请求</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.detail {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.pad {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
}
.hero {
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.hero-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.when {
  flex: 1;
  font-size: 12px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.model {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 16px;
  font-weight: 600;
  min-width: 0;
}
.model .lnk {
  word-break: break-all;
}
.arrow {
  width: 15px;
  height: 15px;
  color: var(--text-3);
}
.final {
  color: var(--accent);
}
.flags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.flag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  height: 20px;
  padding: 0 7px;
  border-radius: 5px;
  font-size: 11.5px;
  color: var(--text-2);
  background: var(--fill-2);
}
.flag svg {
  width: 11px;
  height: 11px;
}
.flag b {
  font-weight: 550;
  color: var(--text);
}
.f-yellow { color: var(--yellow); background: color-mix(in srgb, var(--yellow) 14%, transparent); }
.f-orange { color: var(--orange); background: color-mix(in srgb, var(--orange) 14%, transparent); }
.f-purple { color: var(--purple); background: color-mix(in srgb, var(--purple) 14%, transparent); }
.f-blue { color: var(--blue); background: color-mix(in srgb, var(--blue) 14%, transparent); }
.observe {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 9%, transparent);
}
.o-title {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  font-weight: 550;
  word-break: break-word;
}
.o-title svg {
  width: 13px;
  height: 13px;
  margin-top: 2px;
  flex-shrink: 0;
}
.o-line {
  padding-left: 18px;
  color: var(--text-2);
  word-break: break-word;
}
.facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 14px;
  margin: 0;
}
.facts > div {
  min-width: 0;
}
.facts .wide {
  grid-column: 1 / -1;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  min-width: 0;
}
dd.ch {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.idc :deep(.t) {
  font-size: 12px;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  overflow: hidden;
}
.m {
  padding: 10px 12px;
  border-right: 0.5px solid var(--hairline);
  min-width: 0;
}
.m:last-child {
  border-right: 0;
}
.m.wide {
  grid-column: 1 / -1;
}
.k {
  font-size: 11px;
  color: var(--text-3);
}
.v {
  margin-top: 2px;
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.v.hit {
  color: var(--green);
}
.s {
  margin-top: 1px;
  font-size: 10.5px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sec {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.sec-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.sec-title {
  font-size: 12.5px;
  font-weight: 650;
}
.sec-title .faint {
  font-weight: 400;
  margin-left: 4px;
}
.sub-title {
  margin-top: 4px;
}
.small {
  font-size: 12px;
}
.box {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  font-size: 12px;
  min-width: 0;
}
.line {
  word-break: break-word;
  font-size: 11.5px;
}
.sub-line {
  padding-left: 12px;
  color: var(--text-2);
}
.kvs {
  gap: 8px;
}
.kv {
  display: grid;
  grid-template-columns: 108px minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
}
.kv .k small {
  display: block;
  font-size: 10px;
  color: var(--text-3);
}
.kv .v {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 400;
  word-break: break-all;
}
.chip {
  display: inline-flex;
  gap: 4px;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11.5px;
  color: var(--text-2);
  background: var(--fill-2);
  white-space: nowrap;
}
.chip b {
  color: var(--text);
  font-weight: 550;
}
.timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.timeline > li {
  position: relative;
  display: flex;
  gap: 10px;
  padding-bottom: 12px;
  min-width: 0;
}
.timeline > li:not(:last-child)::before {
  content: '';
  position: absolute;
  left: 4.5px;
  top: 14px;
  bottom: 0;
  width: 1px;
  background: var(--border-strong);
}
.node {
  width: 10px;
  height: 10px;
  margin-top: 4px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--tone, var(--text-3));
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--tone, var(--text-3)) 20%, transparent);
}
.timeline li.green { --tone: var(--green); }
.timeline li.red { --tone: var(--red); }
.timeline li.orange { --tone: var(--orange); }
.timeline li.blue { --tone: var(--blue); }
.t-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.t-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.t-order {
  font-size: 11.5px;
  font-weight: 650;
  color: var(--text-2);
  white-space: nowrap;
}
.t-name {
  flex: 1;
  font-weight: 550;
}
.t-meta {
  font-size: 11.5px;
  color: var(--text-2);
  word-break: break-all;
}
.t-line,
.r-line {
  font-size: 11.5px;
  color: var(--text-3);
  word-break: break-all;
}
.t-err {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--red);
  word-break: break-word;
  font-style: italic;
}
.rounds {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rounds > li {
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.r-head {
  font-size: 12px;
  font-weight: 550;
  word-break: break-all;
}
.r-mark {
  display: inline-block;
  width: 14px;
  color: var(--green);
}
.rounds > li.bad .r-mark {
  color: var(--red);
}
.searches {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.searches > li {
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  font-size: 12px;
  min-width: 0;
}
.s-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px;
}
.s-mark {
  width: 14px;
  color: var(--green);
}
.searches > li.fail .s-mark {
  color: var(--red);
}
.searches > li.wait .s-mark {
  color: var(--blue);
}
.s-line {
  margin-top: 2px;
  font-size: 11.5px;
  word-break: break-all;
}
.s-line a {
  color: var(--accent);
}
.error-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--red) 9%, transparent);
}
.error-box.cancelled {
  background: color-mix(in srgb, var(--orange) 9%, transparent);
}
.e-sum {
  font-size: 12.5px;
  color: var(--red);
  word-break: break-word;
}
.cancelled .e-sum {
  color: var(--orange);
}
.error-box summary {
  font-size: 11.5px;
  color: var(--text-3);
  cursor: pointer;
}
.error-box pre {
  margin: 6px 0 0;
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-2);
}
.lnk {
  max-width: 100%;
  text-align: left;
  border-radius: 4px;
  color: inherit;
}
.lnk:hover:not(:disabled) {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.related {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 12px;
}
.related .lnk {
  color: var(--accent);
}
@media (max-width: 480px) {
  .metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .m:nth-child(2) {
    border-right: 0;
  }
  .m:nth-child(-n + 2) {
    border-bottom: 0.5px solid var(--hairline);
  }
  .kv {
    grid-template-columns: 1fr;
    gap: 2px;
  }
}
</style>
