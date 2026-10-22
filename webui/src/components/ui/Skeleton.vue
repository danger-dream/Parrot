<script setup lang="ts">
defineProps<{ w?: string; h?: string; r?: string }>()
</script>

<template>
  <span class="sk" :style="{ width: w || '100%', height: h || '12px', borderRadius: r || '6px' }" />
</template>

<style scoped>
.sk {
  display: block;
  background: linear-gradient(90deg, var(--fill-3) 0%, var(--fill) 50%, var(--fill-3) 100%) var(--fill-3);
  background-size: 400px 100%;
  background-repeat: no-repeat;
  animation: shimmer 1.3s linear infinite;
}
</style>
