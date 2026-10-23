<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Database, RefreshCw, Trash2, TriangleAlert } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { runAction } from '@/composables/useResource'
import { confirmAction } from '@/stores/dialogs'
import { errorTitle } from '@/utils/errors'
import { humanSeconds } from '@/utils/format'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Pager from '@/components/ui/Pager.vue'

type Entry = Schemas['DnsCacheEntryData']
const items = ref<Entry[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loading = ref(true)
const error = ref('')
const clearing = ref(false)

async function load(p = page.value): Promise<void> {
  loading.value = true
  try {
    const res = await api.get<{ items: Entry[] }>('/network/dns/cache', { page: p, pageSize })
    items.value = res.data.items
    total.value = res.meta.total ?? res.data.items.length
    page.value = p
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}
onMounted(() => load(1))

async function clear(): Promise<void> {
  const ok = await confirmAction({
    title: '清空 DNS 缓存？',
    detail: `将删除全部 ${total.value} 条缓存记录。之后的请求会重新解析域名，首次访问可能稍慢。`,
    confirmText: '清空',
    danger: true,
  })
  if (!ok) return
  clearing.value = true
  const res = await runAction(() => api.del('/network/dns/cache'), 'DNS 缓存已清空')
  clearing.value = false
  if (res) load(1)
}

defineExpose({ reload: () => load(1) })
</script>

<template>
  <Card title="DNS 缓存" :subtitle="`已解析过的域名会缓存一段时间，减少重复查询。当前 ${total} 条。`">
    <template #icon><Database /></template>
    <template #actions>
      <Btn size="sm" variant="ghost" :loading="loading" @click="load()"><RefreshCw /> 刷新</Btn>
      <Btn size="sm" variant="danger" :disabled="!total" :loading="clearing" @click="clear"><Trash2 /> 清空</Btn>
    </template>
    <p v-if="error" class="err"><TriangleAlert /> {{ error }}</p>
    <div v-else-if="loading && !items.length" class="sk"><Skeleton v-for="i in 3" :key="i" h="30px" /></div>
    <Empty v-else-if="!items.length" title="缓存为空" detail="有请求访问上游后，解析结果会出现在这里。" />
    <template v-else>
      <div class="tbl">
        <div class="tr th"><span>域名</span><span>类型</span><span>解析结果</span><span>剩余</span></div>
        <div v-for="e in items" :key="e.host + e.family" class="tr">
          <span class="host mono truncate" :title="e.host">{{ e.host }}</span>
          <span class="fam">{{ e.family === 6 ? 'IPv6' : e.family === 4 ? 'IPv4' : '自动' }}</span>
          <span class="ips mono">{{ e.ips.join(', ') || '—' }}</span>
          <span class="ttl">{{ humanSeconds(e.ttlRemainingSeconds) }}</span>
        </div>
      </div>
      <Pager :page="page" :page-size="pageSize" :total="total" :disabled="loading" @update:page="load" />
    </template>
  </Card>
</template>

<style scoped>
.tbl {
  display: flex;
  flex-direction: column;
}
.tr {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) 56px minmax(0, 2fr) 80px;
  align-items: center;
  column-gap: 12px;
  min-height: 34px;
  padding: 4px 0;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.th {
  min-height: 26px;
  font-size: 11px;
  color: var(--text-3);
}
.fam,
.ttl {
  font-size: 12px;
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
}
.ips {
  font-size: 11.5px;
  color: var(--text-2);
  overflow-wrap: anywhere;
}
.err {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-2);
}
.err svg {
  width: 14px;
  height: 14px;
  color: var(--orange);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
@container (max-width: 640px) {
  .th {
    display: none;
  }
  .tr {
    grid-template-columns: minmax(0, 1fr) auto;
    row-gap: 2px;
    padding: 6px 0;
  }
  .fam {
    display: none;
  }
  .ips {
    grid-column: 1 / -1;
    grid-row: 2;
  }
}
</style>
