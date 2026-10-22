<script setup lang="ts">
defineProps<{ title: string; detail?: string }>()
</script>

<template>
  <div class="empty">
    <div class="icon"><slot name="icon" /></div>
    <p class="title">{{ title }}</p>
    <p v-if="detail" class="detail">{{ detail }}</p>
    <div v-if="$slots.default" class="actions"><slot /></div>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 32px 16px;
  text-align: center;
}
.icon :deep(svg) {
  width: 26px;
  height: 26px;
  color: var(--text-3);
  margin-bottom: 4px;
}
.title {
  font-weight: 600;
  color: var(--text-2);
}
.detail {
  font-size: 12px;
  color: var(--text-3);
  max-width: 320px;
}
.actions {
  margin-top: 8px;
}
</style>
