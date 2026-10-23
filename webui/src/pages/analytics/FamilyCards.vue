<script setup lang="ts">
import { computed } from 'vue'
import { Shapes } from '@lucide/vue'
import { compact, integer, ms, pctValue, usd } from '@/utils/format'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Meter from '@/components/ui/Meter.vue'
import { cacheShare, promptOf } from '@/pages/overview/metrics'
import { cacheRate, FAMILY_HINT, familyLabel, pct, successRate, totalTokens, tps, type Metric } from './metrics'

const props = defineProps<{ families: Record<string, Metric> | null; overall: Metric | null; loading?: boolean }>()

const ORDER = ['anthropic', 'openai']

const rows = computed(() => {
  const f = props.families || {}
  const keys = Object.keys(f).sort((a, b) => {
    const ia = ORDER.indexOf(a)
    const ib = ORDER.indexOf(b)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || f[b].total - f[a].total
  })
  const all = props.overall?.total || keys.reduce((s, k) => s + f[k].total, 0)
  return keys.map((k) => ({ key: k, m: f[k], share: all ? (f[k].total / all) * 100 : 0 }))
})
</script>

<template>
  <Card title="按家族汇总" subtitle="按上游协议家族分别统计，与 Telegram「统计汇总」的分段一致">
    <template #icon><Shapes /></template>
    <div v-if="loading && !families" class="grid"><Skeleton v-for="i in 2" :key="i" h="150px" r="10px" /></div>
    <Empty v-else-if="!rows.length" title="这段时间没有请求" />
    <div v-else class="grid" :class="{ dim: loading }">
      <section v-for="r in rows" :key="r.key" class="fam">
        <header>
          <div class="t">
            <p class="name">{{ familyLabel(r.key) }}</p>
            <p v-if="FAMILY_HINT[r.key]" class="hint">{{ FAMILY_HINT[r.key] }}</p>
          </div>
          <div class="share">
            <span class="mono">{{ pctValue(r.share) }}</span>
            <span class="faint">请求占比</span>
          </div>
        </header>
        <Meter :value="r.share" :warn="101" :danger="101" />
        <dl class="cells">
          <div><dt>请求</dt><dd>{{ integer(r.m.total) }}</dd></div>
          <div><dt>成功率</dt><dd :class="{ 'c-red': (successRate(r.m) ?? 1) < 0.95 }">{{ pct(successRate(r.m)) }}</dd></div>
          <div><dt>失败</dt><dd :class="{ 'c-red': r.m.errorCount }">{{ integer(r.m.errorCount) }}</dd></div>
          <div><dt>Token</dt><dd>{{ compact(totalTokens(r.m)) }}</dd></div>
          <div><dt>读入 ↑ / 写出 ↓</dt><dd>{{ compact(promptOf(r.m)) }} / {{ compact(r.m.outputTokens) }}</dd></div>
          <div><dt>缓存读 / 写</dt><dd>{{ compact(r.m.cacheReadTokens) }}（{{ pct(cacheShare(r.m)) }}）/ {{ compact(r.m.cacheCreationTokens) }}</dd></div>
          <div><dt>缓存命中率</dt><dd>{{ pct(cacheRate(r.m)) }}</dd></div>
          <div><dt>实际金额</dt><dd>{{ usd(r.m.actualCostTicks) }}</dd></div>
          <div><dt>估算金额</dt><dd>{{ usd(r.m.estimatedCostTicks) }}</dd></div>
          <div><dt>首字耗时</dt><dd>{{ ms(r.m.averageFirstTokenMilliseconds) }}</dd></div>
          <div><dt>输出速度</dt><dd>{{ tps(r.m.averageTokensPerSecond) }}</dd></div>
          <div><dt>重试 / 亲和</dt><dd>{{ integer(r.m.totalRetries) }} 次 / {{ pct(r.m.total ? r.m.affinityHits / r.m.total : null) }}</dd></div>
        </dl>
      </section>
    </div>
  </Card>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
  align-items: stretch;
  transition: opacity 0.2s;
}
.grid.dim {
  opacity: 0.55;
}
.fam {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  padding: 12px 14px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.t {
  min-width: 0;
}
.name {
  font-weight: 650;
}
.hint {
  font-size: 11.5px;
  color: var(--text-3);
}
.share {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  flex-shrink: 0;
  font-size: 11px;
}
.share .mono {
  font-size: 15px;
  font-weight: 650;
}
.cells {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px 12px;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 1px 0 0;
  font-size: 12.5px;
  font-weight: 550;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
@container (max-width: 420px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .cells {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
