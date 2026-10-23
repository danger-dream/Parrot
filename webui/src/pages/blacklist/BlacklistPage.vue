<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Ban, Plus, RefreshCw, ShieldBan, TriangleAlert, X } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { openChannel } from '@/links'
import { catalog, channelName, refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Page from '@/components/ui/Page.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import TextInput from '@/components/ui/TextInput.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { channelProvider } from '@/utils/providers'

type Blacklist = Schemas['ContentBlacklistData']

const data = ref<Blacklist | null>(null)
const loading = ref(true)
const error = ref('')
const busy = ref('')
const defaultTerm = ref('')
const channelTerms = ref<Record<string, string>>({})
const newChannel = ref('')
const newChannelTerm = ref('')

async function load(): Promise<void> {
  loading.value = true
  try {
    data.value = (await api.get<Blacklist>('/content-blacklist')).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  load()
  refreshCatalog().catch(() => undefined)
})

const byChannel = computed(() => (data.value?.byChannel || []).filter((c) => c.terms.length))
const channelOptions = computed(() =>
  Object.entries(catalog.channels)
    .map(([id, c]) => ({ value: id, label: `${c.name}（${c.type === 'oauth' ? 'OAuth 账号' : 'API 渠道'}）` }))
    .sort((a, b) => a.label.localeCompare(b.label)),
)
const total = computed(() => (data.value?.default.length || 0) + byChannel.value.reduce((s, c) => s + c.terms.length, 0))

function termError(t: string): string {
  const v = t.trim()
  if (!v) return '关键词不能为空'
  if (v.length > 200) return '关键词过长（上限 200 个字符）'
  return ''
}

async function mutate(key: string, fn: () => Promise<unknown>, ok: string): Promise<boolean> {
  busy.value = key
  try {
    await fn()
    toast(ok, 'success')
    await load()
    return true
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') await load()
    return false
  } finally {
    busy.value = ''
  }
}

async function addDefault(): Promise<void> {
  const t = defaultTerm.value.trim()
  const e = termError(t)
  if (e) return toast(e, 'warning')
  if (data.value?.default.includes(t)) return toast('这个关键词已经在默认规则里了', 'info')
  if (await mutate('add:default', () => api.post('/content-blacklist/default', { term: t }, { ifMatch: data.value?.revision }), '已添加默认关键词')) defaultTerm.value = ''
}
async function delDefault(t: string): Promise<void> {
  const ok = await confirmAction({ title: '删除默认关键词？', detail: `「${t}」将不再对所有渠道生效。`, confirmText: '删除', danger: true })
  if (!ok) return
  await mutate(`del:default:${t}`, () => api.del(`/content-blacklist/default/${enc(t)}`, { ifMatch: data.value?.revision }), '已删除')
}

async function addChannel(channelId: string, term: string, after?: () => void): Promise<void> {
  const t = term.trim()
  const e = termError(t)
  if (e) return toast(e, 'warning')
  if (!channelId) return toast('请先选择渠道', 'warning')
  const existing = data.value?.byChannel.find((c) => c.channelId === channelId)
  if (existing?.terms.includes(t)) return toast('这个渠道已经有这个关键词了', 'info')
  const ok = await mutate(
    `add:${channelId}`,
    () => api.post(`/content-blacklist/channels/${enc(channelId)}`, { term: t }, { ifMatch: data.value?.revision }),
    `已为 ${channelName(channelId)} 添加关键词`,
  )
  if (ok) after?.()
}
async function delChannel(channelId: string, t: string): Promise<void> {
  const ok = await confirmAction({ title: '删除渠道关键词？', detail: `「${t}」将不再对 ${channelName(channelId)} 生效。`, confirmText: '删除', danger: true })
  if (!ok) return
  await mutate(`del:${channelId}:${t}`, () => api.del(`/content-blacklist/channels/${enc(channelId)}/${enc(t)}`, { ifMatch: data.value?.revision }), '已删除')
}
</script>

<template>
  <Page title="内容黑名单" subtitle="上游返回的首包内容中出现任一关键词即视为命中，在尚未向客户端输出之前会改用其他渠道。关键词区分大小写。" width="normal">
    <template #actions>
      <Btn icon title="刷新" :loading="loading && !!data" @click="load"><RefreshCw /></Btn>
    </template>

    <p v-if="error" class="err"><TriangleAlert /> {{ error }} <Btn size="sm" @click="load">重试</Btn></p>
    <div v-else-if="!data" class="sk"><Skeleton h="140px" r="14px" /><Skeleton h="200px" r="14px" /></div>
    <template v-else>
      <Card title="默认规则" subtitle="对所有渠道生效">
        <template #icon><ShieldBan /></template>
        <template #actions><span class="count">{{ data.default.length }} 条</span></template>
        <div class="terms">
          <p v-if="!data.default.length" class="none">还没有默认关键词</p>
          <span v-for="t in data.default" :key="t" class="term">
            <span class="mono">{{ t }}</span>
            <button type="button" title="删除" :disabled="busy === `del:default:${t}`" @click="delDefault(t)"><X /></button>
          </span>
        </div>
        <div class="add">
          <TextInput v-model="defaultTerm" mono placeholder="输入要屏蔽的关键词" @enter="addDefault" />
          <Btn :loading="busy === 'add:default'" @click="addDefault"><Plus /> 添加</Btn>
        </div>
      </Card>

      <Card title="按渠道规则" subtitle="只对指定渠道或账号生效，与默认规则叠加">
        <template #icon><Ban /></template>
        <template #actions><span class="count">{{ byChannel.length }} 个渠道</span></template>
        <Empty v-if="!byChannel.length" title="还没有渠道专属关键词" detail="在下方选择渠道并添加关键词。" />
        <div v-else class="channels">
          <section v-for="c in byChannel" :key="c.channelId" class="ch">
            <div class="ch-head">
              <ProviderIcon :provider="channelProvider(c.channelId)" :size="18" />
              <button type="button" class="ch-name" @click="openChannel(c.channelId)">{{ channelName(c.channelId) }}</button>
              <span v-if="!catalog.channels[c.channelId] && catalog.loadedAt" class="gone">渠道已不存在</span>
              <span class="mono faint id truncate">{{ c.channelId }}</span>
            </div>
            <div class="terms">
              <span v-for="t in c.terms" :key="t" class="term">
                <span class="mono">{{ t }}</span>
                <button type="button" title="删除" :disabled="busy === `del:${c.channelId}:${t}`" @click="delChannel(c.channelId, t)"><X /></button>
              </span>
            </div>
            <div class="add">
              <TextInput v-model="channelTerms[c.channelId]" mono placeholder="为这个渠道再加一个关键词" @enter="addChannel(c.channelId, channelTerms[c.channelId] || '', () => (channelTerms[c.channelId] = ''))" />
              <Btn :loading="busy === `add:${c.channelId}`" @click="addChannel(c.channelId, channelTerms[c.channelId] || '', () => (channelTerms[c.channelId] = ''))"><Plus /> 添加</Btn>
            </div>
          </section>
        </div>
        <div class="new">
          <p class="new-t">为其他渠道添加</p>
          <div class="new-row">
            <Select v-model="newChannel" :options="channelOptions" placeholder="选择渠道或账号" width="100%" />
            <TextInput v-model="newChannelTerm" mono placeholder="关键词" @enter="addChannel(newChannel, newChannelTerm, () => (newChannelTerm = ''))" />
            <Btn variant="primary" :loading="busy === `add:${newChannel}`" :disabled="!newChannel" @click="addChannel(newChannel, newChannelTerm, () => (newChannelTerm = ''))"><Plus /> 添加</Btn>
          </div>
        </div>
      </Card>
      <p class="faint foot">共 {{ total }} 条关键词</p>
    </template>
  </Page>
</template>

<style scoped>
.count {
  font-size: 11.5px;
  color: var(--text-3);
}
.terms {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.none {
  font-size: 12px;
  color: var(--text-3);
}
.term {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  max-width: 100%;
  min-height: 28px;
  padding: 2px 2px 2px 10px;
  border-radius: 7px;
  font-size: 12px;
  background: color-mix(in srgb, var(--red) 10%, transparent);
  color: var(--text);
}
.term .mono {
  word-break: break-all;
}
.term button {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 5px;
  flex-shrink: 0;
  color: var(--text-3);
}
.term button:hover:not(:disabled) {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 14%, transparent);
}
.term svg {
  width: 12px;
  height: 12px;
}
.add {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.add :deep(.ctl) {
  flex: 1;
}
.channels {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ch {
  padding: 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.ch-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  min-width: 0;
}
.ch-name {
  font-weight: 600;
  color: var(--accent);
  text-align: left;
}
.ch-name:hover {
  text-decoration: underline;
}
.gone {
  font-size: 11px;
  color: var(--orange);
}
.id {
  font-size: 11px;
}
.new {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 0.5px solid var(--hairline);
}
.new-t {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 8px;
}
.new-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  gap: 8px;
}
.foot {
  font-size: 11.5px;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 12px;
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
@container (max-width: 600px) {
  .new-row {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
