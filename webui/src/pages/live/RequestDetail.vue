<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowRight, Copy, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc } from '@/api/client'
import { onEvent } from '@/api/events'
import type { RequestLogDetailData } from '@/api/types'
import { channelName } from '@/stores/catalog'
import { toast } from '@/stores/ui'
import { compact, dateTime, integer, ms, relative, usd } from '@/utils/format'
import { protocolLabel, statusLabel, statusTone } from './status'
import Badge from '@/components/ui/Badge.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import BodyViewer from './BodyViewer.vue'

interface Attempt {
  attemptOrder?: number
  channelKey?: string
  model?: string
  clientVisibleModel?: string
  outcome?: string | null
  totalMs?: number | null
  firstByteMs?: number | null
  connectMs?: number | null
  attemptElapsedMs?: number | null
  proxyName?: string | null
  errorDetail?: string | null
  upstreamProtocol?: string | null
  outboundServiceTier?: string | null
}

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
watch(() => props.id, load)

const log = computed(() => detail.value?.log)
const attempts = computed(() => (detail.value?.attempts || []) as Attempt[])
const finalAttempt = computed(() => attempts.value[attempts.value.length - 1])
const firstByte = computed(() => finalAttempt.value?.firstByteMs ?? null)
const tps = computed(() => {
  const l = log.value
  const total = l?.durationMilliseconds
  if (!l || !total || !l.outputTokens) return null
  const gen = total - (firstByte.value ?? 0)
  return gen > 50 ? l.outputTokens / (gen / 1000) : null
})
const costKind = computed(() => {
  const b = log.value?.billing
  if (!b || !b.costTicks) return ''
  if (b.actualCostTicks && !b.estimatedCostTicks) return '实际'
  if (!b.actualCostTicks && b.estimatedCostTicks) return '估算'
  return '实际 + 估算'
})
const modelChanged = computed(() => log.value && log.value.finalModel && log.value.finalModel !== log.value.requestedModel)

function outcomeTone(o: string | null | undefined): 'green' | 'red' | 'orange' | 'neutral' {
  if (o === 'success') return 'green'
  if (!o) return 'neutral'
  if (o.includes('cancel')) return 'orange'
  return 'red'
}

async function copyId(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.id)
    toast('请求 ID 已复制', 'success')
  } catch {
    toast('复制失败', 'error')
  }
}
</script>

<template>
  <div class="detail">
    <p v-if="error" class="err"><TriangleAlert /> {{ error }}</p>
    <template v-else-if="!log">
      <div class="pad"><Skeleton h="60px" r="12px" /><Skeleton h="120px" r="12px" /><Skeleton h="200px" r="12px" /></div>
    </template>
    <template v-else>
      <section class="hero">
        <div class="hero-top">
          <Badge :tone="statusTone(log.status)" dot>{{ statusLabel(log.status) }}</Badge>
          <span class="when">{{ dateTime(log.createdAt) }} · {{ relative(log.createdAt) }}</span>
          <button class="copy" title="复制请求 ID" @click="copyId"><Copy /></button>
        </div>
        <div class="model">
          <span class="mono">{{ log.requestedModel || '—' }}</span>
          <template v-if="modelChanged">
            <ArrowRight class="arrow" />
            <span class="mono final">{{ log.finalModel }}</span>
          </template>
        </div>
        <dl class="facts">
          <div><dt>渠道</dt><dd class="truncate">{{ channelName(log.channelId) }}</dd></div>
          <div><dt>API Key</dt><dd>{{ log.apiKeyName || '—' }}</dd></div>
          <div><dt>协议</dt><dd>{{ protocolLabel(log.protocol) }}<span v-if="log.transport" class="faint"> · {{ log.transport }}</span></dd></div>
          <div><dt>重试</dt><dd>{{ log.retryCount ? `${log.retryCount} 次` : '无' }}</dd></div>
        </dl>
      </section>

      <section class="metrics">
        <div class="m"><p class="k">总耗时</p><p class="v">{{ ms(log.durationMilliseconds) }}</p></div>
        <div class="m"><p class="k">首字</p><p class="v">{{ ms(firstByte) }}</p></div>
        <div class="m"><p class="k">输出速度</p><p class="v">{{ tps ? `${tps.toFixed(1)}/s` : '—' }}</p></div>
        <div class="m"><p class="k">输入 Token</p><p class="v">{{ integer(log.inputTokens) }}</p></div>
        <div class="m"><p class="k">输出 Token</p><p class="v">{{ integer(log.outputTokens) }}</p></div>
        <div class="m">
          <p class="k">费用 <span v-if="costKind" class="faint">{{ costKind }}</span></p>
          <p class="v">{{ log.costTicks ? usd(log.costTicks) : '—' }}</p>
        </div>
      </section>

      <section v-if="log.error" class="error-box">
        <p class="sec-title">错误</p>
        <pre>{{ log.error }}</pre>
      </section>

      <section class="sec">
        <p class="sec-title">调用过程 <span class="faint">{{ attempts.length }} 次尝试</span></p>
        <ol class="timeline">
          <li v-for="(a, i) in attempts" :key="i" :class="outcomeTone(a.outcome)">
            <span class="node" />
            <div class="t-body">
              <div class="t-head">
                <span class="t-name truncate">{{ channelName(a.channelKey) }}</span>
                <Badge :tone="outcomeTone(a.outcome)">{{ a.outcome === 'success' ? '成功' : a.outcome || '进行中' }}</Badge>
              </div>
              <p class="t-meta">
                <span class="mono">{{ a.model }}</span>
                <span v-if="a.firstByteMs != null"> · 首字 {{ ms(a.firstByteMs) }}</span>
                <span v-if="a.totalMs != null"> · 共 {{ ms(a.totalMs) }}</span>
                <span v-if="a.proxyName"> · 代理 {{ a.proxyName }}</span>
              </p>
              <p v-if="a.errorDetail" class="t-err">{{ a.errorDetail }}</p>
            </div>
          </li>
          <li v-if="!attempts.length" class="neutral">
            <span class="node" />
            <div class="t-body"><p class="t-meta">尚未分配渠道</p></div>
          </li>
        </ol>
        <p v-if="detail?.localWebRounds?.length" class="faint small">联网工具 {{ detail.localWebRounds.length }} 轮</p>
      </section>

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
        <p v-if="!detail?.requestBodyAvailable && bodyTab === 'request'" class="faint small">这条请求没有保存请求正文。</p>
        <BodyViewer v-else :id="id" :key="bodyTab" :kind="bodyTab" />
      </section>
      <p class="foot faint mono">{{ compact(log.inputTokens + log.outputTokens) }} tokens · {{ id }}</p>
    </template>
  </div>
</template>

<style scoped>
.detail {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 16px;
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
  gap: 10px;
}
.hero-top {
  display: flex;
  align-items: center;
  gap: 8px;
}
.when {
  flex: 1;
  font-size: 12px;
  color: var(--text-3);
}
.copy {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  color: var(--text-3);
}
.copy:hover {
  background: var(--fill-2);
  color: var(--text);
}
.copy svg {
  width: 13px;
  height: 13px;
}
.model {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 16px;
  font-weight: 600;
}
.arrow {
  width: 15px;
  height: 15px;
  color: var(--text-3);
}
.final {
  color: var(--accent);
}
.facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
}
.facts div {
  min-width: 0;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 1px 0 0;
  font-size: 12.5px;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  overflow: hidden;
}
.m {
  padding: 10px 12px;
  border-right: 0.5px solid var(--hairline);
  border-bottom: 0.5px solid var(--hairline);
}
.m:nth-child(3n) {
  border-right: 0;
}
.m:nth-child(n + 4) {
  border-bottom: 0;
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
.error-box pre {
  margin: 6px 0 0;
  padding: 10px;
  border-radius: 8px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--red);
  background: color-mix(in srgb, var(--red) 9%, transparent);
}
.sec {
  display: flex;
  flex-direction: column;
  gap: 10px;
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
.small {
  font-size: 12px;
}
.timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.timeline li {
  position: relative;
  display: flex;
  gap: 10px;
  padding-bottom: 12px;
}
.timeline li:not(:last-child)::before {
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
.t-body {
  flex: 1;
  min-width: 0;
}
.t-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.t-name {
  flex: 1;
  font-weight: 550;
}
.t-meta {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-2);
}
.t-err {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--red);
  word-break: break-word;
}
.foot {
  font-size: 10.5px;
  text-align: center;
  word-break: break-all;
}
</style>
