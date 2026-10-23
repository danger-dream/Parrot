<script setup lang="ts">
import full from '@/assets/brand/parrot.png'
import head from '@/assets/brand/parrot-head.png'

// Small marks (sidebar, top bar) show the head only, which stays readable at 24 px; larger ones the whole bird.
withDefaults(defineProps<{ size?: number }>(), { size: 26 })
</script>

<template>
  <img class="mark" :src="size <= 40 ? head : full" :width="size" :height="size" alt="" aria-hidden="true" draggable="false" />
</template>

<style scoped>
.mark {
  display: block;
  flex-shrink: 0;
  border-radius: 24%;
  object-fit: cover;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.18);
  user-select: none;
}
</style>
