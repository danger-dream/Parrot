<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LogOut, Monitor, MonitorSmartphone, Moon, Search, Sun, SunMoon } from '@lucide/vue'
import { eventLink } from '@/api/events'
import { logout, session } from '@/stores/session'
import { system, modeLabel } from '@/stores/system'
import { setTheme, ui, type ThemePref } from '@/stores/ui'
import { dateTime } from '@/utils/format'
import { routeTitle } from '@/router'
import BrandMark from './BrandMark.vue'
import StatusDot from '@/components/ui/StatusDot.vue'
import Segmented from '@/components/ui/Segmented.vue'

const route = useRoute()
const router = useRouter()
const menuOpen = ref(false)
const menuRef = ref<HTMLElement | null>(null)

const title = computed(() => routeTitle(route))
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
const themeIcon = computed(() => (ui.theme === 'auto' ? SunMoon : ui.theme === 'dark' ? Moon : Sun))
const themeOptions: { value: ThemePref; label: string }[] = [
  { value: 'auto', label: '自动' },
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
]
const authLabel = computed(() => {
  const m = session.summary?.authMethod
  return m === 'telegramApproval' ? 'Telegram 批准登录' : m === 'managementKey' ? '管理密钥登录' : '已登录'
})
const linkTone = computed(() => (eventLink.state === 'open' ? 'green' : eventLink.state === 'retrying' ? 'orange' : 'gray'))

function onDoc(e: MouseEvent): void {
  if (menuOpen.value && menuRef.value && !menuRef.value.contains(e.target as Node)) menuOpen.value = false
}
onMounted(() => document.addEventListener('mousedown', onDoc))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDoc))

async function signOut(): Promise<void> {
  menuOpen.value = false
  await logout()
  router.replace({ name: 'login' })
}

function goDevices(): void {
  menuOpen.value = false
  router.push('/devices')
}
</script>

<template>
  <header class="topbar glass">
    <div class="left">
      <BrandMark class="mobile-mark" :size="24" />
      <h1 class="title">{{ title }}</h1>
    </div>

    <div class="pill" :title="eventLink.state === 'open' ? '实时连接正常' : '实时连接中断'">
      <StatusDot :tone="linkTone" :pulse="eventLink.state === 'open'" :size="6" />
      <span class="seg"><span class="k">端口</span><span class="v mono">{{ system.overview?.listeners.port ?? '—' }}</span></span>
      <span class="seg hide-md"><span class="k">调度</span><span class="v tag">{{ modeLabel(system.loadBalancing?.mode) }}</span></span>
      <span v-if="system.overview?.publicIp" class="seg hide-lg"><span class="k">出口</span><span class="v mono">{{ system.overview.publicIp }}</span></span>
      <span v-if="system.overview?.update?.available" class="seg hide-md"><span class="v upd">有新版本 {{ system.overview.update.latestVersion }}</span></span>
    </div>

    <div class="right">
      <button class="search" @click="ui.paletteOpen = true">
        <Search />
        <span class="search-text">搜索</span>
        <kbd>{{ isMac ? '⌘' : 'Ctrl' }} K</kbd>
      </button>
      <div ref="menuRef" class="menu-wrap">
        <button class="avatar" :class="{ on: menuOpen }" aria-label="账户菜单" @click="menuOpen = !menuOpen">
          <component :is="themeIcon" class="avatar-theme" />
          <span>管</span>
        </button>
        <Transition name="pop">
          <div v-if="menuOpen" class="menu glass">
            <div class="menu-head">
              <p class="menu-title">管理员</p>
              <p class="menu-sub">{{ authLabel }}</p>
              <p v-if="session.summary" class="menu-sub">会话有效至 {{ dateTime(session.summary.idleExpiresAt) }}</p>
            </div>
            <div class="menu-sec">
              <p class="menu-label"><Monitor /> 外观</p>
              <Segmented :model-value="ui.theme" :options="themeOptions" size="sm" @update:model-value="setTheme" />
            </div>
            <button class="menu-item" @click="goDevices"><MonitorSmartphone /> 登录设备</button>
            <button class="menu-item danger" @click="signOut"><LogOut /> 退出登录</button>
          </div>
        </Transition>
      </div>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  position: relative;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--topbar-h);
  padding: 0 14px 0 20px;
  flex-shrink: 0;
  background: var(--topbar);
  border-bottom: 0.5px solid var(--border);
}
.left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
}
.mobile-mark {
  display: none;
}
.title {
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pill {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 28px;
  padding: 0 12px;
  border-radius: 999px;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
  font-size: 12px;
  white-space: nowrap;
  flex-shrink: 1;
  min-width: 0;
  overflow: hidden;
}
.seg {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.seg + .seg {
  padding-left: 10px;
  border-left: 0.5px solid var(--border-strong);
}
.k {
  color: var(--text-3);
}
.v {
  color: var(--text);
}
.tag {
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 11px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 14%, transparent);
}
.upd {
  color: var(--green);
}
.right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex: 1;
}
.search {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 28px;
  padding: 0 6px 0 9px;
  border-radius: var(--radius-sm);
  background: var(--fill-2);
  box-shadow: inset 0 0 0 0.5px var(--border);
  color: var(--text-2);
  font-size: 12px;
}
.search:hover {
  background: var(--fill);
}
.search svg {
  width: 13px;
  height: 13px;
}
.search-text {
  min-width: 56px;
  text-align: left;
}
kbd {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--fill);
  font-size: 10.5px;
  color: var(--text-2);
}
.menu-wrap {
  position: relative;
}
.avatar {
  position: relative;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 650;
  color: #fff;
  background: linear-gradient(135deg, #5e5ce6, #0a84ff);
  box-shadow: 0 0 0 0.5px var(--border-strong);
  transition: box-shadow 0.15s;
}
.avatar.on,
.avatar:hover {
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.avatar-theme {
  position: absolute;
  right: -3px;
  bottom: -3px;
  width: 13px;
  height: 13px;
  padding: 2px;
  border-radius: 50%;
  background: var(--surface-raised);
  color: var(--text-2);
  box-shadow: 0 0 0 0.5px var(--border-strong);
}
.menu {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: 252px;
  padding: 6px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--surface-raised) 90%, transparent);
  box-shadow: var(--shadow-lg);
}
.menu-head {
  padding: 8px 10px 10px;
  border-bottom: 0.5px solid var(--hairline);
}
.menu-title {
  font-weight: 650;
}
.menu-sub {
  font-size: 11.5px;
  color: var(--text-3);
  margin-top: 2px;
}
.menu-sec {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px;
  border-bottom: 0.5px solid var(--hairline);
}
.menu-label {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-2);
  font-size: 12px;
}
.menu-label svg,
.menu-item svg {
  width: 14px;
  height: 14px;
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 32px;
  margin-top: 4px;
  padding: 0 10px;
  border-radius: 6px;
  font-size: 13px;
  text-align: left;
}
.menu-item:hover {
  background: var(--fill-2);
}
.menu-item.danger {
  color: var(--red);
}
.pop-enter-active,
.pop-leave-active {
  transition: opacity 0.16s, transform 0.2s var(--ease-out);
  transform-origin: top right;
}
.pop-enter-from,
.pop-leave-to {
  opacity: 0;
  transform: scale(0.96) translateY(-4px);
}
@media (max-width: 1279px) {
  .hide-lg {
    display: none;
  }
}
@media (max-width: 1023px) {
  .hide-md {
    display: none;
  }
  .search-text,
  .search kbd {
    display: none;
  }
  .search {
    width: 28px;
    padding: 0;
    justify-content: center;
  }
}
@media (max-width: 767px) {
  .topbar {
    padding: 0 12px;
    padding-top: env(safe-area-inset-top);
    height: calc(var(--topbar-h) + env(safe-area-inset-top));
  }
  .mobile-mark {
    display: inline-grid;
  }
  .pill {
    display: none;
  }
}
</style>
