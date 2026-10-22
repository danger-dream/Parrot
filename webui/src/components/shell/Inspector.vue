<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { X } from '@lucide/vue'
import { closeInspector, inspector, ui } from '@/stores/ui'

const mode = computed(() => (ui.width >= 1280 ? 'docked' : ui.width >= 768 ? 'overlay' : 'sheet'))

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape' && inspector.open && !ui.paletteOpen) closeInspector()
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Transition name="backdrop">
    <div v-if="inspector.open && mode !== 'docked'" class="backdrop" @click="closeInspector" />
  </Transition>
  <aside class="inspector" :class="[mode, { open: inspector.open }]" :aria-hidden="!inspector.open">
    <div class="inner">
      <header class="head">
        <div class="titles">
          <p class="title">{{ inspector.title }}</p>
          <p v-if="inspector.subtitle" class="sub mono">{{ inspector.subtitle }}</p>
        </div>
        <button class="close" aria-label="关闭" @click="closeInspector"><X /></button>
      </header>
      <div class="body">
        <component :is="inspector.component" v-if="inspector.component && inspector.open" :key="inspector.key" v-bind="inspector.props" />
      </div>
    </div>
  </aside>
</template>

<style scoped>
.inspector {
  display: flex;
  flex-direction: column;
  background: var(--bg-elev);
  z-index: 50;
}
.inner {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
}
.inspector.docked {
  position: relative;
  height: 100%;
  width: 0;
  flex-shrink: 0;
  overflow: hidden;
  border-left: 0.5px solid transparent;
  transition: width 0.34s var(--ease-out), border-color 0.34s;
}
.inspector.docked .inner {
  width: var(--inspector-w);
}
.inspector.docked.open {
  width: var(--inspector-w);
  border-left-color: var(--border);
}
.inspector.overlay {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(var(--inspector-w), 92vw);
  box-shadow: var(--shadow-lg);
  transform: translateX(104%);
  transition: transform 0.34s var(--ease-out);
}
.inspector.overlay.open {
  transform: none;
}
.inspector.sheet {
  position: fixed;
  inset: 0;
  top: env(safe-area-inset-top);
  border-radius: 14px 14px 0 0;
  transform: translateY(102%);
  transition: transform 0.36s var(--ease-out);
  padding-bottom: env(safe-area-inset-bottom);
}
.inspector.sheet.open {
  transform: none;
}
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--topbar-h);
  padding: 0 12px 0 18px;
  flex-shrink: 0;
  border-bottom: 0.5px solid var(--border);
}
.titles {
  flex: 1;
  min-width: 0;
}
.title {
  font-weight: 650;
  font-size: 13.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sub {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.close {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  color: var(--text-2);
  background: var(--fill-2);
}
.close:hover {
  background: var(--fill);
}
.close svg {
  width: 15px;
  height: 15px;
}
.body {
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.backdrop {
  position: fixed;
  inset: 0;
  z-index: 45;
  background: var(--overlay);
}
.backdrop-enter-active,
.backdrop-leave-active {
  transition: opacity 0.25s;
}
.backdrop-enter-from,
.backdrop-leave-to {
  opacity: 0;
}
</style>
