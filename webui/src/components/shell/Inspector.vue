<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { ChevronLeft, X } from '@lucide/vue'
import { backInspector, closeInspector, inspector, inspectorHistory, markInspectorInteraction, ui } from '@/stores/ui'

// Details float over the page (like a drawer) so opening one never reflows the list underneath.
const mode = computed(() => (ui.width >= 768 ? 'overlay' : 'sheet'))

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape' && inspector.open && !ui.paletteOpen) closeInspector()
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Transition name="backdrop">
    <div v-if="inspector.open" class="backdrop" @click="closeInspector" />
  </Transition>
  <aside class="inspector" :class="[mode, { open: inspector.open }]" :aria-hidden="!inspector.open" @pointerdown.capture="markInspectorInteraction">
    <div class="inner">
      <header class="head">
        <button v-if="inspectorHistory.length" class="back" :title="`返回 ${inspectorHistory[inspectorHistory.length - 1].title}`" @click="backInspector">
          <ChevronLeft />
        </button>
        <div class="titles">
          <p class="title">{{ inspector.title }}</p>
          <p v-if="inspector.subtitle" class="sub mono" :data-private="inspector.key.startsWith('account:') ? '' : undefined">{{ inspector.subtitle }}</p>
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
.inspector.overlay {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(var(--inspector-w), 92vw);
  border-left: 1px solid var(--border);
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
  border-bottom: 1px solid var(--border);
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
.back {
  width: 28px;
  height: 28px;
  margin-left: -6px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  color: var(--accent);
}
.back:hover {
  background: var(--accent-soft);
}
.back svg {
  width: 18px;
  height: 18px;
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
