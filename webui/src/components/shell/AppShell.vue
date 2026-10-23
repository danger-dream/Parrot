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
import DialogHost from '@/components/ui/DialogHost.vue'

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
          <component :is="Component" :key="route.path" />
        </RouterView>
      </main>
    </div>
    <Inspector />
    <TabBar v-if="mobile" />
    <CommandPalette />
    <DialogHost />
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
    radial-gradient(760px 520px at 8% -6%, var(--bg-glow-1), transparent 62%),
    radial-gradient(680px 480px at 100% 4%, var(--bg-glow-2), transparent 60%),
    radial-gradient(820px 600px at 70% 112%, var(--bg-glow-3), transparent 62%),
    var(--bg-gradient);
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
}
.mobile .content {
  padding-bottom: calc(var(--tabbar-h) + env(safe-area-inset-bottom));
}
</style>
