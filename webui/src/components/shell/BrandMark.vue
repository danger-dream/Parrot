<script setup lang="ts">
import { Bird } from '@lucide/vue'

withDefaults(defineProps<{ size?: number }>(), { size: 26 })
</script>

<template>
  <span class="mark" :style="{ width: `${size}px`, height: `${size}px` }" aria-hidden="true">
    <Bird :style="{ width: `${size * 0.62}px`, height: `${size * 0.62}px` }" :stroke-width="2.1" />
  </span>
</template>

<style scoped>
.mark {
  display: inline-grid;
  place-items: center;
  flex-shrink: 0;
  border-radius: 24%;
  color: #fff;
  background: linear-gradient(160deg, #3fd47a 0%, #12a36a 100%);
  box-shadow: inset 0 0.5px 0 rgba(255, 255, 255, 0.45), 0 1px 2px rgba(0, 0, 0, 0.18);
}
</style>
