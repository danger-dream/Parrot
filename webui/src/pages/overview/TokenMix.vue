<script setup lang="ts">
import { computed } from 'vue'
import Card from '@/components/ui/Card.vue'
import { compact, pct } from '@/utils/format'
import type { Metrics } from './metrics'

const props = defineProps<{ m: Metrics | null; ready: boolean }>()

const parts = computed(() => {
  const m = props.m
  const rows = [
    { key: 'read', label: '缓存读', value: m?.cacheReadTokens ?? 0, color: 'var(--orange)' },
    { key: 'input', label: '输入（未缓存）', value: m?.inputTokens ?? 0, color: 'var(--blue)' },
    { key: 'write', label: '缓存写', value: m?.cacheCreationTokens ?? 0, color: 'var(--purple)' },
    { key: 'output', label: '输出', value: m?.outputTokens ?? 0, color: 'var(--green)' },
  ]
  const total = rows.reduce((a, r) => a + r.value, 0)
  return { total, rows: rows.map((r) => ({ ...r, share: total ? r.value / total : 0 })) }
})
</script>

<template>
  <Card title="Token 构成（今日）">
    <div class="wrap">
      <div class="total">
        <span>总 Token</span>
        <b class="num">{{ ready ? compact(parts.total) : '—' }}</b>
      </div>
      <div v-for="r in parts.rows" :key="r.key" class="row">
        <div class="line">
          <span class="lab"><i :style="{ background: r.color }" />{{ r.label }}</span>
          <span class="val num">{{ compact(r.value) }}</span>
          <span class="pct num">{{ pct(r.share) }}</span>
        </div>
        <div class="bar"><span :style="{ width: `${Math.max(r.share * 100, r.value ? 1 : 0)}%`, background: r.color }" /></div>
      </div>
    </div>
  </Card>
</template>

<style scoped>
.wrap {
  height: 262px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}
.total {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--hairline);
  font-size: 12px;
  color: var(--text-3);
}
.total b {
  font-size: 24px;
  font-weight: 700;
  color: var(--text);
}
.row {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.lab {
  flex: 1;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--text-2);
}
.lab i {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}
.val {
  font-weight: 650;
}
.pct {
  width: 48px;
  text-align: right;
  font-size: 12px;
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
}
</style>
