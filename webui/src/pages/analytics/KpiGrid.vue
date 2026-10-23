<script setup lang="ts">
import { computed } from 'vue'
import { Activity, CircleCheck, Coins, Database, Gauge, Layers, Repeat, Timer } from '@lucide/vue'
import { compact, integer, ms, usd } from '@/utils/format'
import Skeleton from '@/components/ui/Skeleton.vue'
import Spark from '@/components/ui/Spark.vue'
import { deltas, sparks, type Compare, type ComparePeriod, type Delta } from '@/utils/compare'
import { cacheShare, cancelledOf, promptOf } from '@/pages/overview/metrics'
import { cacheRate, pct, successRate, totalTokens, tps, type Focus, type Metric } from './metrics'

/** `compare` is this period against the one before it (absent for 累计). */
const props = defineProps<{ metrics: Metric | null; focus: Focus; loading?: boolean; compare?: Compare | null; period?: ComparePeriod | null }>()
const emit = defineEmits<{ focus: [value: Focus] }>()

interface Cell {
  key: string
  label: string
  icon: unknown
  tint: string
  value: string
  tone?: string
  lines: { k: string; v: string; tone?: string }[]
  focus?: Focus
  delta?: Delta | null
  spark?: number[]
}

const change = computed(() => deltas(props.compare || null, props.period))
const trend = computed(() => (props.compare ? sparks(props.compare) : null))

const cells = computed<Cell[]>(() => {
  const m = props.metrics
  if (!m) return []
  const d = change.value
  const t = trend.value
  const total = m.total
  const sr = successRate(m)
  const cr = cacheRate(m)
  const tokenAll = totalTokens(m)
  const readShare = m.inputTokens + m.cacheReadTokens + m.cacheCreationTokens
  return [
    {
      key: 'requests',
      delta: d.requests,
      spark: t?.requests,
      label: '请求数',
      icon: Activity,
      tint: 'var(--blue)',
      value: integer(total),
      focus: 'requests',
      lines: [
        { k: '成功', v: integer(m.successCount), tone: 'green' },
        { k: '失败', v: integer(m.errorCount), tone: m.errorCount ? 'red' : '' },
        { k: '取消', v: integer(cancelledOf(m)) },
        { k: '进行中', v: integer(m.pendingCount) },
      ],
    },
    {
      key: 'success',
      delta: d.success,
      spark: t?.success,
      label: '成功率',
      icon: CircleCheck,
      tint: 'var(--green)',
      value: pct(sr),
      tone: sr !== null && sr < 0.95 ? 'red' : '',
      lines: [
        { k: '重试次数', v: integer(m.totalRetries) },
        { k: '涉及请求', v: `${integer(m.retriedRequests)}（${pct(total ? m.retriedRequests / total : null)}）` },
      ],
    },
    {
      key: 'tokens',
      delta: d.tokens,
      spark: t?.tokens,
      label: 'Token',
      icon: Layers,
      tint: 'var(--purple)',
      value: compact(tokenAll),
      lines: [
        { k: '读入 ↑', v: compact(promptOf(m)) },
        { k: '写出 ↓', v: compact(m.outputTokens) },
        { k: '缓存读', v: `${compact(m.cacheReadTokens)} · ${pct(cacheShare(m))}` },
        { k: '缓存写', v: compact(m.cacheCreationTokens) },
      ],
    },
    {
      key: 'cache',
      delta: d.hit,
      spark: t?.hit,
      label: '缓存命中率',
      icon: Database,
      tint: 'var(--teal)',
      value: pct(cr),
      focus: 'cache',
      lines: [
        { k: '命中请求', v: `${integer(m.cacheHitRequests)} / ${integer(m.successCount)}` },
        { k: '写入请求', v: integer(m.cacheWriteRequests) },
        { k: '读缓存 Token 占比', v: pct(readShare ? m.cacheReadTokens / readShare : null) },
      ],
    },
    {
      key: 'cost',
      delta: d.cost,
      spark: t?.cost,
      label: '金额',
      icon: Coins,
      tint: 'var(--orange)',
      value: usd(m.costTicks),
      focus: 'cost',
      lines: [
        { k: '实际金额', v: `${usd(m.actualCostTicks)}（${integer(m.actualCostedSuccess)} 个请求）` },
        { k: '估算金额', v: `${usd(m.estimatedCostTicks)}（${integer(m.estimatedCostedSuccess)} 个请求）` },
        { k: '未计价', v: `${integer(m.unpricedSuccess)} 个请求`, tone: m.unpricedSuccess ? 'orange' : '' },
      ],
    },
    {
      key: 'latency',
      delta: d.firstToken,
      spark: t?.firstToken,
      label: '首字耗时',
      icon: Timer,
      tint: 'var(--pink)',
      value: ms(m.averageFirstTokenMilliseconds),
      focus: 'latency',
      lines: [
        { k: '连接', v: ms(m.averageConnectMilliseconds) },
        { k: '总耗时', v: ms(m.averageTotalMilliseconds) },
      ],
    },
    {
      key: 'tps',
      delta: d.tps,
      spark: t?.tps,
      label: '输出速度',
      icon: Gauge,
      tint: 'var(--indigo)',
      value: tps(m.averageTokensPerSecond),
      lines: [
        { k: '峰值', v: tps(m.maximumTokensPerSecond) },
        { k: '最低', v: tps(m.minimumTokensPerSecond) },
      ],
    },
    {
      key: 'affinity',
      delta: d.affinity,
      spark: t?.affinity,
      label: '会话亲和',
      icon: Repeat,
      tint: 'var(--yellow)',
      value: pct(total ? m.affinityHits / total : null),
      lines: [{ k: '命中请求', v: `${integer(m.affinityHits)} / ${integer(total)}` }],
    },
  ]
})

function onClick(c: Cell): void {
  if (c.focus) emit('focus', c.focus)
}
</script>

<template>
  <div v-if="loading && !metrics" class="grid">
    <Skeleton v-for="i in 8" :key="i" h="118px" r="14px" />
  </div>
  <div v-else class="grid" :class="{ dim: loading }">
    <component
      :is="c.focus ? 'button' : 'div'"
      v-for="c in cells"
      :key="c.key"
      type="button"
      class="kpi"
      :class="{ on: c.focus === focus, clickable: Boolean(c.focus) }"
      :style="{ '--tint': c.tint }"
      :title="c.focus ? `以「${c.label}」为重点查看趋势与排行` : undefined"
      @click="onClick(c)"
    >
      <p class="head">
        <component :is="c.icon" class="ic" /><span class="lbl">{{ c.label }}</span>
        <span v-if="c.delta" class="delta" :class="c.delta.tone" :title="c.delta.title">{{ c.delta.text }}</span>
      </p>
      <p class="v" :class="c.tone ? `c-${c.tone}` : ''">{{ c.value }}</p>
      <dl class="lines">
        <div v-for="l in c.lines" :key="l.k">
          <dt>{{ l.k }}</dt>
          <dd :class="l.tone ? `c-${l.tone}` : ''" :title="l.v">{{ l.v }}</dd>
        </div>
      </dl>
      <Spark v-if="c.spark && c.spark.length > 1" class="trend" :values="c.spark" :color="c.tint" :height="26" />
    </component>
  </div>
</template>

<style scoped>
/* Eight cards: two even rows of four, like the overview and request log. */
.grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  align-items: stretch;
  transition: opacity 0.2s;
}
.grid.dim {
  opacity: 0.55;
}
.kpi {
  display: flex;
  flex-direction: column;
  gap: 4px;
  height: 100%;
  padding: 13px 15px 14px;
  text-align: left;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  transition: box-shadow 0.2s, background 0.2s;
}
.kpi.clickable:hover {
  background: var(--surface-hover);
}
.kpi.on {
  box-shadow: var(--card-highlight), inset 0 0 0 1.5px color-mix(in srgb, var(--tint) 70%, transparent);
  background: color-mix(in srgb, var(--tint) 7%, var(--surface));
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 6px;
  font-size: 12px;
  color: var(--text-2);
}
.ic {
  width: 14px;
  height: 14px;
  color: var(--tint);
}
.lbl {
  flex: 1 0 auto;
}
.delta {
  flex-shrink: 0;
  margin-left: auto;
  font-size: 11.5px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--text-3);
}
.delta.good {
  color: var(--green);
}
.delta.bad {
  color: var(--red);
}
.trend {
  margin-top: auto;
  padding-top: 6px;
}
.v {
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.lines {
  margin: 2px 0 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.lines div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
}
.lines dt {
  color: var(--text-3);
  white-space: nowrap;
}
.lines dd {
  margin: 0;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--text-2);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 767px) {
  .grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .kpi {
    padding: 10px 11px 11px;
  }
  .v {
    font-size: 19px;
  }
  .lines div {
    font-size: 11px;
  }
}
</style>
