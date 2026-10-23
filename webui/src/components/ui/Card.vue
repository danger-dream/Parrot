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
  height: 100%;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
}
.card.interactive {
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
}
.card.interactive:hover {
  box-shadow: var(--shadow-md);
}
.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 16px 18px 0;
}
/* The titles give way first, so the actions stay in the top-right corner; they only wrap below on narrow screens. */
.card-titles {
  flex: 1 1 200px;
  min-width: 0;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.card-title :deep(svg) {
  width: 16px;
  height: 16px;
  color: var(--text-2);
}
.card-sub {
  margin-top: 3px;
  font-size: 12px;
  color: var(--text-3);
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.card-body {
  padding: 14px 18px 18px;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.card.flush .card-body {
  padding: 0;
}
.card.flush .card-head {
  padding-bottom: 14px;
}
</style>
