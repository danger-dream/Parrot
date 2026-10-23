<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Globe, Layers, Plus, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api } from '@/api/client'
import { inspector } from '@/stores/ui'
import { compact, integer, ms, percent } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Page from '@/components/ui/Page.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import GroupModal from './GroupModal.vue'
import ProxyAddModal from './ProxyAddModal.vue'
import RoutingPanel from './RoutingPanel.vue'
import { bytes, proxyBus, targetLabel, typeLabel, type Proxy, type ProxyGroup } from './meta'
import { openProxy } from '@/links'

type Tab = 'list' | 'routing'

const route = useRoute()
const tab = ref<Tab>('list')
const proxies = ref<Proxy[]>([])
const groups = ref<ProxyGroup[]>([])
const loading = ref(true)
const error = ref('')
const q = ref('')
const addOpen = ref(false)
const groupOpen = ref(false)

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const [p, g] = await Promise.all([api.get<Proxy[]>('/proxies', { pageSize: 200 }), api.get<ProxyGroup[]>('/proxy-groups', { pageSize: 200 })])
    proxies.value = p.data
    groups.value = g.data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}

const taken = computed(() => [...proxies.value.map((p) => p.name), ...groups.value.map((g) => g.name)])
const s = computed(() => q.value.trim().toLowerCase())
const visibleProxies = computed(() => proxies.value.filter((p) => !s.value || p.name.includes(s.value) || (p.server || '').includes(s.value)))
const visibleGroups = computed(() => groups.value.filter((g) => !s.value || g.name.includes(s.value) || g.members.some((m) => m.includes(s.value))))
const selected = computed(() => (inspector.open ? inspector.key : ''))

function applyDeepLink(): void {
  const qy = route.query
  if (qy.tab === 'routing') tab.value = 'routing'
  else if (qy.tab === 'groups' || qy.tab === 'proxies' || qy.tab === 'list') tab.value = 'list'
  if (typeof qy.proxy === 'string' && qy.proxy) openProxy('proxy', qy.proxy)
  if (typeof qy.group === 'string' && qy.group) {
    tab.value = 'list'
    openProxy('group', qy.group)
  }
}

watch(() => proxyBus.version, () => load(true))
watch(() => route.query, applyDeepLink)
onMounted(() => {
  load()
  applyDeepLink()
})
</script>

<template>
  <Page title="代理" subtitle="出口代理、代理组和分流规则。上游请求、登录和额度查询会按分流规则选择从哪个出口发出。" width="wide">
    <template #toolbar>
      <Segmented
        v-model="tab"
        :options="[
          { value: 'list', label: `代理与代理组 · ${proxies.length + groups.length}` },
          { value: 'routing', label: '分流规则' },
        ]"
      />
      <SearchField v-if="tab === 'list'" v-model="q" placeholder="搜索名称或服务器" width="220px" />
      <span class="grow" />
      <Btn icon title="刷新" :loading="loading && !!proxies.length" @click="load()"><RefreshCw /></Btn>
    </template>

    <p v-if="error" class="err"><TriangleAlert /> {{ error }} <Btn size="sm" @click="load()">重试</Btn></p>
    <div v-else-if="loading && !proxies.length && !groups.length" class="split"><Skeleton h="320px" r="14px" /><Skeleton h="320px" r="14px" /></div>

    <div v-else-if="tab === 'list'" class="split">
      <Card title="代理" :subtitle="`${proxies.length} 个 · Shadowsocks / SOCKS5 出口`">
        <template #icon><Globe /></template>
        <template #actions><Btn size="sm" variant="primary" @click="addOpen = true"><Plus /> 添加代理</Btn></template>
        <Empty v-if="!proxies.length" title="还没有代理" detail="添加后可以在分流规则里让指定的渠道、账号或模型走代理。" />
        <Empty v-else-if="!visibleProxies.length" title="没有符合条件的代理" />
        <div v-else class="list">
          <button v-for="p in visibleProxies" :key="p.proxyId" type="button" class="item" :class="{ selected: selected === `proxy:${p.proxyId}` }" @click="openProxy('proxy', p.proxyId)">
            <div class="i-top">
              <span class="name mono truncate">{{ p.name }}</span>
              <Badge tone="teal">{{ typeLabel(p.type) }}</Badge>
              <span class="grow" />
              <span v-if="p.runtimeStats.requests" class="rate num" :class="p.runtimeStats.failures / p.runtimeStats.requests > 0.05 ? 'c-orange' : 'c-green'">
                {{ percent(p.runtimeStats.successes, p.runtimeStats.requests) }}
              </span>
            </div>
            <p class="url mono truncate" :title="p.maskedUrl">{{ p.maskedUrl }}</p>
            <p v-if="p.runtimeStats.requests" class="stats num">
              {{ integer(p.runtimeStats.requests) }} 次 · 失败 {{ integer(p.runtimeStats.failures) }} · {{ compact(p.runtimeStats.totalTokens) }} tok · {{ bytes(p.runtimeStats.totalBytes) }}
              · 连接 {{ ms(p.runtimeStats.avgConnectMilliseconds) }} · 首字 {{ ms(p.runtimeStats.avgFirstByteMilliseconds) }} · 总耗时 {{ ms(p.runtimeStats.avgTotalMilliseconds) }}
            </p>
            <p v-else class="stats faint">暂无代理请求统计</p>
          </button>
        </div>
      </Card>

      <Card title="代理组" :subtitle="`${groups.length} 个 · 按成员顺序故障转移`">
        <template #icon><Layers /></template>
        <template #actions><Btn size="sm" @click="groupOpen = true"><Plus /> 新建代理组</Btn></template>
        <Empty v-if="!groups.length" title="还没有代理组" detail="把多个代理（以及直连）按顺序组成一组，用作分流规则的目标。" />
        <Empty v-else-if="!visibleGroups.length" title="没有符合条件的代理组" />
        <div v-else class="list">
          <button v-for="g in visibleGroups" :key="g.groupId" type="button" class="item" :class="{ selected: selected === `pgroup:${g.groupId}` }" @click="openProxy('group', g.groupId)">
            <div class="i-top">
              <span class="name mono truncate">{{ g.name }}</span>
              <Badge tone="purple">{{ g.members.length }} 个成员</Badge>
              <span class="grow" />
              <span v-if="g.runtimeStats.requests" class="rate num" :class="g.runtimeStats.failures / g.runtimeStats.requests > 0.05 ? 'c-orange' : 'c-green'">
                {{ percent(g.runtimeStats.successes, g.runtimeStats.requests) }}
              </span>
            </div>
            <p class="chain mono">{{ g.members.map(targetLabel).join(' → ') || '（没有成员）' }}</p>
            <p v-if="g.runtimeStats.requests" class="stats num">
              组内 {{ integer(g.runtimeStats.requests) }} 次 · {{ compact(g.runtimeStats.totalTokens) }} tok · {{ bytes(g.runtimeStats.totalBytes) }} · 连接 {{ ms(g.runtimeStats.avgConnectMilliseconds) }} · 首字 {{ ms(g.runtimeStats.avgFirstByteMilliseconds) }}
            </p>
            <p v-else class="stats faint">暂无组内代理请求统计</p>
          </button>
        </div>
      </Card>
    </div>

    <RoutingPanel v-else :proxies="proxies" :groups="groups" />

    <ProxyAddModal :open="addOpen" :taken="taken" @close="addOpen = false" />
    <GroupModal :open="groupOpen" :group="null" :proxies="proxies" :taken="taken" @close="groupOpen = false" @saved="(g) => openProxy('group', g.groupId)" />
  </Page>
</template>

<style scoped>
.split {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  gap: 14px;
  align-items: start;
}
.list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  min-width: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  text-align: left;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  transition: background 0.15s, box-shadow 0.15s;
}
.item:hover {
  background: var(--fill-2);
}
.item.selected {
  box-shadow: inset 0 0 0 1.5px var(--accent);
}
.i-top {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.name {
  font-size: 13.5px;
  font-weight: 650;
  min-width: 0;
}
.rate {
  font-size: 12px;
  font-weight: 600;
}
.url,
.chain {
  font-size: 11.5px;
  color: var(--text-2);
}
.chain {
  word-break: break-all;
}
.stats {
  font-size: 11.5px;
  color: var(--text-2);
  overflow-wrap: anywhere;
}
.grow {
  flex: 1;
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
}
@container (max-width: 980px) {
  .split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
