<script setup lang="ts">
import { computed, ref } from 'vue'
import type { StatsBreakdownData } from '@/api/types'
import Card from '@/components/ui/Card.vue'
import Segmented from '@/components/ui/Segmented.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { channelName } from '@/stores/catalog'
import { channelProvider } from '@/utils/providers'
import { goPage, openApiKey, openChannel, openModel } from '@/links'
import { compact, integer, usd } from '@/utils/format'
import { inView, RANK_VIEWS, UNKNOWN_KEYS, VIEW_NAME, type RankView } from '@/pages/analytics/metrics'
import { ov } from './data'
import { cacheShare, pctText, promptOf, successRateOf, tpsText } from './metrics'

defineProps<{ ready: boolean }>()

const view = ref<RankView>('model')

/** Today's top spenders of the chosen kind, by amount. */
const list = computed(() => {
  const source: StatsBreakdownData[] = view.value === 'model' ? ov.models : view.value === 'apiKey' ? ov.apiKeys : ov.channels
  const rows = source
    .filter((r) => !UNKNOWN_KEYS.has(r.key) && inView(view.value, r.key) && r.metrics.total > 0)
    .sort((a, b) => b.metrics.costTicks - a.metrics.costTicks || b.metrics.total - a.metrics.total)
    .slice(0, 8)
  const top = Math.max(...rows.map((r) => r.metrics.costTicks), 1)
  return rows.map((r) => ({ ...r, share: r.metrics.costTicks / top, owner: view.value === 'model' ? (r.channels?.[0] ?? null) : null }))
})
const byChannel = computed(() => view.value === 'oauth' || view.value === 'api')

function open(key: string): void {
  if (view.value === 'model') openModel(key)
  else if (view.value === 'apiKey') openApiKey(key)
  else openChannel(key)
}
</script>

<template>
  <Card title="用量排行（今日 · 按金额）">
    <template #actions>
      <button class="more" @click="goPage('/analytics', { period: 'today', dimension: view })">查看全部</button>
    </template>
    <Segmented v-model="view" size="sm" :options="RANK_VIEWS" class="tabs" />
    <div class="list">
      <button v-for="(r, i) in list" :key="r.key" class="item" @click="open(r.key)">
        <span class="rank" :class="`r${i + 1}`">{{ i + 1 }}</span>
        <div class="main">
          <div class="top">
            <span v-if="byChannel" class="who">
              <ProviderIcon :provider="channelProvider(r.key)" :size="15" />
              <span class="name">{{ channelName(r.key) }}</span>
            </span>
            <span v-else class="name" :class="{ mono: view === 'model' }">{{ r.key }}</span>
            <b class="cost num">{{ usd(r.metrics.costTicks) }}</b>
          </div>
          <div class="bar"><span :style="{ width: `${Math.max(r.share * 100, 2)}%` }" /></div>
          <div class="meta">
            <span>{{ integer(r.metrics.total) }} 次 · {{ pctText(successRateOf(r.metrics)) }}</span>
            <span>↑{{ compact(promptOf(r.metrics)) }} ↓{{ compact(r.metrics.outputTokens) }}</span>
            <span>缓存占比 {{ pctText(cacheShare(r.metrics)) }}</span>
            <span>{{ tpsText(r.metrics.averageTokensPerSecond) }}</span>
            <span v-if="r.owner" class="owner">
              <ProviderIcon :provider="channelProvider(r.owner.key)" :size="13" />
              {{ channelName(r.owner.key) }}
            </span>
          </div>
        </div>
      </button>
      <p v-if="ready && !list.length" class="empty">今天还没有{{ VIEW_NAME[view] }}产生请求</p>
    </div>
  </Card>
</template>

<style scoped>
.list {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
  overflow-x: hidden;
  margin: 0 -8px;
}
.item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px;
  border-radius: var(--radius-md);
  text-align: left;
}
.item:hover {
  background: var(--surface-hover);
}
.rank {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-2);
  background: var(--fill-2);
}
.rank.r1 {
  color: #fff;
  background: #f59e0b;
}
.rank.r2 {
  color: #fff;
  background: #94a3b8;
}
.rank.r3 {
  color: #fff;
  background: #c2783f;
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.top {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
}
.cost {
  font-size: 13.5px;
}
.bar {
  height: 5px;
  border-radius: 3px;
  background: var(--track);
  overflow: hidden;
}
.bar span {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--blue);
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
  font-size: 11.5px;
  color: var(--text-3);
}
.owner {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 180px;
  overflow: hidden;
  white-space: nowrap;
}
.tabs {
  align-self: flex-start;
  margin-bottom: 8px;
}
.who {
  flex: 1;
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
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
