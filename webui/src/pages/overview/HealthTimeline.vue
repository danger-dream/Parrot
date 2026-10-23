<script setup lang="ts">
import { computed } from 'vue'
import type { TimePoint } from '@/api/types'
import Card from '@/components/ui/Card.vue'
import { goLogs } from '@/links'
import { integer, pct } from '@/utils/format'

const props = defineProps<{ points: TimePoint[]; ready: boolean }>()

type Cell = { key: string; state: 'none' | 'ok' | 'warn' | 'bad' | 'future'; from: number; total: number; success: number; errors: number }

const grid = computed(() => {
  const now = Date.now()
  const day = new Date()
  day.setHours(0, 0, 0, 0)
  const start = day.getTime()
  const byStart = new Map<number, TimePoint>()
  for (const p of props.points) byStart.set(new Date(p.t).getTime(), p)
  const rows: Cell[][] = []
  for (let r = 0; r < 6; r++) {
    const row: Cell[] = []
    for (let h = 0; h < 24; h++) {
      const from = start + h * 3600_000 + r * 600_000
      const p = byStart.get(from)
      const total = p?.total ?? 0
      const success = p?.successCount ?? 0
      const errors = p?.errorCount ?? 0
      let state: Cell['state'] = 'none'
      if (from > now) state = 'future'
      else if (total) state = errors === 0 ? 'ok' : errors / total < 0.05 ? 'warn' : 'bad'
      row.push({ key: `${r}-${h}`, state, from, total, success, errors })
    }
    rows.push(row)
  }
  return rows
})

const totals = computed(() => {
  let total = 0
  let success = 0
  let errors = 0
  for (const row of grid.value) for (const c of row) (total += c.total), (success += c.success), (errors += c.errors)
  // Same success rate as every other card: successes over all requests (cancelled and running are not successes).
  return { total, errors, ok: success, other: total - success - errors, rate: total ? success / total : null }
})

function label(c: Cell): string {
  const d = new Date(c.from)
  const t = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (c.state === 'future') return `${t} · 未到`
  if (!c.total) return `${t} · 无请求`
  return `${t} · ${integer(c.total)} 次 · 失败 ${integer(c.errors)}`
}

function open(c: Cell): void {
  if (!c.total) return
  goLogs({ status: c.errors ? 'error' : undefined, from: c.from, to: c.from + 600_000 })
}
</script>

<template>
  <Card title="请求健康时间线（今日）" subtitle="按 10 分钟聚合，点格子查看该时段的请求">
    <template #actions>
      <span v-if="totals.rate != null" class="rate" :class="totals.rate >= 0.99 ? 'g' : totals.rate >= 0.95 ? 'o' : 'r'">
        {{ pct(totals.rate) }}
      </span>
    </template>
    <div class="wrap">
      <div class="counts">
        <span><i class="ok" />成功 {{ integer(totals.ok) }}</span>
        <span><i class="bad" />失败 {{ integer(totals.errors) }}</span>
        <span v-if="totals.other" class="faint">取消 / 进行中 {{ integer(totals.other) }}</span>
      </div>
      <div class="hm">
        <div class="hours">
          <span v-for="h in [0, 6, 12, 18, 23]" :key="h" :style="{ gridColumn: h + 2 }">{{ String(h).padStart(2, '0') }}</span>
        </div>
        <div v-for="(row, r) in grid" :key="r" class="hrow">
          <span class="min">{{ r * 10 }}</span>
          <button
            v-for="c in row"
            :key="c.key"
            type="button"
            class="cell"
            :class="ready ? c.state : 'none'"
            :title="label(c)"
            :disabled="!c.total"
            @click="open(c)"
          />
        </div>
      </div>
      <div class="legend">
        <span><i class="none" />无请求</span>
        <span><i class="ok" />成功</span>
        <span><i class="warn" />少量失败</span>
        <span><i class="bad" />失败较多</span>
        <span><i class="future" />未到</span>
      </div>
    </div>
  </Card>
</template>

<style scoped>
.wrap {
  height: 262px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.rate {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 650;
}
.rate.g {
  color: var(--text-green);
  background: color-mix(in srgb, var(--green) 12%, transparent);
}
.rate.o {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.rate.r {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 12%, transparent);
}
.counts {
  display: flex;
  justify-content: flex-end;
  gap: 14px;
  font-size: 12px;
  color: var(--text-2);
}
.counts span,
.legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.counts i,
.legend i {
  width: 9px;
  height: 9px;
  border-radius: 2px;
}
.hm {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.hours,
.hrow {
  display: grid;
  grid-template-columns: 22px repeat(24, minmax(0, 1fr));
  gap: 4px;
}
.hours {
  font-size: 10.5px;
  color: var(--text-3);
}
.hrow {
  flex: 1;
}
.min {
  font-size: 10.5px;
  color: var(--text-3);
  align-self: center;
}
.cell {
  border-radius: 3px;
  min-height: 14px;
}
.cell:not(:disabled):hover {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.none {
  background: var(--track);
}
.ok {
  background: var(--green);
}
.warn {
  background: var(--lime);
}
.bad {
  background: var(--red);
}
.future {
  background: transparent;
  box-shadow: inset 0 0 0 1px var(--border-strong);
}
.legend {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 11.5px;
  color: var(--text-3);
}
</style>
