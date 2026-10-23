<script setup lang="ts">
import { computed } from 'vue'
import { openChannel, openModel } from '@/links'
import { channelName } from '@/stores/catalog'

// Clickable channel / model reference (opens the shared inspector).
const props = defineProps<{ kind: 'channel' | 'model'; id: string; label?: string }>()
const text = computed(() => props.label || (props.kind === 'channel' ? channelName(props.id) : props.id))

function open(): void {
  if (props.kind === 'channel') openChannel(props.id)
  else openModel(props.id)
}
</script>

<template>
  <button type="button" class="lnk truncate" :class="{ mono: kind === 'model' }" :title="id" @click.stop="open">{{ text }}</button>
</template>

<style scoped>
.lnk {
  max-width: 100%;
  min-width: 0;
  color: var(--accent);
  text-align: left;
}
.lnk:hover {
  text-decoration: underline;
}
</style>
