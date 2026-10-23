<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { DatabaseZap, RefreshCw } from '@lucide/vue'
import { api } from '@/api/client'
import { channelName } from '@/stores/catalog'
import { inspector } from '@/stores/ui'
import { compact, dateTime } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import { openRequest } from '@/links'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { PERIOD_LONG, type Period } from './metrics'

// TG「最近未命中样本」：listCacheMissSamples 返回最近最多 3 条成功但没有读取缓存的请求。

/** `GET /stats/cache-misses` (round 2) is not in the generated schema yet. */
interface Sample {
  id: string
  createdAt: string | null
  model: string | null
  apiKeyName: string | null
  channelId: string | null
  inputTokens: number
  cacheCreationTokens: number
  outputTokens: number
  messageCount: number
  toolCount: number
}

const props = defineProps<{ period: Period }>()
const items = ref<Sample[] | null>(null)
const loading = ref(false)
const error = ref('')

let seq = 0
async function load(): Promise<void> {
  const my = ++seq
  loading.value = true
  try {
    const res = await api.get<Sample[]>('/stats/cache-misses', { period: props.period })
    if (my !== seq) return
    items.value = res.data
    error.value = ''
  } catch (err) {
    if (my === seq) error.value = errorTitle(err)
  } finally {
    if (my === seq) loading.value = false
  }
}
onMounted(load)
watch(() => props.period, load)
defineExpose({ reload: load })
</script>

<template>
  <Card title="最近未命中样本" :subtitle="`${PERIOD_LONG[period]} · 成功但没有读取缓存的最近请求，点击查看请求详情`" flush>
    <template #icon><DatabaseZap /></template>
    <template #actions>
      <Btn icon size="sm" variant="ghost" title="刷新" :loading="loading" @click="load"><RefreshCw /></Btn>
    </template>
    <p v-if="error" class="state">{{ error }}</p>
    <div v-else-if="!items" class="skl"><Skeleton v-for="i in 3" :key="i" h="38px" r="8px" /></div>
    <p v-else-if="!items.length" class="state">这段时间没有未命中缓存的成功请求</p>
    <ul v-else class="list" :class="{ dim: loading }">
      <li
        v-for="s in items"
        :key="s.id"
        :class="{ sel: inspector.open && inspector.key === `req:${s.id}` }"
        role="button"
        tabindex="0"
        @click="openRequest(s.id)"
        @keydown.enter="openRequest(s.id)"
      >
        <p class="l1">
          <span v-if="s.model" class="mono truncate">{{ s.model }}</span>
          <span v-else class="faint">—</span>
          <span class="time mono">{{ dateTime(s.createdAt) }}</span>
        </p>
        <p class="l2">
          <span class="tok mono" title="输入 Token · 缓存写入 Token · 输出 Token">↑{{ compact(s.inputTokens) }} · 写 {{ compact(s.cacheCreationTokens) }} · ↓{{ compact(s.outputTokens) }}</span>
          <span class="faint">{{ s.messageCount }} 条消息<template v-if="s.toolCount"> · {{ s.toolCount }} 个工具</template></span>
        </p>
        <p class="l3">
          <span v-if="s.channelId" class="truncate" :title="s.channelId">{{ channelName(s.channelId) }}</span>
          <span v-if="s.apiKeyName" class="key truncate">{{ s.apiKeyName }}</span>
        </p>
      </li>
    </ul>
  </Card>
</template>

<style scoped>
.state {
  padding: 24px 16px;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-3);
  border-top: 0.5px solid var(--hairline);
}
.skl {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 0.5px solid var(--hairline);
  transition: opacity 0.2s;
}
.list.dim {
  opacity: 0.55;
}
li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 14px;
  border-bottom: 0.5px solid var(--hairline);
  cursor: pointer;
}
li:last-child {
  border-bottom: 0;
}
li:hover {
  background: var(--fill-3);
}
li.sel {
  background: var(--accent-soft);
}
.l1,
.l2,
.l3 {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  font-size: 12.5px;
}
.l1 .lk {
  font-weight: 550;
}
.time {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-3);
}
.l2 {
  font-size: 11.5px;
}
.tok {
  color: var(--text-2);
}
.l3 {
  font-size: 11.5px;
  color: var(--text-2);
}
.key {
  color: var(--text-3);
}
</style>
