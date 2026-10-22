<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChevronRight, ShieldCheck } from '@lucide/vue'
import { api, enc } from '@/api/client'
import type { OAuthAccountDetailData, OAuthAccountSummaryData } from '@/api/types'
import { relative } from '@/utils/format'
import Card from '@/components/ui/Card.vue'
import Badge from '@/components/ui/Badge.vue'
import Meter from '@/components/ui/Meter.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

const PROVIDERS: Record<string, { label: string; color: string; glyph: string }> = {
  claude: { label: 'Claude', color: '#d97757', glyph: 'C' },
  openai: { label: 'OpenAI', color: '#10a37f', glyph: 'O' },
  antigravity: { label: 'Antigravity', color: '#4285f4', glyph: 'G' },
  cursor: { label: 'Cursor', color: '#7c7c86', glyph: '▲' },
  xai: { label: 'xAI', color: '#8e8e93', glyph: 'X' },
  zhipu: { label: '智谱', color: '#3c6cff', glyph: '智' },
  qoder: { label: 'Qoder', color: '#a259ff', glyph: 'Q' },
  workbuddy: { label: 'WorkBuddy', color: '#14b8a6', glyph: 'W' },
}
const WINDOW: Record<string, string> = { fiveHour: '5 小时', sevenDay: '7 天', thirtyDay: '30 天', daily: '每日', monthly: '每月' }

const accounts = ref<OAuthAccountSummaryData[]>([])
const details = ref<Record<string, OAuthAccountDetailData>>({})
const loading = ref(true)
const MAX = 8

async function load(): Promise<void> {
  try {
    const res = await api.get<{ items: OAuthAccountSummaryData[] }>('/oauth/accounts', { pageSize: 100 })
    accounts.value = res.data.items
  } finally {
    loading.value = false
  }
  const queue = accounts.value.filter((a) => a.enabled && !a.invalid).map((a) => a.accountId)
  const worker = async (): Promise<void> => {
    for (let id = queue.shift(); id; id = queue.shift()) {
      try {
        const res = await api.get<OAuthAccountDetailData>(`/oauth/accounts/${enc(id)}`)
        details.value = { ...details.value, [id]: res.data }
      } catch {
        /* keep summary-only row */
      }
    }
  }
  await Promise.all([worker(), worker(), worker(), worker()])
}
onMounted(load)
defineExpose({ reload: load })

function windows(id: string) {
  return (details.value[id]?.usageWindows || []).filter((w) => w.usedPercent !== null && w.usedPercent !== undefined).slice(0, 2)
}
function peak(id: string): number {
  return Math.max(-1, ...windows(id).map((w) => w.usedPercent ?? 0))
}
function state(a: OAuthAccountSummaryData): { label: string; tone: 'green' | 'orange' | 'red' | 'neutral' } {
  if (a.invalid) return { label: '失效', tone: 'red' }
  if (!a.enabled) return { label: '停用', tone: 'neutral' }
  if (a.quotaLimited) return { label: '额度受限', tone: 'orange' }
  return { label: '可用', tone: 'green' }
}

const summary = computed(() => {
  const s = { available: 0, limited: 0, invalid: 0, disabled: 0 }
  for (const a of accounts.value) {
    if (a.invalid) s.invalid++
    else if (!a.enabled) s.disabled++
    else if (a.quotaLimited) s.limited++
    else s.available++
  }
  return s
})

const sorted = computed(() =>
  [...accounts.value].sort((a, b) => {
    const rank = (x: OAuthAccountSummaryData) => (x.invalid ? 3 : !x.enabled ? 2 : 0)
    return rank(a) - rank(b) || peak(b.accountId) - peak(a.accountId)
  }),
)
const visible = computed(() => sorted.value.slice(0, MAX))
const provider = (p: string) => PROVIDERS[p] || { label: p, color: 'var(--text-3)', glyph: p.slice(0, 1).toUpperCase() }
</script>

<template>
  <Card title="账号池">
    <template #icon><ShieldCheck /></template>
    <template #actions>
      <RouterLink to="/planned/oauth" class="all">全部 {{ accounts.length }} 个 <ChevronRight /></RouterLink>
    </template>
    <div v-if="loading" class="sk"><Skeleton v-for="i in 4" :key="i" h="36px" r="8px" /></div>
    <template v-else>
      <div class="sum">
        <span><b class="mono c-green">{{ summary.available }}</b> 可用</span>
        <span v-if="summary.limited"><b class="mono c-orange">{{ summary.limited }}</b> 额度受限</span>
        <span v-if="summary.invalid"><b class="mono c-red">{{ summary.invalid }}</b> 失效</span>
        <span v-if="summary.disabled"><b class="mono">{{ summary.disabled }}</b> 停用</span>
      </div>
      <ul class="list">
        <li v-for="a in visible" :key="a.accountId" :class="{ dim: a.invalid || !a.enabled }">
          <span class="glyph" :style="{ background: provider(a.provider).color }">{{ provider(a.provider).glyph }}</span>
          <div class="who">
            <p class="name truncate">{{ a.displayName }}</p>
            <p class="prov">{{ provider(a.provider).label }}<template v-if="details[a.accountId]?.planType"> · {{ details[a.accountId].planType }}</template></p>
          </div>
          <div class="usage">
            <template v-if="windows(a.accountId).length">
              <div v-for="w in windows(a.accountId)" :key="w.name" class="win" :title="w.resetsAt ? `${relative(w.resetsAt)}重置` : ''">
                <span class="w-k">{{ WINDOW[w.name] || w.name }}</span>
                <Meter :value="w.usedPercent" />
                <span class="w-v mono">{{ Math.round(w.usedPercent ?? 0) }}%</span>
              </div>
            </template>
            <Badge v-else :tone="state(a).tone">{{ state(a).label }}</Badge>
          </div>
        </li>
      </ul>
    </template>
  </Card>
</template>

<style scoped>
.all {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 12px;
}
.all svg {
  width: 13px;
  height: 13px;
}
.sum {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: 12px;
  color: var(--text-2);
  margin-bottom: 10px;
}
.sum b {
  font-size: 14px;
  margin-right: 2px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.list li:last-child {
  border-bottom: 0;
}
.list li.dim {
  opacity: 0.55;
}
.glyph {
  width: 26px;
  height: 26px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.25);
}
.who {
  flex: 1;
  min-width: 0;
}
.name {
  font-size: 12.5px;
}
.prov {
  font-size: 11px;
  color: var(--text-3);
}
.usage {
  width: 46%;
  max-width: 220px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
}
.win {
  width: 100%;
  display: grid;
  grid-template-columns: 38px 1fr 32px;
  align-items: center;
  gap: 6px;
}
.w-k {
  font-size: 10.5px;
  color: var(--text-3);
}
.w-v {
  font-size: 10.5px;
  text-align: right;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
