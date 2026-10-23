<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ArrowUpRight, History, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { goPage } from '@/links'
import { errorTitle } from '@/utils/errors'
import Page from '@/components/ui/Page.vue'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Pager from '@/components/ui/Pager.vue'
import DnsCard from './DnsCard.vue'
import DnsCacheCard from './DnsCacheCard.vue'
import MonitorSection from './MonitorSection.vue'
import ChecksList from './ChecksList.vue'
import type { NetworkSettings } from './types'

type Check = Schemas['NetworkCheckData']

const net = ref<NetworkSettings | null>(null)
const netError = ref('')
async function loadNet(): Promise<void> {
  try {
    net.value = (await api.get<NetworkSettings>('/network')).data
    netError.value = ''
  } catch (err) {
    netError.value = errorTitle(err)
  }
}

const checks = ref<Check[]>([])
const checksTotal = ref(0)
const checksPage = ref(1)
const checksLoading = ref(true)
const checksError = ref('')
const pageSize = 20
async function loadChecks(p = checksPage.value): Promise<void> {
  checksLoading.value = true
  try {
    const res = await api.get<{ items: Check[] }>('/network/monitor/checks', { page: p, pageSize })
    checks.value = res.data.items
    checksTotal.value = res.meta.total ?? res.data.items.length
    checksPage.value = p
    checksError.value = ''
  } catch (err) {
    checksError.value = errorTitle(err)
  } finally {
    checksLoading.value = false
  }
}

const cache = ref<InstanceType<typeof DnsCacheCard> | null>(null)
function onDnsUpdated(v: NetworkSettings): void {
  net.value = v
  cache.value?.reload()
}

onMounted(() => {
  loadNet()
  loadChecks(1)
})
</script>

<template>
  <Page title="网络" subtitle="出站 DNS 与网络连通性监控。修改 DNS 前会先测试；代理在「代理」页面管理。" width="normal">
    <template #actions>
      <Btn variant="ghost" @click="goPage('/proxies')">代理与分流规则 <ArrowUpRight /></Btn>
    </template>

    <p v-if="netError" class="err"><TriangleAlert /> {{ netError }} <Btn size="sm" @click="loadNet">重试</Btn></p>
    <div v-else-if="!net" class="sk"><Skeleton h="72px" r="14px" /><Skeleton h="220px" r="14px" /></div>
    <template v-else>
      <div class="summary">
        <div class="kv">
          <span>DNS</span>
          <b class="mono truncate">{{ net.dns.servers.join(', ') }}</b>
        </div>
        <button type="button" class="kv link" @click="goPage('/proxies')">
          <span>代理</span>
          <b>{{ net.proxySummary.proxyCount }} 个代理 · {{ net.proxySummary.groupCount }} 个代理组 · {{ net.proxySummary.ruleCount }} 条规则</b>
        </button>
        <div class="kv">
          <span>默认出口</span>
          <b>{{ net.proxySummary.defaultRoute === 'direct' ? '直连' : net.proxySummary.defaultRoute }}{{ net.proxySummary.directFallback ? '（失败时直连兜底）' : '' }}</b>
        </div>
      </div>

      <DnsCard :net="net" @updated="onDnsUpdated" />
      <DnsCacheCard ref="cache" />
    </template>

    <MonitorSection @ran="loadChecks(1)" />

    <Card title="检测记录" subtitle="网络监控每个检测项最近一次的结果。">
      <template #icon><History /></template>
      <template #actions>
        <Btn size="sm" variant="ghost" :loading="checksLoading" @click="loadChecks()"><RefreshCw /> 刷新</Btn>
      </template>
      <p v-if="checksError" class="err"><TriangleAlert /> {{ checksError }}</p>
      <div v-else-if="checksLoading && !checks.length" class="sk"><Skeleton v-for="i in 3" :key="i" h="30px" /></div>
      <Empty v-else-if="!checks.length" title="还没有检测记录" detail="自动监控开启后，定时检测和「立即运行」的结果都会记录在这里；总开关关闭时的手动检测只在上方显示。" />
      <template v-else>
        <ChecksList :items="checks" />
        <Pager :page="checksPage" :page-size="pageSize" :total="checksTotal" :disabled="checksLoading" @update:page="loadChecks" />
      </template>
    </Card>
  </Page>
</template>

<style scoped>
.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1px;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--hairline);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.kv {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  padding: 12px 16px;
  background: var(--surface);
  text-align: left;
}
.kv span {
  font-size: 11.5px;
  color: var(--text-3);
}
.kv b {
  font-size: 13px;
  font-weight: 550;
  min-width: 0;
}
.kv.link:hover {
  background: var(--surface-hover);
}
.kv.link b {
  color: var(--accent);
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-2);
}
.err svg {
  width: 15px;
  height: 15px;
  color: var(--orange);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>
