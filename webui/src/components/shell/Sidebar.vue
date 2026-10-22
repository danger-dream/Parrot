<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { navGroups, type NavItem } from '@/nav'
import { eventLink } from '@/api/events'
import { live } from '@/stores/live'
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
    ({ open: '实时连接正常', connecting: '正在连接…', retrying: '重新连接中…', unauthorized: '登录已过期', closed: '未连接', idle: '未连接' })[
      eventLink.state
    ],
)
</script>

<template>
  <aside class="sidebar glass" :class="{ rail }">
    <div class="brand">
      <BrandMark :size="rail ? 30 : 26" />
      <div v-if="!rail" class="brand-text">
        <span class="name">Parrot</span>
        <span class="ver mono">{{ system.overview ? 'v' + system.overview.version : '' }}</span>
      </div>
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
          :class="{ on: active(item), planned: item.phase > 1 }"
          :title="rail ? item.label : undefined"
        >
          <component :is="item.icon" class="ic" :style="{ color: item.tint }" />
          <span v-if="!rail" class="label">{{ item.label }}</span>
          <template v-if="!rail">
            <span v-if="item.key === 'live' && inFlight" class="count mono">{{ inFlight }}</span>
            <span v-else-if="item.phase > 1" class="soon">规划中</span>
          </template>
        </RouterLink>
      </div>
    </nav>

    <div class="foot">
      <div class="foot-row" :title="linkText">
        <StatusDot :tone="linkTone" :pulse="eventLink.state === 'open'" />
        <span v-if="!rail" class="foot-text">{{ linkText }}</span>
      </div>
      <div v-if="!rail && system.overview" class="foot-row faint">
        <span class="foot-text">已运行 {{ duration(system.overview.uptimeSeconds) }}</span>
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
.brand-text {
  display: flex;
  align-items: baseline;
  gap: 7px;
}
.name {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 15px;
  letter-spacing: -0.01em;
}
.ver {
  font-size: 10.5px;
  color: var(--text-3);
}
.nav {
  flex: 1;
  overflow-y: auto;
  padding: 4px 10px 12px;
}
.rail .nav {
  padding: 4px 8px 12px;
}
.group + .group {
  margin-top: 14px;
}
.group-label {
  padding: 0 8px 4px;
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
  padding: 0 8px;
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 13px;
  transition: background 0.15s;
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
  background: var(--fill-2);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.item.on::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--accent);
}
.rail .item.on::before {
  left: -8px;
}
.item.planned .label {
  color: var(--text-2);
}
.item.planned .ic {
  opacity: 0.55;
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
.soon {
  font-size: 10px;
  color: var(--text-3);
  opacity: 0;
  transition: opacity 0.15s;
}
.item:hover .soon {
  opacity: 1;
}
.foot {
  flex-shrink: 0;
  padding: 10px 16px 14px;
  border-top: 0.5px solid var(--hairline);
}
.rail .foot {
  padding: 12px 0;
  display: flex;
  justify-content: center;
}
.foot-row {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 20px;
}
.foot-text {
  font-size: 11.5px;
  color: var(--text-2);
}
.faint .foot-text {
  color: var(--text-3);
}
</style>
