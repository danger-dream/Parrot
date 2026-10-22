<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { ui } from '@/stores/ui'
import { refreshSystem } from '@/stores/system'
import { refreshCatalog } from '@/stores/catalog'
import Sidebar from './Sidebar.vue'
import Topbar from './Topbar.vue'
import TabBar from './TabBar.vue'
import Inspector from './Inspector.vue'
import CommandPalette from './CommandPalette.vue'
import Toasts from '@/components/ui/Toasts.vue'

const mobile = computed(() => ui.width < 768)
const rail = computed(() => ui.width < 1280)

let timer: number | undefined
onMounted(() => {
  refreshSystem()
  refreshCatalog(true)
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    refreshSystem()
    refreshCatalog()
  }, 30_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <div class="shell" :class="{ mobile }">
    <div class="atmosphere" aria-hidden="true" />
    <Sidebar v-if="!mobile" :rail="rail" />
    <div class="main">
      <Topbar />
      <main class="content">
        <RouterView v-slot="{ Component, route }">
          <Transition name="page" mode="out-in">
            <component :is="Component" :key="route.path" />
          </Transition>
        </RouterView>
      </main>
    </div>
    <Inspector />
    <TabBar v-if="mobile" />
    <CommandPalette />
    <Toasts />
  </div>
</template>

<style scoped>
.shell {
  position: relative;
  display: flex;
  height: 100dvh;
  overflow: hidden;
  isolation: isolate;
}
.atmosphere {
  position: absolute;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background:
    radial-gradient(900px 600px at 0% 0%, var(--bg-glow-1), transparent 60%),
    radial-gradient(700px 500px at 100% 0%, var(--bg-glow-3), transparent 60%),
    radial-gradient(800px 600px at 60% 110%, var(--bg-glow-2), transparent 60%),
    var(--bg);
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  scroll-behavior: smooth;
}
.mobile .content {
  padding-bottom: calc(var(--tabbar-h) + env(safe-area-inset-bottom));
}
.page-enter-active,
.page-leave-active {
  transition: opacity 0.16s, transform 0.22s var(--ease-out);
}
.page-enter-from {
  opacity: 0;
  transform: translateY(4px);
}
.page-leave-to {
  opacity: 0;
}
</style>
