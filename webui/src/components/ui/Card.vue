<script setup lang="ts">
defineProps<{
  title?: string
  subtitle?: string
  flush?: boolean
  interactive?: boolean
}>()
</script>

<template>
  <section class="card" :class="{ flush, interactive }">
    <header v-if="title || $slots.header || $slots.actions" class="card-head">
      <slot name="header">
        <div class="card-titles">
          <h3 class="card-title">
            <slot name="icon" />
            <span>{{ title }}</span>
          </h3>
          <p v-if="subtitle" class="card-sub">{{ subtitle }}</p>
        </div>
      </slot>
      <div v-if="$slots.actions" class="card-actions"><slot name="actions" /></div>
    </header>
    <div class="card-body"><slot /></div>
  </section>
</template>

<style scoped>
.card {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--card-highlight), var(--shadow-sm);
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
}
.card.interactive {
  cursor: pointer;
  transition: background 0.2s, transform 0.2s var(--ease-out), border-color 0.2s;
}
.card.interactive:hover {
  background: var(--surface-hover);
  border-color: var(--border-strong);
}
.card.interactive:active {
  transform: scale(0.995);
}
.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 14px 16px 0;
}
.card-titles {
  min-width: 0;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.card-title :deep(svg) {
  width: 15px;
  height: 15px;
  color: var(--text-2);
}
.card-sub {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-3);
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.card-body {
  padding: 14px 16px 16px;
  flex: 1;
  min-height: 0;
}
.card.flush .card-body {
  padding: 0;
}
.card.flush .card-head {
  padding-bottom: 12px;
}
</style>
