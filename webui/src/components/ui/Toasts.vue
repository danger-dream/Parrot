<script setup lang="ts">
import { CircleCheck, CircleX, Info, TriangleAlert, X } from '@lucide/vue'
import { dismissToast, toasts } from '@/stores/ui'

const icons = { info: Info, success: CircleCheck, warning: TriangleAlert, error: CircleX }
</script>

<template>
  <div class="toasts" aria-live="polite">
    <TransitionGroup name="toast">
      <div v-for="t in toasts" :key="t.id" class="toast glass" :class="`t-${t.tone}`">
        <component :is="icons[t.tone]" class="ic" />
        <div class="body">
          <p class="title">{{ t.title }}</p>
          <p v-if="t.detail" class="detail">{{ t.detail }}</p>
        </div>
        <button class="close" aria-label="关闭" @click="dismissToast(t.id)"><X /></button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed;
  top: calc(var(--topbar-h) + 10px);
  right: 14px;
  z-index: 90;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: min(360px, calc(100vw - 28px));
  pointer-events: none;
}
.toast {
  pointer-events: auto;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 11px 12px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--surface-raised) 86%, transparent);
  box-shadow: var(--shadow-lg);
}
.ic {
  width: 17px;
  height: 17px;
  flex-shrink: 0;
  margin-top: 1px;
}
.t-info .ic { color: var(--blue); }
.t-success .ic { color: var(--green); }
.t-warning .ic { color: var(--orange); }
.t-error .ic { color: var(--red); }
.body {
  flex: 1;
  min-width: 0;
}
.title {
  font-weight: 600;
  font-size: 12.5px;
}
.detail {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-2);
  word-break: break-word;
}
.close {
  color: var(--text-3);
  display: flex;
}
.close svg {
  width: 14px;
  height: 14px;
}
.toast-enter-active,
.toast-leave-active {
  transition: all 0.3s var(--ease-out);
}
.toast-enter-from {
  opacity: 0;
  transform: translateX(24px) scale(0.98);
}
.toast-leave-to {
  opacity: 0;
  transform: scale(0.96);
}
@media (max-width: 767px) {
  .toasts {
    top: auto;
    bottom: calc(var(--tabbar-h) + env(safe-area-inset-bottom) + 10px);
    right: 14px;
  }
}
</style>
