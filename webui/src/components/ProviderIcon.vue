<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { providerIconUrl, providerTile } from '@/utils/providers'

const props = withDefaults(defineProps<{ provider?: string | null; size?: number; title?: string }>(), {
  provider: '',
  size: 28,
  title: undefined,
})

const failed = ref(false)
watch(
  () => props.provider,
  () => (failed.value = false),
)

const url = computed(() => (failed.value ? '' : providerIconUrl(props.provider)))
const tile = computed(() => providerTile(props.provider))
</script>

<template>
  <span
    class="pi"
    :class="{ img: Boolean(url) }"
    :style="{ '--s': `${size}px`, '--c': tile.color }"
    :title="title ?? tile.label"
    role="img"
    :aria-label="title ?? tile.label"
  >
    <img v-if="url" :src="url" alt="" draggable="false" @error="failed = true" />
    <template v-else>{{ tile.glyph }}</template>
  </span>
</template>

<style scoped>
.pi {
  flex-shrink: 0;
  display: inline-grid;
  place-items: center;
  width: var(--s);
  height: var(--s);
  border-radius: calc(var(--s) * 0.28);
  background: var(--c);
  color: #fff;
  font-size: calc(var(--s) * 0.46);
  font-weight: 650;
  line-height: 1;
  user-select: none;
}
.pi.img {
  background: transparent;
  border-radius: calc(var(--s) * 0.22);
}
.pi img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}
</style>
