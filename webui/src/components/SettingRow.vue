<script setup lang="ts">
defineProps<{ title: string; hint?: string }>()
</script>

<template>
  <div class="sr">
    <div class="sr-txt">
      <p>{{ title }}</p>
      <p v-if="hint" class="sr-hint">{{ hint }}</p>
    </div>
    <div class="sr-ctl"><slot /></div>
  </div>
</template>

<style scoped>
/* Rows may be teleported in from provider panels, so separators cannot rely on siblings:
   every row draws its own top line and the container (overflow: hidden) clips the first. */
.sr {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 10px 12px;
  margin-top: -0.5px;
  border-top: 0.5px solid var(--hairline);
}
.sr-txt {
  min-width: 0;
  font-size: 13px;
}
.sr-hint {
  margin-top: 1px;
  font-size: 11.5px;
  color: var(--text-3);
}
.sr-ctl {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
</style>
