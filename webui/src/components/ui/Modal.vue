<script lang="ts">
// Escape closes only the most recently opened dialog.
const openStack: symbol[] = []
const handlers = new Map<symbol, () => void>()
let listening = false

function onKey(e: KeyboardEvent): void {
  if (e.key !== 'Escape' || !openStack.length) return
  e.stopImmediatePropagation()
  e.preventDefault()
  handlers.get(openStack[openStack.length - 1])?.()
}

function pushDialog(id: symbol, onEscape: () => void): void {
  handlers.set(id, onEscape)
  openStack.push(id)
  if (!listening) {
    window.addEventListener('keydown', onKey, true)
    listening = true
  }
}

function popDialog(id: symbol): void {
  handlers.delete(id)
  const i = openStack.indexOf(id)
  if (i >= 0) openStack.splice(i, 1)
}
</script>

<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import { X } from '@lucide/vue'

const props = withDefaults(
  defineProps<{ open: boolean; title?: string; subtitle?: string; width?: 'sm' | 'md' | 'lg' | 'xl'; dismissible?: boolean; layer?: number }>(),
  { width: 'md', dismissible: true, layer: 85 },
)
const emit = defineEmits<{ close: [] }>()

const dialogId = Symbol('modal')
watch(
  () => props.open,
  (open) => {
    if (open) pushDialog(dialogId, () => props.dismissible && emit('close'))
    else popDialog(dialogId)
  },
  { immediate: true },
)
onBeforeUnmount(() => popDialog(dialogId))
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="wrap" :style="{ zIndex: layer }" @mousedown.self="dismissible && emit('close')">
        <div class="modal glass" :class="`w-${width}`" role="dialog" aria-modal="true">
          <header v-if="title || $slots.header" class="head">
            <slot name="header">
              <div class="titles">
                <p class="title">{{ title }}</p>
                <p v-if="subtitle" class="sub">{{ subtitle }}</p>
              </div>
            </slot>
            <button v-if="dismissible" class="close" aria-label="关闭" @click="emit('close')"><X /></button>
          </header>
          <div class="body"><slot /></div>
          <footer v-if="$slots.footer" class="foot"><slot name="footer" /></footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.wrap {
  position: fixed;
  inset: 0;
  z-index: 85;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--overlay);
}
.modal {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-height: min(86dvh, 860px);
  border-radius: 16px;
  background: var(--popover);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.w-sm { max-width: 400px; }
.w-md { max-width: 520px; }
.w-lg { max-width: 720px; }
.w-xl { max-width: 960px; }
.head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 18px 10px 22px;
  flex-shrink: 0;
}
.titles {
  flex: 1;
  min-width: 0;
}
.title {
  font-size: 15px;
  font-weight: 650;
}
.sub {
  margin-top: 3px;
  font-size: 12.5px;
  color: var(--text-2);
}
.close {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  color: var(--text-2);
  background: var(--fill-2);
}
.close svg {
  width: 14px;
  height: 14px;
}
.body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 6px 22px 18px;
}
.foot {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 18px 16px;
  border-top: 0.5px solid var(--hairline);
  flex-shrink: 0;
}
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s;
}
.modal-enter-active .modal,
.modal-leave-active .modal {
  transition: transform 0.28s var(--ease-out), opacity 0.2s;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
.modal-enter-from .modal,
.modal-leave-to .modal {
  transform: scale(0.97) translateY(6px);
  opacity: 0;
}
@media (max-width: 767px) {
  .wrap {
    align-items: flex-end;
    padding: 0;
  }
  .modal {
    max-width: none;
    max-height: 90dvh;
    border-radius: 18px 18px 0 0;
    padding-bottom: env(safe-area-inset-bottom);
  }
  .modal-enter-from .modal,
  .modal-leave-to .modal {
    transform: translateY(100%);
  }
}
</style>
