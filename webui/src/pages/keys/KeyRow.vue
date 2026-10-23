<script setup lang="ts">
import { computed, ref } from 'vue'
import { ChevronDown, ChevronRight, DatabaseZap, Download, GripVertical, Upload } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { compact, dateTime, integer, pct, tps, usd } from '@/utils/format'
import OutcomeStrip from '@/components/ui/OutcomeStrip.vue'
import type { OutcomeItem } from '@/utils/outcomes'
import { openModel } from '@/links'
import { toUsage } from '@/pages/channels/meta'
import KeySecret from './KeySecret.vue'
import { keySecret, limitBrief, type ApiKey } from './meta'

const props = defineProps<{
  k: ApiKey
  live?: { inFlight: number; waiting: number } | null
  history?: number | null
  outcome?: OutcomeItem | null
  outcomeFrom?: string | null
  lastAt?: string | null
  selected?: boolean
  dragging?: boolean
  draggable?: boolean
}>()
const emit = defineEmits<{ open: []; dragstart: [e: PointerEvent] }>()

const inFlight = computed(() => props.live?.inFlight ?? props.k.limiter.inFlight)
const waiting = computed(() => props.live?.waiting ?? props.k.limiter.waiting)
const status = computed(() => {
  if (!props.k.enabled) return { text: '已停用', tone: 'red' }
  if (inFlight.value || waiting.value) return { text: '使用中', tone: 'blue' }
  if (props.k.monthStats.total) return { text: '活跃', tone: 'green' }
  return { text: props.history ? `闲置（历史 ${integer(props.history)} 次）` : '闲置', tone: 'neutral' }
})
const usage = computed(() => toUsage(props.k.monthStats))
const perms = computed(() => {
  const k = props.k
  const out: string[] = [k.allowedModels.length ? `${k.allowedModels.length} 个模型` : '全部模型']
  if ((k.allowedChannels || []).length && k.channelBindingEnabled) out.push(`绑定 ${k.allowedChannels!.length} 个渠道`)
  if (k.allowImages) out.push('图片')
  if (k.allowVideos) out.push('视频')
  if (k.allowMcp) out.push('MCP')
  return out
})

const open = ref(false)
const byModel = ref<Schemas['ApiKeyModelUsageData'][] | null>(null)
async function toggle(): Promise<void> {
  open.value = !open.value
  if (open.value && !byModel.value) {
    try {
      const r = await api.get<Schemas['ApiKeyStatsData']>(`/api-keys/${enc(props.k.keyId)}/stats`)
      byModel.value = [...(r.data.byModel || [])].sort((a, b) => b.usage.total - a.usage.total)
    } catch {
      byModel.value = []
    }
  }
}
</script>

<template>
  <div class="kwrap" :class="{ selected, off: !k.enabled, dragging, open }" :data-drag-id="k.keyId">
    <div class="krow" role="button" tabindex="0" @click="toggle" @keydown.enter="toggle">
      <button
        type="button"
        class="grip"
        :class="{ disabled: !draggable }"
        :title="draggable ? '拖动调整顺序' : '清除搜索和筛选后可以拖动排序'"
        @click.stop
        @pointerdown="draggable && emit('dragstart', $event)"
      >
        <GripVertical />
      </button>
      <span class="chev"><ChevronDown v-if="open" /><ChevronRight v-else /></span>

      <div class="c-key">
        <p class="name mono truncate">{{ k.name }}</p>
        <KeySecret :secret="keySecret(k)" :masked="k.maskedHint" />
      </div>

      <div class="c-state">
        <span class="badge" :class="`b-${status.tone}`">{{ status.text }}</span>
        <p class="sub truncate">{{ perms.join(' · ') }}</p>
      </div>

      <div class="c-num">
        <b class="num">{{ integer(usage?.total ?? 0) }}</b>
        <p class="sub"><span class="ok">{{ integer(usage?.success ?? 0) }}</span> / <span :class="usage?.error ? 'bad' : ''">{{ integer(usage?.error ?? 0) }}</span></p>
      </div>

      <div class="c-num">
        <b class="num" :class="usage?.successRate == null ? '' : usage.successRate >= 0.99 ? 'ok' : usage.successRate >= 0.9 ? 'warn' : 'bad'">
          {{ pct(usage?.successRate) }}
        </b>
      </div>

      <div class="c-num">
        <b class="num">{{ compact((usage?.prompt ?? 0) + (usage?.output ?? 0)) }}</b>
        <p class="sub num">↑{{ compact(usage?.prompt ?? 0) }} ↓{{ compact(usage?.output ?? 0) }}</p>
      </div>

      <div class="c-num">
        <b class="num cost">{{ usd(usage?.costTicks ?? 0) }}</b>
        <p class="sub">缓存占比 {{ pct(usage?.cacheReadRatio) }}</p>
      </div>

      <div class="c-recent">
        <OutcomeStrip :item="outcome" :from="outcomeFrom" :width="3" :gap="1.5" :height="18" />
        <p class="sub num">{{ lastAt ? dateTime(lastAt).slice(0, 11) : '24 小时内无请求' }}</p>
      </div>

      <div class="c-act" @click.stop>
        <button class="detail" @click="emit('open')">详情<ChevronRight /></button>
      </div>
    </div>

    <div v-if="open" class="expand">
      <section class="box">
        <p class="bt">Token 结构（本月）</p>
        <div class="tiles">
          <div class="tile t-in"><Download /><span>读入</span><b class="num">{{ compact(usage?.prompt ?? 0) }}</b></div>
          <div class="tile t-out"><Upload /><span>输出</span><b class="num">{{ compact(usage?.output ?? 0) }}</b></div>
          <div class="tile t-cache"><DatabaseZap /><span>缓存读</span><b class="num">{{ compact(usage?.cacheRead ?? 0) }}</b></div>
        </div>
        <p class="lim">限流 {{ limitBrief(k.limiter, live) }}</p>
        <p v-if="usage?.avgTps" class="lim">速度 平均 {{ tps(usage.avgTps) }}<template v-if="usage.maxTps"> · 峰值 {{ tps(usage.maxTps) }}</template></p>
      </section>
      <section class="box models">
        <p class="bt">模型使用（本月）</p>
        <table v-if="byModel && byModel.length">
          <thead>
            <tr><th class="l">模型</th><th>调用</th><th>成功率</th><th>读入</th><th>写出</th><th>缓存占比</th><th>金额</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in byModel.slice(0, 6)" :key="m.model" @click="openModel(m.model)">
              <td class="l mono">{{ m.model }}</td>
              <td class="num">{{ integer(m.usage.total) }}</td>
              <td class="num" :class="(m.usage.successRate ?? 1) < 0.95 ? 'bad' : 'ok'">{{ pct(m.usage.successRate) }}</td>
              <td class="num">{{ compact(m.usage.promptTokens) }}</td>
              <td class="num">{{ compact(m.usage.outputTokens) }}</td>
              <td class="num">{{ pct(m.usage.cacheReadRatio) }}</td>
              <td class="num">{{ usd(m.usage.costTicks) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="sub">{{ byModel ? '本月还没有调用' : '加载中…' }}</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.kwrap {
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.kwrap:hover {
  box-shadow: var(--shadow-md);
}
.kwrap.selected {
  border-color: var(--accent);
}
.kwrap.dragging {
  opacity: 0.55;
}
.kwrap.off .c-key .name {
  opacity: 0.6;
}
.krow {
  display: grid;
  grid-template-columns: var(--cols);
  align-items: center;
  column-gap: 14px;
  padding: 12px 16px 12px 8px;
  cursor: pointer;
}
.krow > * {
  min-width: 0;
}
.grip {
  display: grid;
  place-items: center;
  color: var(--text-3);
  cursor: grab;
  touch-action: none;
}
.grip.disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.grip svg,
.chev svg {
  width: 15px;
  height: 15px;
}
.chev {
  display: grid;
  place-items: center;
  color: var(--text-3);
}
.c-key {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.name {
  font-size: 13.5px;
  font-weight: 650;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.c-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.badge {
  max-width: 100%;
  padding: 2px 9px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.b-green {
  color: var(--text-green);
  background: color-mix(in srgb, var(--green) 11%, transparent);
}
.b-blue {
  color: var(--accent);
  background: var(--accent-soft);
}
.b-red {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
}
.b-neutral {
  color: var(--text-2);
  background: var(--fill-2);
}
.c-num {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 13.5px;
}
.c-num b {
  font-weight: 650;
}
.ok {
  color: var(--text-green);
}
.warn {
  color: var(--text-orange);
}
.bad {
  color: var(--text-red);
}
.cost {
  color: var(--text);
}
.c-recent {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.c-act {
  display: flex;
  justify-content: flex-end;
  cursor: default;
}
.detail {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 30px;
  padding: 0 8px 0 10px;
  border-radius: 8px;
  font-size: 12.5px;
  font-weight: 600;
  white-space: nowrap;
  color: var(--accent);
  background: var(--accent-soft);
}
.detail svg {
  width: 14px;
  height: 14px;
}
.expand {
  display: grid;
  grid-template-columns: minmax(260px, 0.8fr) minmax(0, 2fr);
  gap: 12px;
  padding: 0 16px 16px 50px;
}
.box {
  padding: 14px;
  border-radius: var(--radius-lg);
  background: var(--surface-sunken);
  border: 0.5px solid var(--border);
}
.bt {
  margin-bottom: 10px;
  font-size: 13px;
  font-weight: 650;
}
.tiles {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tile {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12.5px;
}
.tile svg {
  width: 14px;
  height: 14px;
}
.tile b {
  margin-left: auto;
  font-size: 14px;
}
.t-in {
  color: var(--text-green);
  background: color-mix(in srgb, var(--green) 9%, transparent);
}
.t-out {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 10%, transparent);
}
.t-cache {
  color: var(--cyan);
  background: color-mix(in srgb, var(--cyan) 10%, transparent);
}
.tile b {
  color: var(--text);
}
.lim {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-2);
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
th {
  padding: 6px 8px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  text-align: right;
}
td {
  padding: 8px;
  text-align: right;
  border-top: 1px solid var(--hairline);
}
tbody tr {
  cursor: pointer;
}
tbody tr:hover td {
  background: var(--surface-hover);
}
.l {
  text-align: left;
}
@media (max-width: 1023px) {
  .expand {
    grid-template-columns: minmax(0, 1fr);
    padding-left: 16px;
  }
  .models {
    overflow-x: auto;
  }
}
</style>
