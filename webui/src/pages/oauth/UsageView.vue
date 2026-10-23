<script setup lang="ts">
import { computed } from 'vue'
import { dateTime } from '@/utils/format'
import Meter from '@/components/ui/Meter.vue'
import { localUsageLine } from './channelStats'
import { countdown, noteLabel, num, quotaLabel, remainingAmount, sectionTitle, usageValue, WINDOW_LABEL, windowName, type UsageItem, type UsageSection, type UsageWindow } from './meta'

const props = withDefaults(
  defineProps<{
    sections?: UsageSection[] | null
    /** Legacy 5h / 7d / 30d windows; used when the server sent no sections. */
    windows?: UsageWindow[] | null
    compact?: boolean
    now: number
    /** Max rows in compact mode. */
    limit?: number
  }>(),
  { limit: 4 },
)

const list = computed<UsageSection[]>(() => {
  if (props.sections?.length) return props.sections
  const items: UsageItem[] = (props.windows || [])
    .filter((w) => num(w.usedPercent) !== null)
    .map((w) => ({ label: WINDOW_LABEL[w.name] || w.name, usedPercent: w.usedPercent, resetsAt: w.resetsAt }))
  return items.length ? [{ id: 'windows', title: '用量窗口', items }] : []
})

interface Row {
  key: string
  label: string
  item: UsageItem
  pct: number | null
}
/** Compact: percent rows first (they are what the card is for), then a value row or two. */
const compactRows = computed<Row[]>(() => {
  const pctRows: Row[] = []
  const valueRows: Row[] = []
  for (const s of list.value) {
    for (const [i, it] of s.items.entries()) {
      const pct = num(it.usedPercent)
      const label = quotaLabel(s, it)
      const row = { key: `${s.id}-${i}`, label, item: it, pct }
      if (pct !== null) pctRows.push(row)
      else if (usageValue(it) || it.note) valueRows.push(row)
    }
  }
  return [...pctRows, ...valueRows].slice(0, props.limit)
})
const hidden = computed(() => {
  let n = 0
  for (const s of list.value) for (const it of s.items) if (num(it.usedPercent) !== null || usageValue(it) || it.note) n++
  return Math.max(0, n - compactRows.value.length)
})

/** Some items carry an expiry, not a reset, in resetsAt (Qoder 套餐, WorkBuddy 资源包). */
function isExpiry(it: UsageItem): boolean {
  return it.label === '套餐' || it.note === 'resetsAt=最近到期'
}
function reset(it: UsageItem): string {
  if (!it.resetsAt) return ''
  if (isExpiry(it)) return it.resetsAt.startsWith('9999') ? '' : `${dateTime(it.resetsAt)} 到期`
  return countdown(it.resetsAt, props.now)
}
function note(it: UsageItem): string {
  if (it.note === 'resetsAt=最近到期') return '最近到期'
  return noteLabel(it.note)
}
/** What went through Parrot inside this upstream window: tokens, cache, speed and cost. */
function tone(left: number): string {
  return left < 15 ? 'r' : left < 40 ? 'o' : ''
}
function localLine(it: UsageItem): string {
  return localUsageLine(it.local)
}
</script>

<template>
  <div class="uv" :class="{ compact }">
    <template v-if="compact">
      <template v-for="r in compactRows" :key="r.key">
        <div v-if="r.pct !== null" class="win">
          <div class="wh">
            <span class="k truncate" :title="r.label">{{ r.label }}</span>
            <span class="v">剩余 <b class="mono" :class="tone(100 - r.pct)">{{ Math.round(100 - r.pct) }}%</b><template v-if="remainingAmount(r.item)"> · {{ remainingAmount(r.item) }}</template><template v-if="reset(r.item)"> · {{ reset(r.item) }}</template></span>
          </div>
          <Meter :value="100 - r.pct" remaining />
          <p v-if="localLine(r.item)" class="loc mono">{{ localLine(r.item) }}</p>
        </div>
        <div v-else class="vrow">
          <span class="k truncate" :title="r.label">{{ r.label }}</span>
          <span class="text truncate">{{ usageValue(r.item) }}<span v-if="r.item.note" class="faint">{{ usageValue(r.item) ? ' · ' : '' }}{{ note(r.item) }}</span><span v-if="reset(r.item)" class="faint"> · {{ reset(r.item) }}</span></span>
        </div>
      </template>
      <p v-if="hidden" class="more">另有 {{ hidden }} 项明细，打开详情查看</p>
    </template>

    <template v-else>
      <section v-for="s in list" :key="s.id" class="sec">
        <p class="st">{{ sectionTitle(s) }}</p>
        <template v-for="(it, i) in s.items" :key="i">
          <div v-if="num(it.usedPercent) !== null" class="win">
            <div class="wh">
              <span class="k" :title="it.label">{{ windowName(it.label) }}</span>
              <span class="v" :title="it.resetsAt ? dateTime(it.resetsAt) : ''">剩余 <b class="mono" :class="tone(100 - (it.usedPercent ?? 0))">{{ Math.round(100 - (it.usedPercent ?? 0)) }}%</b><template v-if="remainingAmount(it)"> · {{ remainingAmount(it) }}</template><template v-if="reset(it)"> · {{ reset(it) }}</template></span>
            </div>
            <Meter :value="100 - (it.usedPercent ?? 0)" remaining />
            <p v-if="localLine(it)" class="loc mono">{{ localLine(it) }}</p>
            <p v-if="(!remainingAmount(it) && usageValue(it)) || it.note" class="sub">
              <template v-if="!remainingAmount(it)">{{ usageValue(it) }}</template><template v-if="it.note"><template v-if="!remainingAmount(it) && usageValue(it)"> · </template>{{ note(it) }}</template>
            </p>
          </div>
          <div v-else class="vrow">
            <span class="k" :title="it.label">{{ windowName(it.label) }}</span>
            <span class="text">
              {{ usageValue(it) }}<template v-if="it.note"><template v-if="usageValue(it)"> · </template><span class="muted">{{ note(it) }}</span></template>
              <template v-if="!usageValue(it) && !it.note">—</template>
            </span>
            <span v-if="it.resetsAt" class="r" :title="dateTime(it.resetsAt)">{{ reset(it) }}</span>
          </div>
        </template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.uv {
  display: flex;
  flex-direction: column;
  gap: 9px;
  min-width: 0;
}
.win {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.wh {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
}
.k {
  color: var(--text-2);
  min-width: 0;
}
.v {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
}
.v b {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text);
}
.v b.o {
  color: var(--text-orange);
}
.v b.r {
  color: var(--text-red);
}
.loc {
  font-size: 11px;
  color: var(--text-3);
}
.sub {
  font-size: 11px;
  color: var(--text-3);
}
.vrow {
  display: grid;
  grid-template-columns: minmax(56px, 104px) minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
}
.compact .vrow {
  grid-template-columns: 78px minmax(0, 1fr);
  font-size: 11.5px;
}
.text {
  font-variant-numeric: tabular-nums;
  min-width: 0;
}
.r {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}
.more {
  font-size: 11px;
  color: var(--text-3);
}
.uv:not(.compact) {
  gap: 12px;
}
.sec {
  display: flex;
  flex-direction: column;
  gap: 11px;
  padding: 11px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.st {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-2);
}
.uv:not(.compact) .k {
  word-break: break-word;
}
</style>
