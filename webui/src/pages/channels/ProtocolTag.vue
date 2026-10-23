<script setup lang="ts">
import { computed } from 'vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { protocolInfo } from './meta'

const props = defineProps<{ protocol: string | null | undefined }>()
const info = computed(() => protocolInfo(props.protocol))
</script>

<template>
  <span class="pt" :title="info ? `${info.label} · ${info.path}` : '未知协议'">
    <ProviderIcon v-if="info" :provider="info.family" :size="15" :title="info.label" />
    <span>{{ info?.api || '未知协议' }}</span>
  </span>
</template>

<style scoped>
.pt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  white-space: nowrap;
}
</style>
