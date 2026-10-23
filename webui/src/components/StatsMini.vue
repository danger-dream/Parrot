<script setup lang="ts">
import { computed } from 'vue'
import type { StatsMetricData } from '@/api/types'
import { compact, integer, ms, percent, usd } from '@/utils/format'
import { deltas, type Compare, type ComparePeriod } from '@/utils/compare'
import UsageTiles, { type UsageTile } from './UsageTiles.vue'

const props = defineProps<{ metrics: Partial<StatsMetricData> | null | undefined; label?: string; compare?: Compare | null; period?: ComparePeriod | null }>()

const cells = computed<UsageTile[]>(() => {
  const m = props.metrics
  if (!m) return []
  const total = m.total ?? 0
  const d = deltas(props.compare || null, props.period)
  return [
    { k: '请求', v: integer(total), delta: d.requests },
    { k: '成功率', v: total ? percent(m.successCount ?? 0, total) : '—', bad: Boolean(total && (m.errorCount ?? 0) / total > 0.05), delta: d.success },
    { k: '费用', v: usd(m.costTicks ?? 0), delta: d.cost },
    { k: '输入 Token', v: compact((m.inputTokens ?? 0) + (m.cacheReadTokens ?? 0) + (m.cacheCreationTokens ?? 0)), delta: d.prompt },
    { k: '输出 Token', v: compact(m.outputTokens ?? 0), delta: d.output },
    { k: '首字耗时', v: ms(m.averageFirstTokenMilliseconds ?? null), delta: d.firstToken },
  ]
})
</script>

<template>
  <div class="sm">
    <p v-if="label" class="label">{{ label }}</p>
    <UsageTiles :tiles="cells" />
  </div>
</template>

<style scoped>
.label {
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
}
</style>
