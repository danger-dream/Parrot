<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheck, HeartPulse } from '@lucide/vue'
import { catalog, channelName } from '@/stores/catalog'
import { system } from '@/stores/system'
import { relative } from '@/utils/format'
import Card from '@/components/ui/Card.vue'
import Badge from '@/components/ui/Badge.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

const HEALTH: { key: string; label: string; color: string }[] = [
  { key: 'healthy', label: '正常', color: 'var(--green)' },
  { key: 'unknown', label: '暂无流量', color: 'var(--fill)' },
  { key: 'degraded', label: '降级', color: 'var(--yellow)' },
  { key: 'cooldown', label: '冷却中', color: 'var(--orange)' },
  { key: 'quotaCooldown', label: '额度冷却', color: 'var(--orange)' },
  { key: 'unhealthy', label: '异常', color: 'var(--red)' },
  { key: 'permanentCooldown', label: '永久冷却', color: 'var(--red)' },
  { key: 'disabled', label: '已停用', color: 'var(--text-3)' },
]

const REASON: Record<string, string> = {
  auth_error: '认证失败',
  quota: '额度用尽',
  quota_exhausted: '额度用尽',
  disabled: '已停用',
  cooldown: '冷却中',
  permanent_cooldown: '永久冷却',
  low_success_rate: '成功率低',
  error_rate: '错误率高',
  active: '冷却中',
  degraded: '成功率下降',
  unhealthy: '连续失败',
  quota_cooldown: '额度冷却',
}

function healthTone(key: string): 'neutral' | 'orange' | 'red' {
  if (key === 'disabled' || key === 'unknown') return 'neutral'
  if (key === 'unhealthy' || key === 'permanentCooldown') return 'red'
  return 'orange'
}

const status = computed(() => catalog.status)

const counts = computed(() => {
  const out: Record<string, number> = {}
  for (const ch of status.value?.channels || []) out[ch.health] = (out[ch.health] || 0) + 1
  return out
})
const total = computed(() => status.value?.channels.length || 0)
const segments = computed(() => HEALTH.filter((h) => counts.value[h.key]).map((h) => ({ ...h, n: counts.value[h.key] })))

const problems = computed(() => (status.value?.problemChannels || []).slice(0, 6))
const quotaWarnings = computed(() => status.value?.quotaWarnings || [])
const alerts = computed(() => {
  const out: { provider: string; text: string }[] = []
  const active = system.overview?.activeAlerts || {}
  for (const [provider, list] of Object.entries(active)) {
    if (!Array.isArray(list)) continue
    for (const item of list) {
      const o = item as Record<string, unknown>
      out.push({ provider, text: String(o.title || o.name || o.summary || '服务异常') })
    }
  }
  return out
})
const allGood = computed(() => !problems.value.length && !quotaWarnings.value.length && !alerts.value.length)

function reason(list: string[]): string {
  return list.map((r) => REASON[r] || r).join('、') || '状态异常'
}
function healthLabel(key: string): string {
  return HEALTH.find((h) => h.key === key)?.label || key
}
</script>

<template>
  <Card title="健康状况" :subtitle="catalog.loadedAt ? `${relative(catalog.loadedAt)}更新` : ''">
    <template #icon><HeartPulse /></template>
    <div v-if="!status" class="sk"><Skeleton h="10px" /><Skeleton h="40px" /><Skeleton h="40px" /></div>
    <template v-else>
      <div class="bar" :title="`共 ${total} 个渠道与账号`">
        <span v-for="s in segments" :key="s.key" :style="{ flex: s.n, background: s.color }" />
      </div>
      <div class="legend">
        <span v-for="s in segments" :key="s.key"><i :style="{ background: s.color }" />{{ s.label }} <b class="mono">{{ s.n }}</b></span>
      </div>

      <div v-if="allGood" class="ok">
        <CircleCheck />
        <div>
          <p class="ok-t">一切正常</p>
          <p class="ok-s">没有异常渠道、额度预警或上游告警。</p>
        </div>
      </div>
      <ul v-else class="issues">
        <li v-for="a in alerts" :key="a.provider + a.text">
          <Badge tone="red">告警</Badge>
          <span class="i-main truncate">{{ a.provider }} · {{ a.text }}</span>
        </li>
        <li v-for="q in quotaWarnings" :key="q.accountId">
          <Badge tone="orange">额度</Badge>
          <span class="i-main truncate">{{ channelName(`oauth:${q.accountId}`) }}</span>
          <span class="i-side mono">{{ q.metrics.map((m) => `${m.window} ${Math.round(m.utilizationPercent)}%`).join(' · ') }}</span>
        </li>
        <li v-for="p in problems" :key="p.id">
          <Badge :tone="healthTone(p.health)">{{ healthLabel(p.health) }}</Badge>
          <span class="i-main truncate">{{ p.name }}</span>
          <span class="i-side">{{ reason(p.problemReasons) }}</span>
        </li>
        <li v-if="(status.problemChannels?.length || 0) > problems.length" class="more">
          另有 {{ status.problemChannels.length - problems.length }} 个渠道需要留意
        </li>
      </ul>
    </template>
  </Card>
</template>

<style scoped>
.bar {
  display: flex;
  gap: 2px;
  height: 8px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--fill-3);
}
.bar span {
  min-width: 4px;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--text-2);
}
.legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.legend i {
  width: 7px;
  height: 7px;
  border-radius: 2px;
}
.legend b {
  font-weight: 600;
  color: var(--text);
}
.ok {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
  padding: 12px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--green) 9%, transparent);
}
.ok svg {
  width: 22px;
  height: 22px;
  color: var(--green);
  flex-shrink: 0;
}
.ok-t {
  font-weight: 600;
}
.ok-s {
  font-size: 12px;
  color: var(--text-2);
}
.issues {
  list-style: none;
  margin: 14px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.issues li {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.issues li:last-child {
  border-bottom: 0;
}
.i-main {
  flex: 1;
}
.i-side {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
}
.more {
  color: var(--text-3);
  font-size: 12px;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>
