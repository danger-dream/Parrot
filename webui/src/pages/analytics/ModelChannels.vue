<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, enc, type Schemas } from '@/api/client'
import { channelName } from '@/stores/catalog'
import { integer, percent } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import { goLogs, openChannel } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { periodStart, type Period } from './metrics'

// Telegram「按模型」视图里的“所属”一行：这个模型的请求实际落到了哪些渠道。

const props = defineProps<{ model: string; period: Period }>()
const data = ref<Schemas['ModelStatsData'] | null>(null)
const error = ref('')

onMounted(async () => {
  try {
    data.value = (await api.get<Schemas['ModelStatsData']>(`/stats/models/${enc(props.model)}`, { period: props.period })).data
  } catch (err) {
    error.value = errorTitle(err)
  }
})

const total = computed(() => (data.value?.channels || []).reduce((s, c) => s + c.count, 0))

const PROTO: Record<string, string> = {
  anthropic: 'Anthropic',
  'openai-chat': 'Chat',
  'openai-responses': 'Responses',
  chat: 'Chat',
  responses: 'Responses',
}
</script>

<template>
  <div class="mc">
    <p class="t">渠道分布</p>
    <p v-if="error" class="faint">{{ error }}</p>
    <div v-else-if="!data" class="skl"><Skeleton h="12px" w="60%" /><Skeleton h="12px" w="40%" /></div>
    <p v-else-if="!data.channels.length" class="faint">没有渠道记录</p>
    <ul v-else>
      <li v-for="c in data.channels" :key="c.key">
        <button type="button" class="ch truncate" :title="c.key" @click="openChannel(c.key)">{{ channelName(c.key) }}</button>
        <Badge v-if="c.type" :tone="c.type === 'oauth' ? 'orange' : 'green'">{{ c.type === 'oauth' ? 'OAuth' : 'API' }}</Badge>
        <Badge v-if="c.upstreamProtocol">{{ PROTO[c.upstreamProtocol] || c.upstreamProtocol }}</Badge>
        <span class="spacer" />
        <span class="mono n">{{ integer(c.count) }} 次</span>
        <span class="mono p">{{ percent(c.count, total) }}</span>
        <button type="button" class="lk" @click="goLogs({ model, channel: c.key, from: periodStart(period) })">查看请求</button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.mc {
  padding: 8px 14px 12px 40px;
  border-bottom: 0.5px solid var(--hairline);
  background: var(--fill-3);
}
.t {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  margin-bottom: 4px;
}
ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
li {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 28px;
  font-size: 12px;
}
.ch {
  text-align: left;
  max-width: 50%;
}
.ch:hover,
.lk:hover {
  color: var(--accent);
}
.spacer {
  flex: 1;
}
.n {
  color: var(--text-2);
}
.p {
  width: 36px;
  text-align: right;
  color: var(--text-3);
}
.lk {
  font-size: 11.5px;
  color: var(--accent);
  white-space: nowrap;
}
.skl {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
@media (max-width: 767px) {
  .mc {
    padding-left: 14px;
  }
  li {
    flex-wrap: wrap;
  }
  .ch {
    max-width: 100%;
  }
}
</style>
