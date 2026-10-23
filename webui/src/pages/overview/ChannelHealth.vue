<script setup lang="ts">
import { computed } from 'vue'
import Card from '@/components/ui/Card.vue'
import OutcomeStrip from '@/components/ui/OutcomeStrip.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { catalog } from '@/stores/catalog'
import { channelProvider } from '@/utils/providers'
import { goPage, openChannel } from '@/links'
import { compact, integer, pctValue } from '@/utils/format'
import { ov } from './data'
import { promptOf } from './metrics'

defineProps<{ ready: boolean }>()

type Status = NonNullable<typeof catalog.status>['channels'][number]

/** What is wrong, naming the models: cooldowns first, then models whose recent success rate is low. */
function problemOf(c: Status): { label: string; tone: string } | null {
  const cool = c.cooldowns || []
  if (cool.length) {
    const permanent = cool.some((x) => x.state === 'permanent')
    const kind = permanent ? '永久冷却' : cool.every((x) => x.quota) ? '额度冷却' : '冷却中'
    const names = cool.map((x) => x.model).filter(Boolean)
    return { label: `${kind} · ${names.slice(0, 2).join('、')}${names.length > 2 ? ` 等 ${names.length} 个` : ''}`, tone: permanent ? 'r' : 'o' }
  }
  const weak = c.weakModels || []
  if (weak.length) {
    const w = weak[0]
    const more = weak.length > 1 ? `，另 ${weak.length - 1} 个模型偏低` : ''
    return {
      label: `${w.model} 成功率 ${pctValue(w.successRate)}（最近 ${w.recentRequests} 次）${more}`,
      tone: w.successRate < 50 ? 'r' : 'o',
    }
  }
  return null
}

// Enabled channels/accounts with requests in the last 24 hours: problems first, then by today's traffic.
const all = computed(() => {
  const today = new Map(ov.channels.map((r) => [r.key, r.metrics]))
  return (catalog.status?.channels || [])
    .filter((c) => c.enabled)
    .map((c) => {
      const o = ov.outcomes?.items[c.id]
      const m = today.get(c.id)
      const n = o?.total ?? 0
      return {
        id: c.id,
        name: c.name,
        health: problemOf(c),
        outcome: o ?? null,
        errors: o?.errorCount ?? 0,
        n,
        today: m?.total ?? 0,
        tokens: m ? promptOf(m) + m.outputTokens : 0,
      }
    })
    .sort((a, b) => Number(!!b.health) - Number(!!a.health) || b.today - a.today || b.n - a.n || a.name.localeCompare(b.name))
})
const rows = computed(() => all.value.filter((r) => r.n > 0))
const quiet = computed(() => all.value.length - rows.value.length)
</script>

<template>
  <Card title="渠道健康" subtitle="近 24 小时，一格一小时：绿全部成功、橙有失败、红失败占两成以上，右侧最新">
    <template #actions>
      <button class="more" @click="goPage('/channels')">全部渠道</button>
    </template>
    <div class="head">
      <span>渠道 / 账号</span>
      <span>近 24 小时</span>
      <span class="r">失败</span>
    </div>
    <div class="list">
      <button v-for="r in rows" :key="r.id" class="item" @click="openChannel(r.id)">
        <ProviderIcon :provider="channelProvider(r.id)" :size="22" />
        <div class="main">
          <span class="name truncate">{{ r.name }}</span>
          <span v-if="r.health" class="sub truncate" :class="r.health.tone" :title="r.health.label">{{ r.health.label }}</span>
          <span v-else class="sub">今日 {{ integer(r.today) }} 次 · {{ compact(r.tokens) }} Token</span>
        </div>
        <OutcomeStrip :item="r.outcome" :from="ov.outcomes?.from" :width="3" :gap="1" :height="16" />
        <span class="rate num" :class="r.errors ? (r.errors / r.n >= 0.2 ? 'r' : 'o') : 'g'" :title="`近 24 小时 ${integer(r.n)} 次`">{{ r.errors ? `失败 ${integer(r.errors)}` : '无失败' }}</span>
      </button>
      <p v-if="ready && !rows.length" class="empty">近 24 小时没有请求</p>
      <p v-if="quiet" class="quiet">另有 {{ quiet }} 个启用的渠道近 24 小时没有请求</p>
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
  height: 278px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  margin: 0 -8px;
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
