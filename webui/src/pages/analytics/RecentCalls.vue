<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ChevronLeft, ChevronRight, History, RefreshCw } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { channelName } from '@/stores/catalog'
import { inspector } from '@/stores/ui'
import { dateTime, ms } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import { goLogs, openChannel, openModel, openRequest } from '@/links'
import { statusLabel, statusTone } from '@/pages/live/status'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

type Call = Schemas['RecentCallData']
const PAGE = 8

const items = ref<Call[]>([])
const page = ref(1)
const hasNext = ref(false)
const loading = ref(false)
const error = ref('')

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await api.get<Call[]>('/stats/recent-calls', { page: page.value, pageSize: PAGE })
    items.value = res.data
    hasNext.value = Boolean(res.meta.hasNext)
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}
onMounted(load)
defineExpose({ reload: load })

function go(d: number): void {
  page.value = Math.max(1, page.value + d)
  load()
}
</script>

<template>
  <Card title="最近调用" subtitle="所有渠道的最新请求，点击查看请求详情" flush>
    <template #icon><History /></template>
    <template #actions>
      <Btn icon size="sm" variant="ghost" title="刷新" :loading="loading" @click="load"><RefreshCw /></Btn>
      <Btn size="sm" variant="ghost" @click="goLogs()">全部请求</Btn>
    </template>
    <p v-if="error" class="state">{{ error }}</p>
    <div v-else class="list">
      <template v-if="loading && !items.length">
        <div v-for="i in 6" :key="i" class="skl"><Skeleton h="14px" /></div>
      </template>
      <Empty v-else-if="!items.length" title="还没有请求" />
      <div v-else :class="{ dim: loading }">
        <div
          v-for="c in items"
          :key="c.id"
          class="row"
          :class="[`t-${statusTone(c.status)}`, { sel: inspector.open && inspector.key === `req:${c.id}` }]"
          role="button"
          tabindex="0"
          @click="openRequest(c.id)"
          @keydown.enter="openRequest(c.id)"
        >
          <span class="time num">{{ dateTime(c.createdAt) }}</span>
          <span class="st"><i class="dot" />{{ statusLabel(c.status) }}</span>
          <button v-if="c.model" type="button" class="lk mono truncate model" @click.stop="openModel(c.model)">{{ c.model }}</button>
          <span v-else class="model faint">—</span>
          <button v-if="c.channelId" type="button" class="lk truncate ch" :title="c.channelId" @click.stop="openChannel(c.channelId)">{{ channelName(c.channelId) }}</button>
          <span v-else class="ch faint">{{ c.status === 'pending' ? '分配中…' : '—' }}</span>
          <span class="dur mono">{{ ms(c.durationMilliseconds) }}</span>
        </div>
      </div>
    </div>
    <div class="pager">
      <span class="faint">第 {{ page }} 页</span>
      <Btn icon size="sm" :disabled="page <= 1 || loading" @click="go(-1)"><ChevronLeft /></Btn>
      <Btn icon size="sm" :disabled="!hasNext || loading" @click="go(1)"><ChevronRight /></Btn>
    </div>
  </Card>
</template>

<style scoped>
.list {
  container-type: inline-size;
  border-top: 0.5px solid var(--hairline);
}
.row {
  display: grid;
  grid-template-columns: 112px 70px minmax(120px, 1.4fr) minmax(110px, 1.2fr) 70px;
  align-items: baseline;
  align-content: center;
  column-gap: 12px;
  min-height: 36px;
  padding: 0 14px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
  cursor: pointer;
}
.row:hover {
  background: var(--fill-3);
}
.row.sel {
  background: var(--accent-soft);
}
.t-green { --tone: var(--green); }
.t-red { --tone: var(--red); }
.t-orange { --tone: var(--orange); }
.t-blue { --tone: var(--blue); }
.t-neutral { --tone: var(--text-3); }
.time {
  font-size: 11.5px;
  color: var(--text-2);
}
.st {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--tone);
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--tone);
}
.lk {
  text-align: left;
}
.lk:hover {
  color: var(--accent);
}
.ch {
  color: var(--text-2);
}
.dur {
  text-align: right;
  font-size: 11.5px;
}
.skl {
  padding: 11px 14px;
  border-bottom: 0.5px solid var(--hairline);
}
.dim {
  opacity: 0.55;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 8px 14px 10px;
  font-size: 12px;
}
.state {
  padding: 40px 16px;
  text-align: center;
  color: var(--text-3);
}
@container (max-width: 560px) {
  .row {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas: 'model st' 'ch dur' 'time time';
    row-gap: 2px;
    padding: 8px 12px;
  }
  .model { grid-area: model; }
  .st { grid-area: st; justify-self: end; }
  .ch { grid-area: ch; }
  .dur { grid-area: dur; }
  .time { grid-area: time; font-size: 11px; color: var(--text-3); }
}
</style>
