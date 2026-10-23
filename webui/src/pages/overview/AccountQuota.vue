<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { Schemas } from '@/api/client'
import Card from '@/components/ui/Card.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { goPage, openAccount } from '@/links'
import { compact, dollars, integer, pctValue, relative, usd } from '@/utils/format'
import { ov } from './data'
import { promptOf } from './metrics'
import { quotaLabel, remainingAmount } from '@/pages/oauth/meta'

defineProps<{ ready: boolean }>()

type Item = Schemas['OAuthUsageItemData']

const now = ref(Date.now())
let tick: number | undefined
onMounted(() => (tick = window.setInterval(() => (now.value = Date.now()), 60_000)))
onBeforeUnmount(() => window.clearInterval(tick))

const STATUS: Record<string, { label: string; tone: string }> = {
  authFailed: { label: '认证失效', tone: 'r' },
  quotaDisabled: { label: '额度暂停', tone: 'o' },
  providerRestricted: { label: '平台限制', tone: 'o' },
  userDisabled: { label: '已停用', tone: 'n' },
}

function remaining(i: Item): number | null {
  if (i.remainingPercent != null) return i.remainingPercent
  if (i.usedPercent != null) return 100 - i.usedPercent
  return null
}

function valueText(i: Item): string {
  const v = i.remaining ?? i.used
  if (v == null) return i.note || ''
  if (i.unit === 'usd') return dollars(v)
  return compact(v)
}

function resetText(at: string | null | undefined): string {
  if (!at) return ''
  const diff = new Date(at).getTime() - now.value
  if (diff <= 0) return '已重置'
  const h = Math.floor(diff / 3600_000)
  if (h >= 24) return `${Math.floor(h / 24)} 天后重置`
  if (h >= 1) return `${h} 小时后重置`
  return `${Math.max(1, Math.round(diff / 60_000))} 分钟后重置`
}

const rows = computed(() => {
  const today = new Map(ov.channels.map((r) => [r.key, r.metrics]))
  return ov.accounts
    .map((a) => {
      const m = today.get(`oauth:${a.accountId}`)
      const items = (a.card?.usageSections || []).flatMap((s) => s.items.map((it) => ({ ...it, label: quotaLabel(s, it) })))
      const bars = items.filter((i) => remaining(i) != null).slice(0, 6)
      const values = items.filter((i) => remaining(i) == null && (i.remaining != null || i.used != null)).slice(0, 2)
      return {
        a,
        tokens: m ? promptOf(m) + m.outputTokens : 0,
        requests: m?.total ?? 0,
        cost: m?.costTicks ?? 0,
        status: a.card?.statusTag ? STATUS[a.card.statusTag] : a.invalid ? STATUS.authFailed : null,
        bars,
        values,
        fetchedAt: a.card?.usageFetchedAt ?? null,
      }
    })
    .filter((r) => r.a.enabled)
})
</script>

<template>
  <Card title="OAuth 账号额度" subtitle="按调度顺序列出启用的账号：剩余额度、余额与积分">
    <template #actions>
      <button class="more" @click="goPage('/oauth')">全部账号</button>
    </template>
    <div class="list">
      <button v-for="r in rows" :key="r.a.accountId" class="item" @click="openAccount(r.a.accountId, r.a.displayName)">
        <div class="head">
          <ProviderIcon :provider="r.a.provider" :size="22" />
          <div class="who">
            <div class="l1">
              <span class="name truncate">{{ r.a.displayName }}</span>
              <span v-if="r.status" class="tag" :class="r.status.tone">{{ r.status.label }}</span>
            </div>
            <span class="plan truncate">{{ r.a.card?.plan.label || r.a.provider }}</span>
          </div>
          <div class="use num" title="今日用量">
            <b>{{ usd(r.cost) }}</b>
            <span>今日 {{ integer(r.requests) }} 次 · {{ compact(r.tokens) }} Token</span>
          </div>
        </div>
        <div v-if="r.bars.length || r.values.length" class="quota">
          <div v-for="(q, i) in r.bars" :key="i" class="q">
            <span class="ql">{{ q.label }}</span>
            <span class="qbar">
              <span
                :style="{ width: `${Math.max(0, Math.min(100, remaining(q) ?? 0))}%` }"
                :class="(remaining(q) ?? 0) < 15 ? 'r' : (remaining(q) ?? 0) < 40 ? 'o' : 'g'"
              />
            </span>
            <span class="qv num">剩 {{ pctValue(remaining(q) ?? 0) }}</span>
            <span class="qr">{{ resetText(q.resetsAt) || remainingAmount(q) }}</span>
          </div>
          <div v-if="r.values.length" class="vals">
            <span v-for="(v, i) in r.values" :key="i">{{ v.label }} <b class="num">{{ valueText(v) }}</b></span>
          </div>
        </div>
        <p v-else class="noq">{{ r.fetchedAt ? `额度更新于 ${relative(r.fetchedAt)}，无额度窗口` : '尚未获取额度数据' }}</p>
      </button>
      <p v-if="ready && !rows.length" class="empty">今天还没有 OAuth 账号被使用</p>
    </div>
  </Card>
</template>

<style scoped>
.list {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
  overflow-x: hidden;
  margin: 0 -8px;
}
.item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border-radius: var(--radius-md);
  text-align: left;
}
.item + .item {
  border-top: 1px solid var(--hairline);
  border-radius: 0 0 var(--radius-md) var(--radius-md);
}
.item:hover {
  background: var(--surface-hover);
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.who {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.l1 {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.name {
  font-size: 13px;
  font-weight: 600;
  min-width: 0;
}
.plan {
  font-size: 11.5px;
  color: var(--text-3);
  min-width: 0;
}
.use {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-size: 11.5px;
  color: var(--text-3);
}
.use b {
  font-size: 13px;
  color: var(--text);
}
.tag {
  flex-shrink: 0;
  padding: 0 6px;
  border-radius: 999px;
  font-size: 11px;
}
.tag.r {
  color: var(--text-red);
  background: color-mix(in srgb, var(--red) 12%, transparent);
}
.tag.o {
  color: var(--text-orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}
.tag.n {
  color: var(--text-3);
  background: var(--fill-2);
}
/* One grid per account, so its bars line up whatever the widths of their texts. */
.quota {
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 4px 8px;
  padding-left: 30px;
  font-size: 11.5px;
}
.q {
  display: contents;
}
.vals {
  grid-column: 1 / -1;
}
.ql {
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.qbar {
  height: 6px;
  border-radius: 3px;
  background: var(--track);
  overflow: hidden;
}
.qbar span {
  display: block;
  height: 100%;
  border-radius: 3px;
}
.qbar .g {
  background: var(--green);
}
.qbar .o {
  background: var(--orange);
}
.qbar .r {
  background: var(--red);
}
.qv {
  font-weight: 650;
  text-align: right;
  white-space: nowrap;
}
.qr {
  color: var(--text-3);
  text-align: right;
  white-space: nowrap;
}
.vals {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: 11.5px;
  color: var(--text-2);
}
.noq {
  padding-left: 30px;
  font-size: 11.5px;
  color: var(--text-3);
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
