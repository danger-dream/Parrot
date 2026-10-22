<script setup lang="ts">
withDefaults(defineProps<{ size?: number }>(), { size: 26 })
</script>

<template>
  <span class="mark" :style="{ width: `${size}px`, height: `${size}px`, fontSize: `${size * 0.62}px` }" aria-hidden="true">
    <span>🦜</span>
  </span>
</template>

<style scoped>
.mark {
  display: inline-grid;
  place-items: center;
  flex-shrink: 0;
  border-radius: 28%;
  background: radial-gradient(120% 120% at 20% 10%, #3ee07a 0%, #13b5a6 45%, #0a6cff 100%);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.35), inset 0 -1px 0 rgba(0, 0, 0, 0.15), 0 4px 12px -4px rgba(10, 108, 255, 0.55);
}
.mark span {
  line-height: 1;
  transform: translateY(1px);
  filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.25));
}
</style>
