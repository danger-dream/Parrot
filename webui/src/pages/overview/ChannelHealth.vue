<script setup lang="ts">
import { computed } from 'vue'
import Card from '@/components/ui/Card.vue'
import OutcomeStrip from '@/components/ui/OutcomeStrip.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { catalog } from '@/stores/catalog'
import { channelProvider } from '@/utils/providers'
import { goLogs, goPage, openChannel, openRequest } from '@/links'
import { Ban } from '@lucide/vue'
import { compact, integer } from '@/utils/format'
import type { OutcomeItem } from '@/utils/outcomes'
import { ov } from './data'
import { promptOf } from './metrics'

defineProps<{ ready: boolean }>()

type Status = NonNullable<typeof catalog.status>['channels'][number]
type Failure = NonNullable<OutcomeItem['failures']>[number]

/** "HTTP 429 · 您的使用量已超出频率限制…" from a stored attempt error (`HTTP nnn: {json}` or plain text). */
function briefError(raw: string | null | undefined): string {
  if (!raw) return '未知错误'
  const http = /^HTTP (\d{3}): ([\s\S]*)$/.exec(raw)
  const body = http ? http[2] : raw
  let text = body
  try {
    const obj = JSON.parse(body)
    const err = obj?.error && typeof obj.error === 'object' ? obj.error : obj
    text = String(err?.message || err?.msg || obj?.message || obj?.msg || body)
  } catch {
    // Plain text, or JSON cut off by the stored length limit.
    const quoted = /"(?:message|msg)"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(body)
    if (quoted) text = quoted[1].replace(/\\(["\\/])/g, '$1').replace(/\\n/g, ' ')
  }
  text = text.replace(/\s+/g, ' ').trim()
  return http ? `HTTP ${http[1]} · ${text}` : text
}

/**
 * What is wrong, naming the models: cooldowns first, then today's failed attempts with the latest
 * error. Both come from the same attempts as the strip and the failure count on the right.
 */
function problemOf(c: Status, failures: Failure[], tone: string): { label: string; tone: string } | null {
  const cool = c.cooldowns || []
  if (cool.length) {
    const permanent = cool.some((x) => x.state === 'permanent')
    const kind = permanent ? '永久冷却' : cool.every((x) => x.quota) ? '额度冷却' : '冷却中'
    const names = cool.map((x) => x.model).filter(Boolean)
    return { label: `${kind} · ${names.slice(0, 2).join('、')}${names.length > 2 ? ` 等 ${names.length} 个` : ''}`, tone: permanent ? 'r' : 'o' }
  }
  if (failures.length) {
    const f = failures[0]
    const more = failures.length > 1 ? `；另 ${failures.length - 1} 个模型也有失败` : ''
    return { label: `${f.model} 今日失败 ${integer(f.count)} 次 · ${briefError(f.lastError)}${more}`, tone }
  }
  return null
}

/** The most recent failed request on this channel, which shows the whole attempt chain. */
function latestFailure(failures: Failure[]): string | null {
  let best: Failure | null = null
  for (const f of failures) if (f.lastRequestId && (!best || String(f.lastAt || '') > String(best.lastAt || ''))) best = f
  return best?.lastRequestId || null
}

// Enabled channels/accounts with attempts today: problems first, then by today's traffic.
// The strip and the failure count cover every attempt today, so a failure that failover
// absorbed still shows on the channel that failed.
const all = computed(() => {
  const today = new Map(ov.channels.map((r) => [r.key, r.metrics]))
  return (catalog.status?.channels || [])
    .filter((c) => c.enabled)
    .map((c) => {
      const o = ov.outcomes?.items[c.id]
      const m = today.get(c.id)
      const n = o?.total ?? 0
      const errors = o?.errorCount ?? 0
      const tone = errors ? (errors / n >= 0.2 ? 'r' : 'o') : 'g'
      const failures = o?.failures || []
      return {
        id: c.id,
        name: c.name,
        health: problemOf(c, failures, tone),
        failedRequest: latestFailure(failures),
        outcome: o ?? null,
        errors,
        tone,
        n,
        today: m?.total ?? 0,
        tokens: m ? promptOf(m) + m.outputTokens : 0,
      }
    })
    .sort((a, b) => Number(!!b.health) - Number(!!a.health) || b.today - a.today || b.n - a.n || a.name.localeCompare(b.name))
})
const rows = computed(() => all.value.filter((r) => r.n > 0))
const quiet = computed(() => all.value.length - rows.value.length)
// Requests that failed before reaching any channel (e.g. no channel available for the model).
const unassigned = computed(() => {
  const o = ov.outcomes?.items['?']
  return o && o.total ? o : null
})
const elapsed = computed(() => ov.outcomes?.count ?? 0)

function openUnassigned(): void {
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  goLogs({ status: 'error', from: start.getTime() })
}

function openRow(r: { id: string; failedRequest: string | null }): void {
  if (r.failedRequest) openRequest(r.failedRequest)
  else openChannel(r.id)
}
</script>

<template>
  <Card title="渠道健康" subtitle="今日每次尝试按小时统计：绿全部成功、橙有失败、红失败超两成">
    <template #actions>
      <button class="more" @click="goPage('/channels')">全部渠道</button>
    </template>
    <div class="head">
      <span>渠道 / 账号</span>
      <span>今日 00–23 时</span>
      <span class="r">失败</span>
    </div>
    <div class="list">
      <button v-if="unassigned && unassigned.errorCount" class="item" @click="openUnassigned">
        <span class="ic"><Ban /></span>
        <div class="main">
          <span class="name truncate">未分配到渠道</span>
          <span class="sub o truncate" title="没有可用渠道等原因，请求没有发到任何渠道">今日 {{ integer(unassigned.total) }} 次 · 没有可用渠道等原因</span>
        </div>
        <OutcomeStrip :item="unassigned" :from="ov.outcomes?.from" :count="24" :elapsed="elapsed" :width="3" :gap="1" :height="16" />
        <span class="rate num r">失败 {{ integer(unassigned.errorCount) }}</span>
      </button>
      <button
        v-for="r in rows"
        :key="r.id"
        class="item"
        :title="r.failedRequest ? '查看最近一次失败的请求' : undefined"
        @click="openRow(r)"
      >
        <ProviderIcon :provider="channelProvider(r.id)" :size="22" />
        <div class="main">
          <span class="name truncate">{{ r.name }}</span>
          <span v-if="r.health" class="sub truncate" :class="r.health.tone" :title="r.health.label">{{ r.health.label }}</span>
          <span v-else class="sub">今日 {{ integer(r.today) }} 次 · {{ compact(r.tokens) }} Token</span>
        </div>
        <OutcomeStrip :item="r.outcome" :from="ov.outcomes?.from" :count="24" :elapsed="elapsed" :width="3" :gap="1" :height="16" />
        <span class="rate num" :class="r.tone" :title="`今日尝试 ${integer(r.n)} 次${r.errors ? `，失败 ${integer(r.errors)} 次（含已切换到其他渠道完成的请求）` : ''}`">{{ r.errors ? `失败 ${integer(r.errors)}` : '无失败' }}</span>
      </button>
      <p v-if="ready && !rows.length && !unassigned" class="empty">今天还没有请求</p>
      <p v-if="quiet" class="quiet">另有 {{ quiet }} 个启用的渠道今天没有请求</p>
    </div>
  </Card>
</template>

<style scoped>
.head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 96px 56px;
  gap: 10px;
  padding: 0 0 6px 32px;
  font-size: 11.5px;
  color: var(--text-3);
}
.head .r {
  text-align: right;
}
.list {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  overflow-x: hidden;
  margin: 0 -8px;
}
.ic {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border-radius: 6px;
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.ic svg {
  width: 14px;
  height: 14px;
}
.item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 8px;
  border-radius: var(--radius-md);
  text-align: left;
}
.item:hover {
  background: var(--surface-hover);
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.name {
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
}
.sub.o {
  color: var(--text-orange);
}
.sub.r {
  color: var(--text-red);
}
.quiet {
  padding: 8px 8px 0;
  font-size: 11.5px;
  color: var(--text-3);
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rate {
  width: 48px;
  text-align: right;
  font-size: 13px;
  font-weight: 650;
  color: var(--text-3);
}
.rate.g {
  color: var(--text-green);
}
.rate.o {
  color: var(--text-orange);
}
.rate.r {
  color: var(--text-red);
}
.more {
  font-size: 12.5px;
  color: var(--accent);
}
.empty {
  padding: 40px 0;
  text-align: center;
  color: var(--text-3);
}
</style>
