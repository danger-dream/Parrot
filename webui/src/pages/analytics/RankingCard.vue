<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, ListFilter, Trophy } from '@lucide/vue'
import type { Schemas } from '@/api/client'
import { channelKind, compact, integer, ms, providerOf, usd } from '@/utils/format'
import Card from '@/components/ui/Card.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import ModelChannels from './ModelChannels.vue'
import { cacheShare, promptOf } from '@/pages/overview/metrics'
import {
  cacheRate,
  entityName,
  logsFor,
  openEntity,
  pct,
  PERIOD_LONG,
  periodStart,
  successRate,
  tps,
  UNKNOWN_KEYS,
  type Dim,
  type Focus,
  type Period,
} from './metrics'

type Row = Schemas['StatsBreakdownData']
type ColKey =
  | 'name'
  | 'total'
  | 'success'
  | 'input'
  | 'output'
  | 'cacheShare'
  | 'cacheRead'
  | 'cacheWrite'
  | 'cache'
  | 'actual'
  | 'estimated'
  | 'latency'
  | 'tps'

const props = defineProps<{ rows: Row[]; dim: Dim; period: Period; focus: Focus; loading?: boolean; truncated?: boolean; error?: string }>()

const FOCUS_SORT: Record<Focus, ColKey> = { requests: 'total', cost: 'actual', latency: 'latency', cache: 'cache' }

interface Col {
  key: ColKey
  label: string
  title?: string
  get: (r: Row) => number | string | null
  fmt: (r: Row) => string
}

const COLS: Col[] = [
  { key: 'total', label: '请求', get: (r) => r.metrics.total, fmt: (r) => integer(r.metrics.total) },
  { key: 'success', label: '成功率', get: (r) => successRate(r.metrics), fmt: (r) => pct(successRate(r.metrics)) },
  { key: 'input', label: '读入 ↑', title: '读入 Token（含缓存读写，与其他页面一致）', get: (r) => promptOf(r.metrics), fmt: (r) => compact(promptOf(r.metrics)) },
  { key: 'output', label: '写出 ↓', title: '输出 Token', get: (r) => r.metrics.outputTokens, fmt: (r) => compact(r.metrics.outputTokens) },
  { key: 'cacheShare', label: '缓存占比', title: '缓存读取占读入的比例', get: (r) => cacheShare(r.metrics), fmt: (r) => pct(cacheShare(r.metrics)) },
  { key: 'cacheRead', label: '缓存读', title: '缓存读取 Token', get: (r) => r.metrics.cacheReadTokens, fmt: (r) => compact(r.metrics.cacheReadTokens) },
  { key: 'cacheWrite', label: '缓存写', title: '缓存写入 Token', get: (r) => r.metrics.cacheCreationTokens, fmt: (r) => compact(r.metrics.cacheCreationTokens) },
  { key: 'cache', label: '命中率', title: '成功请求中命中缓存的比例', get: (r) => cacheRate(r.metrics), fmt: (r) => pct(cacheRate(r.metrics)) },
  { key: 'actual', label: '实际费用', title: '按上游返回的真实账单结算', get: (r) => r.metrics.actualCostTicks, fmt: (r) => usd(r.metrics.actualCostTicks) },
  { key: 'estimated', label: '估算费用', title: '按价格表估算', get: (r) => r.metrics.estimatedCostTicks, fmt: (r) => usd(r.metrics.estimatedCostTicks) },
  { key: 'latency', label: '首字', title: '平均首字耗时', get: (r) => r.metrics.averageFirstTokenMilliseconds ?? null, fmt: (r) => ms(r.metrics.averageFirstTokenMilliseconds) },
  { key: 'tps', label: '速度', title: '平均输出速度（Token/秒）', get: (r) => r.metrics.averageTokensPerSecond ?? null, fmt: (r) => tps(r.metrics.averageTokensPerSecond) },
]

const sortKey = ref<ColKey>(FOCUS_SORT[props.focus])
const desc = ref(true)
const q = ref('')
const expanded = ref<string | null>(null)
const LIMIT = 20
const showAll = ref(false)

watch(
  () => props.focus,
  (f) => {
    sortKey.value = FOCUS_SORT[f]
    desc.value = true
  },
)
watch(
  () => props.dim,
  () => {
    expanded.value = null
    q.value = ''
    showAll.value = false
  },
)

function sortBy(k: ColKey): void {
  if (sortKey.value === k) desc.value = !desc.value
  else {
    sortKey.value = k
    desc.value = k !== 'name'
  }
}

const sorted = computed(() => {
  const s = q.value.trim().toLowerCase()
  const list = props.rows.filter((r) => !s || r.key.toLowerCase().includes(s) || entityName(props.dim, r.key).toLowerCase().includes(s))
  const col = COLS.find((c) => c.key === sortKey.value)
  const val = (r: Row): number | string | null => (sortKey.value === 'name' ? entityName(props.dim, r.key) : col ? col.get(r) : null)
  return [...list].sort((a, b) => {
    const va = val(a)
    const vb = val(b)
    // Missing values always sink to the bottom regardless of direction.
    if (va === null && vb === null) return 0
    if (va === null) return 1
    if (vb === null) return -1
    const cmp = typeof va === 'string' ? va.localeCompare(String(vb), 'zh-CN') : va - (vb as number)
    return desc.value ? -cmp : cmp
  })
})

const visibleRows = computed(() => (showAll.value || q.value ? sorted.value : sorted.value.slice(0, LIMIT)))

/** Share bar under each name: share of the column being sorted on (additive columns only). */
const shareMax = computed(() => {
  const col = COLS.find((c) => c.key === sortKey.value)
  if (!col) return 0
  return Math.max(0, ...props.rows.map((r) => Number(col.get(r) ?? 0)))
})
function share(r: Row): number {
  const col = COLS.find((c) => c.key === sortKey.value)
  if (!col || !shareMax.value) return 0
  return (Number(col.get(r) ?? 0) / shareMax.value) * 100
}

const PROVIDER: Record<string, string> = {
  claude: 'Claude',
  openai: 'OpenAI',
  xai: 'xAI',
  zhipu: '智谱',
  workbuddy: 'WorkBuddy',
  qoder: 'Qoder',
  antigravity: 'Antigravity',
  cursor: 'Cursor',
}

/** Channel rows: OAuth accounts carry their provider (several accounts share one e-mail), API channels say API. */
function kindBadge(key: string): { text: string; tone: 'orange' | 'green' } | null {
  if (props.dim !== 'channel') return null
  const k = channelKind(key)
  if (k === 'api') return { text: 'API', tone: 'green' }
  if (k !== 'oauth') return null
  const p = providerOf(key) || ''
  return { text: PROVIDER[p] || p || 'OAuth', tone: 'orange' }
}

function viewLogs(r: Row): void {
  logsFor(props.dim, r.key, { from: periodStart(props.period) })
}

const DIM_NAME: Record<Dim, string> = { model: '模型', channel: '渠道', apiKey: 'Key' }
</script>

<template>
  <Card :title="`${DIM_NAME[dim]}排行`" :subtitle="`${PERIOD_LONG[period]} · 点击表头排序，点击一行查看详情`" flush>
    <template #icon><Trophy /></template>
    <template #actions>
      <SearchField v-model="q" :placeholder="`筛选${DIM_NAME[dim]}`" width="200px" />
    </template>
    <p v-if="error" class="state">{{ error }}</p>
    <div v-else class="table" :class="`d-${dim}`">
      <div class="head">
        <button type="button" class="c-name" :class="{ on: sortKey === 'name' }" @click="sortBy('name')">
          {{ DIM_NAME[dim] }}
          <component :is="desc ? ArrowDown : ArrowUp" v-if="sortKey === 'name'" class="arr" />
        </button>
        <button
          v-for="c in COLS"
          :key="c.key"
          type="button"
          class="num"
          :class="[`c-${c.key}`, { on: sortKey === c.key }]"
          :title="c.title"
          @click="sortBy(c.key)"
        >
          <component :is="desc ? ArrowDown : ArrowUp" v-if="sortKey === c.key" class="arr" />{{ c.label }}
        </button>
        <span class="c-act" />
      </div>
      <template v-if="loading && !rows.length">
        <div v-for="i in 8" :key="i" class="skl"><Skeleton h="14px" /></div>
      </template>
      <Empty v-else-if="!sorted.length" :title="q ? '没有匹配的项' : '这段时间没有请求'" />
      <div v-else :class="{ dim: loading }">
        <template v-for="(r, i) in visibleRows" :key="r.key">
          <div
            class="row"
            :class="{ unknown: UNKNOWN_KEYS.has(r.key), open: expanded === r.key }"
            role="button"
            tabindex="0"
            @click="openEntity(dim, r.key)"
            @keydown.enter="openEntity(dim, r.key)"
          >
            <span class="c-name">
              <span class="rank mono">{{ i + 1 }}</span>
              <span class="nm">
                <span class="line">
                  <span class="truncate" :class="{ mono: dim === 'model' }" :title="r.key">{{ entityName(dim, r.key) }}</span>
                  <Badge v-if="kindBadge(r.key)" :tone="kindBadge(r.key)!.tone">{{ kindBadge(r.key)!.text }}</Badge>
                </span>
                <i class="bar"><i :style="{ width: `${share(r)}%` }" /></i>
              </span>
            </span>
            <span v-for="c in COLS" :key="c.key" class="num mono" :class="[`c-${c.key}`, { on: sortKey === c.key }]">
              <span class="lbl">{{ c.label }}</span>{{ c.fmt(r) }}
            </span>
            <span class="c-act" @click.stop>
              <Btn
                v-if="dim === 'model' && !UNKNOWN_KEYS.has(r.key)"
                variant="ghost"
                size="sm"
                icon
                :title="expanded === r.key ? '收起渠道分布' : '展开渠道分布'"
                @click="expanded = expanded === r.key ? null : r.key"
              >
                <component :is="expanded === r.key ? ChevronDown : ChevronRight" />
              </Btn>
              <Btn variant="ghost" size="sm" title="在请求记录中查看" @click="viewLogs(r)"><ListFilter /><span class="bt">查看请求</span></Btn>
            </span>
          </div>
          <ModelChannels v-if="expanded === r.key" :model="r.key" :period="period" />
        </template>
      </div>
      <div v-if="!showAll && !q && sorted.length > LIMIT" class="more">
        <Btn size="sm" variant="ghost" @click="showAll = true">显示全部 {{ sorted.length }} 项</Btn>
      </div>
      <p v-if="truncated" class="more faint">只显示前 200 项</p>
    </div>
  </Card>
</template>

<style scoped>
.table {
  container-type: inline-size;
  --cols: minmax(190px, 2.2fr) repeat(12, minmax(58px, 0.75fr)) 128px;
  border-top: 0.5px solid var(--hairline);
}
.head,
.row {
  display: grid;
  grid-template-columns: var(--cols);
  align-items: center;
  column-gap: 10px;
  padding: 0 14px;
}
.head {
  height: 32px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 0.5px solid var(--border);
}
.head button {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 100%;
  font-weight: 600;
  white-space: nowrap;
}
.head button:hover,
.head button.on {
  color: var(--text);
}
.head .num {
  justify-content: flex-end;
}
.arr {
  width: 11px;
  height: 11px;
}
.row {
  min-height: 42px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
  cursor: pointer;
  transition: background 0.15s;
}
.row:hover {
  background: var(--fill-3);
}
.row.open {
  background: var(--fill-3);
}
.row.unknown {
  cursor: default;
}
.row.unknown .nm {
  color: var(--text-3);
}
.c-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.rank {
  width: 18px;
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-3);
  text-align: right;
}
.nm {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}
.line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.bar {
  display: block;
  height: 3px;
  border-radius: 2px;
  background: var(--fill-2);
  overflow: hidden;
}
.bar i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--teal), var(--blue));
  transition: width 0.4s var(--ease-out);
}
.num {
  text-align: right;
  font-size: 12px;
  white-space: nowrap;
}
.row .num {
  color: var(--text-2);
}
.row .num.on {
  color: var(--text);
  font-weight: 600;
}
.lbl {
  display: none;
}
.c-act {
  display: flex;
  justify-content: flex-end;
  gap: 2px;
}
.skl {
  padding: 13px 14px;
  border-bottom: 0.5px solid var(--hairline);
}
.dim {
  opacity: 0.55;
  transition: opacity 0.2s;
}
.state {
  padding: 40px 16px;
  text-align: center;
  color: var(--text-3);
}
.more {
  padding: 10px 14px;
  font-size: 11.5px;
  text-align: center;
}
@container (max-width: 1240px) {
  .table {
    --cols: minmax(180px, 2.2fr) repeat(10, minmax(58px, 0.75fr)) 128px;
  }
  .table .c-cacheRead,
  .table .c-cacheWrite {
    display: none;
  }
}
@container (max-width: 1000px) {
  .table {
    --cols: minmax(170px, 2fr) repeat(8, minmax(58px, 0.8fr)) 118px;
  }
  .table .c-input,
  .table .c-output {
    display: none;
  }
}
@container (max-width: 760px) {
  .head {
    display: none;
  }
  .row {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    row-gap: 6px;
    padding: 10px 12px;
  }
  .row .c-name {
    grid-column: 1 / 4;
    grid-row: 1;
  }
  .row .c-act {
    grid-column: 4;
    grid-row: 1;
  }
  .bt {
    display: none;
  }
  .row .num {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    text-align: left;
    font-size: 12px;
  }
  .row .c-input,
  .row .c-output,
  .row .c-cacheRead,
  .row .c-cacheWrite {
    display: none;
  }
  .lbl {
    display: block;
    font-family: var(--font-sans);
    font-size: 10.5px;
    color: var(--text-3);
  }
}
</style>
