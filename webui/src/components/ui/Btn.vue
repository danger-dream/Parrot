<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'telegram'
    size?: 'sm' | 'md' | 'lg'
    loading?: boolean
    disabled?: boolean
    block?: boolean
    icon?: boolean
    type?: 'button' | 'submit'
  }>(),
  { variant: 'secondary', size: 'md', type: 'button' },
)
</script>

<template>
  <button
    :type="type"
    class="btn"
    :class="[`v-${variant}`, `s-${size}`, { block, icon, loading }]"
    :disabled="disabled || loading"
  >
    <span v-if="loading" class="spinner" aria-hidden="true" />
    <slot />
  </button>
</template>

<style scoped>
.btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 28px;
  padding: 0 11px;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  font-weight: 500;
  white-space: nowrap;
  user-select: none;
  transition: background 0.15s, color 0.15s, box-shadow 0.15s, transform 0.12s, opacity 0.15s;
}
.btn:active:not(:disabled) {
  transform: scale(0.97);
}
.btn:disabled {
  opacity: 0.45;
}
.btn :deep(svg) {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
.s-sm {
  height: 24px;
  padding: 0 8px;
  font-size: 12px;
  border-radius: 6px;
}
.s-lg {
  height: 40px;
  padding: 0 18px;
  font-size: 14px;
  border-radius: var(--radius-md);
}
.s-lg :deep(svg) {
  width: 17px;
  height: 17px;
}
.icon {
  width: 28px;
  padding: 0;
}
.icon.s-sm {
  width: 24px;
}
.block {
  display: flex;
  width: 100%;
}
.v-primary {
  background: var(--accent);
  color: #fff;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.18), 0 1px 2px rgba(0, 0, 0, 0.2);
}
.v-primary:hover:not(:disabled) {
  filter: brightness(1.08);
}
.v-secondary {
  background: var(--fill-2);
  color: var(--text);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.v-secondary:hover:not(:disabled) {
  background: var(--fill);
}
.v-ghost {
  color: var(--text-2);
}
.v-ghost:hover:not(:disabled) {
  background: var(--fill-2);
  color: var(--text);
}
.v-danger {
  background: color-mix(in srgb, var(--red) 14%, transparent);
  color: var(--red);
}
.v-danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--red) 22%, transparent);
}
.v-telegram {
  background: linear-gradient(180deg, #37b5f3, #229ed9);
  color: #fff;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.25), 0 6px 20px -6px rgba(34, 158, 217, 0.6);
}
.v-telegram:hover:not(:disabled) {
  filter: brightness(1.06);
}
.loading {
  color: transparent !important;
}
.loading :deep(svg) {
  opacity: 0;
}
.spinner {
  position: absolute;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid currentColor;
  border-color: rgba(255, 255, 255, 0.9) rgba(255, 255, 255, 0.25) rgba(255, 255, 255, 0.25);
  animation: spin 0.7s linear infinite;
}
.v-secondary .spinner,
.v-ghost .spinner {
  border-color: var(--text-2) var(--fill) var(--fill);
}
</style>
