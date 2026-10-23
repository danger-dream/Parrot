<script setup lang="ts">
defineProps<{ w?: string; h?: string; r?: string }>()
</script>

<template>
  <span class="sk" :style="{ width: w || '100%', height: h || '12px', borderRadius: r || '6px' }" />
</template>

<style scoped>
/* Stays invisible for fast loads, then breathes slowly in sync with every other placeholder. */
.sk {
  display: block;
  background: var(--fill-2);
  opacity: 0;
  animation:
    sk-in 0.35s ease-out 0.3s forwards,
    sk-breathe 2.4s ease-in-out 0.65s infinite alternate;
}
@keyframes sk-in {
  to {
    opacity: 0.85;
  }
}
@keyframes sk-breathe {
  from {
    opacity: 0.85;
  }
  to {
    opacity: 0.45;
  }
}
</style>
