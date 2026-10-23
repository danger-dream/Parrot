<script setup lang="ts">
import { CircleCheck, CircleX } from '@lucide/vue'
import type { Schemas } from '@/api/client'
import { clock, dateTime, relative } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import EntityLink from '@/components/ui/EntityLink.vue'
import { CATEGORY_LABEL } from './types'

type Check = Schemas['NetworkCheckData']
defineProps<{ items: Check[]; compact?: boolean }>()

/** Channel checks are keyed “channel:<routing channel id>”. */
function channelId(c: Check): string | null {
  if (c.category !== 'channel' || !c.key.startsWith('channel:')) return null
  const id = c.key.slice('channel:'.length)
  return id.includes(':') ? id : `api:${id}`
}
function label(c: Check): string {
  return c.category === 'channel' ? c.label.replace(/^渠道\s*/, '') : c.label
}
</script>

<template>
  <div class="cl" :class="{ compact }">
    <div v-for="(c, i) in items" :key="c.key + i" class="row">
      <CircleCheck v-if="c.ok" class="ic ok" />
      <CircleX v-else class="ic bad" />
      <span class="cat"><Badge>{{ CATEGORY_LABEL[c.category] || '其他' }}</Badge></span>
      <span class="nm">
        <EntityLink v-if="channelId(c)" kind="channel" :id="channelId(c)!" :label="label(c)" />
        <template v-else>{{ label(c) }}</template>
      </span>
      <span class="lat num">{{ c.latencyMilliseconds !== null && c.latencyMilliseconds !== undefined ? `${c.latencyMilliseconds} ms` : '—' }}</span>
      <span v-if="!compact" class="at num" :title="dateTime(c.checkedAt)">{{ c.checkedAt ? `${clock(c.checkedAt)} · ${relative(c.checkedAt)}` : '—' }}</span>
      <span class="dt" :class="{ bad: !c.ok }">{{ c.detail || (c.ok ? '正常' : '失败') }}</span>
    </div>
  </div>
</template>

<style scoped>
.cl {
  display: flex;
  flex-direction: column;
}
.row {
  display: grid;
  grid-template-columns: 16px 76px minmax(0, 1.2fr) 64px 130px minmax(0, 1.6fr);
  align-items: center;
  column-gap: 10px;
  min-height: 34px;
  padding: 4px 0;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.compact .row {
  grid-template-columns: 16px 76px minmax(0, 1.2fr) 64px minmax(0, 1.6fr);
}
.row:last-child {
  border-bottom: 0;
}
.ic {
  width: 14px;
  height: 14px;
}
.ok {
  color: var(--green);
}
.bad {
  color: var(--red);
}
.nm {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: flex;
}
.lat,
.at {
  font-size: 12px;
  color: var(--text-2);
  text-align: right;
  white-space: nowrap;
}
.at {
  text-align: left;
}
.dt {
  font-size: 11.5px;
  color: var(--text-3);
  overflow-wrap: anywhere;
  min-width: 0;
}
.dt.bad {
  color: var(--red);
}
@container (max-width: 720px) {
  .row,
  .compact .row {
    grid-template-columns: 16px minmax(0, 1fr) auto;
    row-gap: 2px;
    padding: 6px 0;
  }
  .cat {
    display: none;
  }
  .at,
  .dt {
    grid-column: 2 / -1;
    text-align: left;
  }
}
</style>
