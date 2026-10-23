<script setup lang="ts">
import { computed } from 'vue'
import type { Schemas } from '@/api/client'
import { dateTime, relative } from '@/utils/format'
import Meter from '@/components/ui/Meter.vue'
import { tokens } from './meta'
import { windowName } from '@/pages/oauth/meta'

// Upstream account quota of an API channel, laid out like the Telegram channel list (summary)
// and channel detail (full): Zhipu Coding windows + 24h model/tool usage, OpenRouter key credit,
// balances, generic quota windows.

type Usage = Schemas['ProviderUsageData']
type Metric = Schemas['ProviderUsageMetricData']

const props = defineProps<{ usage: Usage; compact?: boolean }>()

const snap = computed(() => props.usage.snapshot)
const source = computed(() => snap.value?.source || props.usage.source || '')
const windows = computed(() => snap.value?.windows || [])
const balances = computed(() => snap.value?.balances || [])
const counters = computed(() => snap.value?.counters || [])

function num(v: string | number | null | undefined, compact = false): string {
  if (v === null || v === undefined || v === '') return '?'
  const n = Number(v)
  if (!Number.isFinite(n)) return String(v)
  if (compact && Math.abs(n) >= 1000) return tokens(n)
  return Number.isInteger(n) ? n.toLocaleString('en-US') : n.toLocaleString('en-US', { maximumFractionDigits: 4 })
}
function value(m: Metric | undefined): string {
  if (!m) return '—'
  return `${num(m.value)}${m.currency ? ` ${m.currency}` : ''}`
}
function item(list: Metric[], id: string, ...labels: string[]): Metric | undefined {
  return list.find((x) => x.id === id) || list.find((x) => labels.includes(x.label))
}
function pct(m: Metric): number | null {
  if (m.usedPercent !== null && m.usedPercent !== undefined) return m.usedPercent
  const used = Number(m.used)
  const total = Number(m.total)
  return Number.isFinite(used) && Number.isFinite(total) && total > 0 ? (used / total) * 100 : null
}
function windowCore(m: Metric): string {
  const p = pct(m)
  if (m.used != null && m.total != null && (m.id === 'mcp_month' || p === null)) return `已用 ${num(m.used)} / ${num(m.total)}${p !== null ? `（${p.toFixed(p < 10 ? 1 : 0)}%）` : ''}`
  if (p !== null) return `已用 ${p.toFixed(p < 10 ? 1 : 0)}%`
  return m.status || '已获取'
}
function resetText(m: Metric): string {
  const at = m.resetAt || m.endAt
  if (at) return props.compact ? `${relative(at)}重置` : `${dateTime(at)} 重置（${relative(at)}）`
  if (m.resetInSeconds) return `${relative(Date.now() + m.resetInSeconds * 1000)}重置`
  return ''
}

interface Row {
  label: string
  text: string
  pct?: number | null
  sub?: string
}

const zhipuWindows = computed<Metric[]>(() =>
  [
    item(windows.value, 'tokens_5h', '5 小时额度'),
    item(windows.value, 'tokens_7d', '7 天额度'),
    item(windows.value, 'mcp_month', '月 MCP 额度'),
  ].filter((x): x is Metric => Boolean(x)),
)

const rows = computed<Row[]>(() => {
  if (!snap.value) return []
  const s = source.value
  const out: Row[] = []
  if (s === 'zhipu-coding') {
    if (!snap.value.version) return []
    for (const m of zhipuWindows.value) out.push({ label: windowName(m.label || ''), text: windowCore(m), pct: pct(m), sub: resetText(m) })
  } else if (s === 'openrouter') {
    const remaining = item(balances.value, 'key_remaining', 'Key 剩余额度')
    const limit = item(balances.value, 'key_limit', 'Key 额度上限')
    if (remaining) out.push({ label: 'Key 剩余额度', text: value(remaining) + (limit ? ` / ${value(limit)}` : '') })
    const usage: [string, string][] = props.compact
      ? [['usage_daily', '今日用量'], ['usage_weekly', '本周用量'], ['usage_monthly', '本月用量']]
      : [['usage_daily', '今日用量'], ['usage_weekly', '本周用量'], ['usage_monthly', '本月用量'], ['usage_total', '累计用量'], ['byok_usage', 'BYOK 用量']]
    for (const [id, label] of usage) {
      const m = item(balances.value, id, label)
      if (m) out.push({ label, text: value(m) })
    }
  } else if (balances.value.length) {
    if (props.compact) {
      const primary = item(balances.value, 'available', '可用余额') || item(balances.value, 'total', '总余额') || balances.value[0]
      const total = item(balances.value, 'total', '总余额')
      out.push({ label: primary.label || '余额', text: value(primary) + (total && total !== primary ? ` / 总额 ${value(total)}` : '') })
    } else {
      for (const m of balances.value) out.push({ label: m.label || '余额', text: value(m) })
    }
  } else {
    for (const m of props.compact ? windows.value.slice(0, 2) : windows.value) out.push({ label: windowName(m.label || '额度窗口'), text: windowCore(m), pct: pct(m), sub: resetText(m) })
  }
  return out
})

const warning = computed(() => {
  const st = props.usage.status
  if (snap.value) return st === 'partial' ? '部分数据暂未获取' : st === 'stale_error' ? '最近一次更新失败，当前显示上次数据' : ''
  return ''
})
const emptyText = computed(() => {
  if (!props.usage.supported) return '不支持查询'
  if (!snap.value) return props.usage.status === 'error' ? '上游用量获取失败' : props.usage.status === 'refreshing' ? '正在更新…' : '上游用量尚未获取'
  if (source.value === 'zhipu-coding' && !snap.value.version) return '上游额度暂无法展示'
  return rows.value.length ? '' : '上游额度已获取'
})

// Zhipu Coding: 24h model and tool usage (detail only).
const modelItems = computed(() => counters.value.filter((x) => x.group === 'model'))
const toolItems = computed(() => counters.value.filter((x) => x.group === 'tool' && Number(x.value) !== 0).slice(0, 9))
const modelCalls = computed(() => item(modelItems.value, 'model_calls', '模型调用总数'))
const trendTokens = computed(() => item(modelItems.value, 'trend_tokens', 'Token 用量合计'))
const distribution = computed(() => {
  const dist = modelItems.value.filter((x) => x.kind === 'distribution' || String(x.label).startsWith('模型 ·')).slice(0, 8)
  const denom = dist[0]?.distributionTotal != null ? Number(dist[0].distributionTotal) : dist.reduce((s, x) => s + Number(x.value || 0), 0)
  if (!dist.length || !denom) return []
  return dist.map((x) => ({ label: String(x.label).replace(/^模型 · /, ''), pct: (Number(x.value) / denom) * 100 }))
})
const notices = computed(() => (snap.value?.notices || []).filter((n) => n !== 'model_scope_mismatch' && !String(n).startsWith('额度重置:')))
const scopeMismatch = computed(() => (snap.value?.notices || []).includes('model_scope_mismatch'))
</script>

<template>
  <!-- list card: the same lines the bot prints under each channel -->
  <div v-if="compact" class="ub compact">
    <p v-if="warning" class="warn">{{ warning }}</p>
    <p v-if="emptyText" class="empty" :class="{ bad: usage.status === 'error' }">{{ emptyText }}</p>
    <div v-for="(r, i) in rows" :key="i" class="row">
      <div class="line">
        <span class="lbl">{{ r.label }}</span>
        <span class="val num">{{ r.text }}</span>
      </div>
      <Meter v-if="r.pct != null" :value="r.pct" />
      <p v-if="r.sub" class="sub">{{ r.sub }}</p>
    </div>
  </div>

  <div v-else class="ub">
    <p v-if="!usage.supported" class="empty">当前服务商或方案暂不支持只读查询上游额度。</p>
    <template v-else>
      <p v-if="warning" class="warn">{{ warning }}</p>
      <p v-if="emptyText" class="empty" :class="{ bad: usage.status === 'error' }">{{ emptyText }}</p>
      <div v-for="(r, i) in rows" :key="i" class="row">
        <div class="line">
          <span class="lbl">{{ r.label }}</span>
          <span class="val num">{{ r.text }}</span>
        </div>
        <Meter v-if="r.pct != null" :value="r.pct" />
        <p v-if="r.sub" class="sub">{{ r.sub }}</p>
      </div>
      <template v-if="source === 'zhipu-coding' && modelItems.length">
        <p class="sec">近 24 小时模型用量</p>
        <div v-if="modelCalls" class="line"><span class="lbl">模型调用</span><span class="val num">{{ num(modelCalls.value) }} 次</span></div>
        <div v-if="trendTokens" class="line"><span class="lbl">Token 用量</span><span class="val num">{{ num(trendTokens.value, true) }}</span></div>
        <div v-if="distribution.length" class="dist">
          <span v-for="d in distribution" :key="d.label" class="chip">{{ d.label }} <b class="num">{{ d.pct.toFixed(1) }}%</b></span>
        </div>
        <p v-if="scopeMismatch" class="sub">上游趋势总量与模型分项为不同统计口径，未直接相加。</p>
      </template>
      <template v-if="source === 'zhipu-coding' && toolItems.length">
        <p class="sec">近 24 小时工具调用</p>
        <div v-for="t in toolItems" :key="t.label" class="line"><span class="lbl">{{ t.label }}</span><span class="val num">{{ num(t.value) }} 次</span></div>
      </template>
      <p v-for="(n, i) in notices" :key="'n' + i" class="sub">{{ n }}</p>
      <p v-if="usage.fetchedAt" class="sub">更新于 {{ dateTime(usage.fetchedAt) }}（{{ relative(usage.fetchedAt) }}）</p>
      <p v-if="usage.error" class="err">错误：{{ usage.error }}<template v-if="usage.errorAt">（{{ relative(usage.errorAt) }}）</template></p>
    </template>
  </div>
</template>

<style scoped>
.ub {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 12.5px;
  min-width: 0;
}
.ub.compact {
  gap: 5px;
  font-size: 12px;
}
.row {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.line {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}
.lbl {
  color: var(--text-2);
  min-width: 0;
}
.val {
  text-align: right;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.sub {
  font-size: 11px;
  color: var(--text-3);
}
.sec {
  margin-top: 4px;
  font-size: 11.5px;
  font-weight: 650;
  color: var(--text-2);
}
.dist {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.chip {
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 11px;
  background: var(--fill-2);
}
.empty {
  color: var(--text-3);
}
.empty.bad,
.warn {
  color: var(--orange);
}
.err {
  font-size: 12px;
  color: var(--red);
  word-break: break-word;
}
</style>
