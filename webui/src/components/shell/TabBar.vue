<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { Activity, Ellipsis, LayoutDashboard, ShieldCheck, Waypoints, X } from '@lucide/vue'
import { navGroups } from '@/nav'

const route = useRoute()
const moreOpen = ref(false)

const tabs = [
  { to: '/', label: '概览', icon: LayoutDashboard },
  { to: '/live', label: '实时', icon: Activity },
  { to: '/planned/oauth', label: '账户', icon: ShieldCheck },
  { to: '/planned/channels', label: '渠道', icon: Waypoints },
]

function active(to: string): boolean {
  return to === '/' ? route.path === '/' : route.path.startsWith(to)
}
</script>

<template>
  <nav class="tabbar glass">
    <RouterLink v-for="t in tabs" :key="t.to" :to="t.to" class="tab" :class="{ on: active(t.to) }">
      <component :is="t.icon" />
      <span>{{ t.label }}</span>
    </RouterLink>
    <button class="tab" :class="{ on: moreOpen }" @click="moreOpen = true">
      <Ellipsis />
      <span>更多</span>
    </button>
  </nav>

  <Transition name="sheet">
    <div v-if="moreOpen" class="sheet-wrap" @click.self="moreOpen = false">
      <div class="sheet glass">
        <div class="sheet-head">
          <span class="grabber" />
          <p>全部功能</p>
          <button class="close" aria-label="关闭" @click="moreOpen = false"><X /></button>
        </div>
        <div class="sheet-body">
          <section v-for="g in navGroups" :key="g.label">
            <p class="g-label">{{ g.label }}</p>
            <div class="grid">
              <RouterLink v-for="item in g.items" :key="item.key" :to="item.to" class="cell" @click="moreOpen = false">
                <span class="cell-ic" :style="{ color: item.tint }"><component :is="item.icon" /></span>
                <span class="cell-label">{{ item.label }}</span>
                <span v-if="item.phase > 1" class="cell-soon">规划中</span>
              </RouterLink>
            </div>
          </section>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.tabbar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 40;
  display: flex;
  height: calc(var(--tabbar-h) + env(safe-area-inset-bottom));
  padding-bottom: env(safe-area-inset-bottom);
  background: var(--topbar);
  border-top: 0.5px solid var(--border);
}
.tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  color: var(--text-3);
  font-size: 10.5px;
  font-weight: 500;
}
.tab svg {
  width: 21px;
  height: 21px;
}
.tab.on {
  color: var(--accent);
}
.sheet-wrap {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: var(--overlay);
  display: flex;
  align-items: flex-end;
}
.sheet {
  width: 100%;
  max-height: 82dvh;
  display: flex;
  flex-direction: column;
  border-radius: 18px 18px 0 0;
  background: color-mix(in srgb, var(--surface-raised) 92%, transparent);
  box-shadow: var(--shadow-lg);
  padding-bottom: env(safe-area-inset-bottom);
}
.sheet-head {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 48px;
  flex-shrink: 0;
  font-weight: 650;
}
.grabber {
  position: absolute;
  top: 6px;
  width: 36px;
  height: 5px;
  border-radius: 3px;
  background: var(--fill);
}
.close {
  position: absolute;
  right: 12px;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--fill-2);
  color: var(--text-2);
}
.close svg {
  width: 15px;
  height: 15px;
}
.sheet-body {
  overflow-y: auto;
  padding: 0 14px 18px;
}
.g-label {
  margin: 12px 4px 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(76px, 1fr));
  gap: 8px;
}
.cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 10px 4px;
  border-radius: 12px;
  color: var(--text);
  background: var(--fill-3);
  position: relative;
}
.cell-ic {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: var(--surface-solid);
  box-shadow: var(--shadow-sm), 0 0 0 0.5px var(--border);
}
.cell-ic svg {
  width: 19px;
  height: 19px;
}
.cell-label {
  font-size: 11.5px;
  text-align: center;
}
.cell-soon {
  position: absolute;
  top: 5px;
  right: 6px;
  font-size: 9px;
  color: var(--text-3);
}
.sheet-enter-active,
.sheet-leave-active {
  transition: opacity 0.25s;
}
.sheet-enter-active .sheet,
.sheet-leave-active .sheet {
  transition: transform 0.32s var(--ease-out);
}
.sheet-enter-from,
.sheet-leave-to {
  opacity: 0;
}
.sheet-enter-from .sheet,
.sheet-leave-to .sheet {
  transform: translateY(100%);
}
</style>
