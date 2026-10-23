<script setup lang="ts">
withDefaults(defineProps<{ title?: string; subtitle?: string; width?: 'narrow' | 'normal' | 'wide' | 'full' }>(), { width: 'wide' })
</script>

<template>
  <div class="page" :class="`w-${width}`">
    <header v-if="title || $slots.actions || $slots.header" class="head">
      <slot name="header">
        <div class="titles">
          <h2>{{ title }}</h2>
          <p v-if="subtitle" class="sub">{{ subtitle }}</p>
        </div>
      </slot>
      <div v-if="$slots.actions" class="actions"><slot name="actions" /></div>
    </header>
    <div v-if="$slots.toolbar" class="toolbar"><slot name="toolbar" /></div>
    <div class="body"><slot /></div>
  </div>
</template>

<style scoped>
.page {
  container-type: inline-size;
  margin: 0 auto;
  padding: 22px 28px 40px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.w-narrow { max-width: 900px; }
.w-normal { max-width: 1200px; }
.w-wide { max-width: 1720px; }
.w-full { max-width: none; }
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px 16px;
}
.titles {
  min-width: 0;
}
h2 {
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.sub {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-2);
  max-width: 760px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.body {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
@media (max-width: 767px) {
  .page {
    padding: 14px 12px 28px;
    gap: 12px;
  }
  h2 {
    font-size: 20px;
  }
}
</style>
