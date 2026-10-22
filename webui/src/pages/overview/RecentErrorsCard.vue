<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { CircleCheck, CircleX } from '@lucide/vue'
import { api } from '@/api/client'
import { onEvent } from '@/api/events'
import type { RequestLogData } from '@/api/types'
import { channelName } from '@/stores/catalog'
import { relative } from '@/utils/format'
import { openRequest } from '@/pages/live/openRequest'
import Card from '@/components/ui/Card.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

const items = ref<RequestLogData[]>([])
const loading = ref(true)
const now = ref(Date.now())
let t: number | undefined

async function load(): Promise<void> {
  try {
    const res = await api.get<RequestLogData[]>('/logs', { status: 'error', pageSize: 6, sort: 'createdAt', descending: true })
    items.value = res.data
  } finally {
    loading.value = false
  }
}

const off = onEvent('request.finished', ({ log }) => {
  if (log.status !== 'error') return
  items.value = [log, ...items.value.filter((i) => i.id !== log.id)].slice(0, 6)
})

onMounted(() => {
  load()
  t = window.setInterval(() => (now.value = Date.now()), 15_000)
})
onBeforeUnmount(() => {
  off()
  window.clearInterval(t)
})
defineExpose({ reload: load })
</script>

<template>
  <Card title="最近错误">
    <template #icon><CircleX /></template>
    <div v-if="loading" class="sk"><Skeleton v-for="i in 4" :key="i" h="38px" r="8px" /></div>
    <div v-else-if="!items.length" class="ok"><CircleCheck /> 最近没有失败的请求</div>
    <ul v-else class="list">
      <li v-for="e in items" :key="e.id">
        <button @click="openRequest(e.id)">
          <div class="l1">
            <span class="mono truncate model">{{ e.requestedModel || '—' }}</span>
            <span class="when">{{ relative(e.createdAt, now) }}</span>
          </div>
          <p class="msg truncate">{{ e.error || '未知错误' }}</p>
          <p class="meta truncate">{{ channelName(e.channelId) }} · {{ e.apiKeyName || '—' }}</p>
        </button>
      </li>
    </ul>
  </Card>
</template>

<style scoped>
.list {
  list-style: none;
  margin: 0 -8px;
  padding: 0;
}
.list button {
  width: 100%;
  text-align: left;
  padding: 7px 8px;
  border-radius: 8px;
}
.list button:hover {
  background: var(--fill-3);
}
.l1 {
  display: flex;
  align-items: center;
  gap: 8px;
}
.model {
  flex: 1;
  font-size: 12.5px;
}
.when {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}
.msg {
  font-size: 12px;
  color: var(--red);
  margin-top: 1px;
}
.meta {
  font-size: 11px;
  color: var(--text-3);
}
.ok {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 20px 0;
  color: var(--text-2);
}
.ok svg {
  width: 18px;
  height: 18px;
  color: var(--green);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
