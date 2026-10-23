<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { visibleNavGroups as navGroups, type NavItem } from '@/nav'
import { eventLink } from '@/api/events'
import { live, qps } from '@/stores/live'
import { goPage } from '@/links'
import Spark from '@/components/ui/Spark.vue'
import { system } from '@/stores/system'
import { duration } from '@/utils/format'
import BrandMark from './BrandMark.vue'
import StatusDot from '@/components/ui/StatusDot.vue'

defineProps<{ rail?: boolean }>()

const route = useRoute()

function active(item: NavItem): boolean {
  if (item.to === '/') return route.path === '/'
  return route.path === item.to || route.path.startsWith(item.to + '/')
}

const inFlight = computed(() => live.inFlight.size)
const linkTone = computed(() => (eventLink.state === 'open' ? 'green' : eventLink.state === 'retrying' ? 'orange' : 'gray'))
const linkText = computed(
  () =>
    ({ open: '实时已连接', connecting: '正在连接…', retrying: '重新连接中…', unauthorized: '登录已过期', closed: '未连接', idle: '未连接' })[
      eventLink.state
    ],
)
const pulse = computed(() => {
  void qps.version
  const n = qps.ok.length
  const spark: number[] = []
  for (let i = n - 300; i < n; i += 10) {
    let s = 0
    for (let j = i; j < i + 10; j++) s += (qps.ok[j] || 0) + (qps.err[j] || 0)
    spark.push(s)
  }
  const perMinute = Math.round(spark.slice(-6).reduce((a, b) => a + b, 0))
  return { spark, perMinute }
})
</script>

<template>
  <aside class="sidebar glass" :class="{ rail }">
    <div class="brand">
      <BrandMark :size="rail ? 30 : 26" />
      <span v-if="!rail" class="name">Parrot</span>
    </div>

    <nav class="nav">
      <div v-for="group in navGroups" :key="group.label" class="group">
        <p v-if="!rail" class="group-label">{{ group.label }}</p>
        <div v-else class="group-sep" />
        <RouterLink
          v-for="item in group.items"
          :key="item.key"
          :to="item.to"
          class="item"
          :class="{ on: active(item) }"
          :title="rail ? item.label : undefined"
        >
          <component :is="item.icon" class="ic" :style="{ color: item.tint }" />
          <span v-if="!rail" class="label">{{ item.label }}</span>
          <template v-if="!rail">
            <span v-if="item.key === 'live' && inFlight" class="count mono">{{ inFlight }}</span>
          </template>
        </RouterLink>
      </div>
    </nav>

    <div class="foot">
      <div v-if="rail" class="rail-dot" :title="linkText">
        <StatusDot :tone="linkTone" :pulse="eventLink.state === 'open'" />
      </div>
      <div v-else class="status-card" role="button" tabindex="0" title="打开实时请求" @click="goPage('/live')">
        <div class="sc-top">
          <StatusDot :tone="linkTone" :pulse="eventLink.state === 'open'" :size="6" />
          <span class="sc-state">{{ linkText }}</span>
          <span class="sc-rate"><b class="num">{{ pulse.perMinute }}</b> 次/分</span>
        </div>
        <Spark class="sc-spark" :values="pulse.spark" :color="linkTone === 'green' ? 'var(--teal)' : 'var(--text-3)'" :height="22" />
        <div class="sc-bottom">
          <button class="sc-ver mono" title="版本更新" @click.stop="goPage('/updates')">
            v{{ system.overview?.version ?? '—' }}
            <i v-if="system.overview?.update?.available" class="sc-new" />
          </button>
          <span class="sc-up">{{ system.overview ? `已运行 ${duration(system.overview.uptimeSeconds)}` : '' }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  width: var(--sidebar-w);
  flex-shrink: 0;
  height: 100%;
  background: var(--sidebar);
  border-right: 0.5px solid var(--border);
  backdrop-filter: blur(30px) saturate(180%);
  -webkit-backdrop-filter: blur(30px) saturate(180%);
}
.sidebar.rail {
  width: var(--sidebar-rail-w);
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--topbar-h);
  padding: 0 16px;
  flex-shrink: 0;
}
.rail .brand {
  justify-content: center;
  padding: 0;
}
.name {
  font-family: var(--font-display);
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 16px;
  letter-spacing: -0.01em;
}
.nav {
  flex: 1;
  overflow-y: auto;
  padding: 6px 12px 12px;
}
.rail .nav {
  padding: 4px 8px 12px;
}
.group + .group {
  margin-top: 14px;
}
.group-label {
  padding: 0 10px 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
}
.group-sep {
  height: 0.5px;
  margin: 8px 10px;
  background: var(--border);
}
.group:first-child .group-sep {
  display: none;
}
.item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 9px;
  height: 30px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 13px;
  transition: background 0.12s;
}
.rail .item {
  justify-content: center;
  height: 38px;
  padding: 0;
}
.item:hover {
  background: var(--fill-3);
}
.item.on {
  background: var(--fill);
  font-weight: 600;
}
.ic {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}
.rail .ic {
  width: 18px;
  height: 18px;
}
.label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.count {
  min-width: 20px;
  height: 17px;
  padding: 0 5px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--teal) 18%, transparent);
  color: var(--teal);
  font-size: 10.5px;
  display: grid;
  place-items: center;
}
.item:hover .foot {
  flex-shrink: 0;
  padding: 10px 10px 12px;
}
.rail .foot {
  padding: 12px 0;
  display: flex;
  justify-content: center;
  border-top: 0.5px solid var(--hairline);
}
.rail-dot {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
}
.status-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px 9px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
  cursor: pointer;
  transition: background 0.15s;
}
.status-card:hover {
  background: var(--fill-2);
}
.sc-top {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
}
.sc-state {
  flex: 1;
  color: var(--text-2);
}
.sc-rate {
  color: var(--text-3);
  font-size: 11px;
}
.sc-rate b {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text);
}
.sc-spark {
  margin: 0 -2px;
}
.sc-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.sc-ver {
  position: relative;
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 10.5px;
  color: var(--text-2);
  background: var(--fill-2);
}
.sc-ver:hover {
  color: var(--accent);
  background: var(--accent-soft);
}
.sc-new {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green);
}
.sc-up {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}
</style>
