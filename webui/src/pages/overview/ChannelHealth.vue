<script setup lang="ts">
import { computed } from 'vue'
import Card from '@/components/ui/Card.vue'
import StatusBars from '@/components/ui/StatusBars.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { catalog } from '@/stores/catalog'
import { channelProvider } from '@/utils/providers'
import { goPage, openChannel } from '@/links'
import { compact, integer } from '@/utils/format'
import { ov } from './data'
import { promptOf } from './metrics'

defineProps<{ ready: boolean }>()

const HEALTH: Record<string, { label: string; tone: string }> = {
  permanentCooldown: { label: '永久冷却', tone: 'r' },
  quotaCooldown: { label: '额度冷却', tone: 'o' },
  cooldown: { label: '冷却中', tone: 'o' },
  unhealthy: { label: '有模型成功率低', tone: 'r' },
  degraded: { label: '有模型成功率下降', tone: 'o' },
}

// Enabled channels/accounts with requests in the last 7 days: problems first, then by today's traffic.
const all = computed(() => {
  const today = new Map(ov.channels.map((r) => [r.key, r.metrics]))
  return (catalog.status?.channels || [])
    .filter((c) => c.enabled)
    .map((c) => {
      const o = ov.outcomes[c.id]
      const m = today.get(c.id)
      const n = o ? o.outcomes.length : 0
      return {
        id: c.id,
        name: c.name,
        health: HEALTH[c.health] ?? null,
        outcomes: o?.outcomes ?? [],
        ok: o?.successCount ?? 0,
        n,
        rate: n ? o!.successCount / n : null,
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
  <Card title="渠道健康" subtitle="最近 20 次请求，一格一次：绿成功、红失败，右侧最新">
    <template #actions>
      <button class="more" @click="goPage('/channels')">全部渠道</button>
    </template>
    <div class="head">
      <span>渠道 / 账号</span>
      <span>最近 20 次</span>
      <span class="r">成功</span>
    </div>
    <div class="list">
      <button v-for="r in rows" :key="r.id" class="item" @click="openChannel(r.id)">
        <ProviderIcon :provider="channelProvider(r.id)" :size="22" />
        <div class="main">
          <span class="name truncate">{{ r.name }}</span>
          <span v-if="r.health" class="sub" :class="r.health.tone">{{ r.health.label }}</span>
          <span v-else class="sub">今日 {{ integer(r.today) }} 次 · {{ compact(r.tokens) }} Token</span>
        </div>
        <StatusBars :outcomes="r.outcomes" :slots="20" :width="3" :gap="2" :height="16" />
        <span class="rate num" :class="r.rate == null ? '' : r.rate >= 0.99 ? 'g' : r.rate >= 0.9 ? 'o' : 'r'">{{ r.ok }}/{{ r.n }}</span>
      </button>
      <p v-if="ready && !rows.length" class="empty">最近 7 天没有请求</p>
      <p v-if="quiet" class="quiet">另有 {{ quiet }} 个启用的渠道最近 7 天没有请求</p>
    </div>
  </Card>
</template>

<style scoped>
.head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 98px 48px;
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
