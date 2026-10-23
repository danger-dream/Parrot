<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ExternalLink, FlaskConical, Pencil, Plus, Trash2, TriangleAlert, X } from '@lucide/vue'
import { api, ApiError, enc } from '@/api/client'
import type { Operation } from '@/api/operations'
import { channelName } from '@/stores/catalog'
import { confirmAction, promptText } from '@/stores/dialogs'
import { closeInspector, toast } from '@/stores/ui'
import { compact, integer, ms, percent } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import KeyValue from '@/components/ui/KeyValue.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import DetailSection from '@/components/DetailSection.vue'
import { runOperation } from '@/pages/channels/meta'
import SortList from '@/pages/channels/SortList.vue'
import ProbeResult from './ProbeResult.vue'
import ProxyEditModal from './ProxyEditModal.vue'
import { bytes, nameError, notifyProxiesChanged, proxyBus, typeLabel, type ProbeItem, type ProbeOutcome, type Proxy, type ProxyGroup, type Routing } from './meta'
import { openProxy } from '@/links'

const props = defineProps<{ id: string; kind: 'proxy' | 'group' }>()

const proxy = ref<Proxy | null>(null)
const group = ref<ProxyGroup | null>(null)
const allProxies = ref<Proxy[]>([])
const allGroups = ref<ProxyGroup[]>([])
const error = ref('')
const busy = ref('')
const editOpen = ref(false)
const testResult = ref<ProbeItem[] | null>(null)
const testError = ref('')

const isGroup = computed(() => props.kind === 'group')
const item = computed(() => (isGroup.value ? group.value : proxy.value))
const stats = computed(() => item.value?.runtimeStats)
const taken = computed(() => [...allProxies.value.map((p) => p.name), ...allGroups.value.map((g) => g.name)])

async function load(): Promise<void> {
  try {
    if (isGroup.value) group.value = (await api.get<ProxyGroup>(`/proxy-groups/${enc(props.id)}`)).data
    else proxy.value = (await api.get<Proxy>(`/proxies/${enc(props.id)}`)).data
    error.value = ''
  } catch (err) {
    error.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND' ? `这个${isGroup.value ? '代理组' : '代理'}不存在，可能已被删除或改名。` : errorTitle(err)
  }
  Promise.all([api.get<Proxy[]>('/proxies', { pageSize: 200 }), api.get<ProxyGroup[]>('/proxy-groups', { pageSize: 200 })])
    .then(([p, g]) => {
      allProxies.value = p.data
      allGroups.value = g.data
    })
    .catch(() => undefined)
}
onMounted(load)
watch(() => [props.id, props.kind], load)
// Reload on changes made elsewhere; our own saves already hold the fresh object.
let ownVersion = -1
watch(
  () => proxyBus.version,
  (v) => v !== ownVersion && load(),
)
function notifyOwn(): void {
  ownVersion = proxyBus.version + 1
  notifyProxiesChanged()
}

async function test(): Promise<void> {
  busy.value = 'test'
  testResult.value = null
  testError.value = ''
  const path = isGroup.value ? `/proxy-groups/${enc(props.id)}/actions/test` : `/proxies/${enc(props.id)}/actions/test`
  const out = await runOperation<ProbeOutcome>(() => api.post<Operation>(path))
  busy.value = ''
  if (!out.ok || !out.result) {
    testError.value = out.error || '测试失败'
    return
  }
  const r = out.result.results
  testResult.value = Array.isArray(r) ? r : [r]
}
const testSummary = computed(() => {
  if (!testResult.value) return ''
  const ok = testResult.value.filter((r) => r.ok).length
  if (!isGroup.value) return ''
  return ok === testResult.value.length ? '全部通过' : ok ? '部分失败' : '全部失败'
})

function onSaved(next: Proxy | ProxyGroup): void {
  const newId = 'groupId' in next ? next.groupId : next.proxyId
  if (newId !== props.id) openProxy(props.kind, newId)
  else if ('groupId' in next) group.value = next
  else proxy.value = next
}

// ---- group members: reorder, add and remove right here, saved immediately ----
const members = ref<string[]>([])
watch(
  () => group.value?.members,
  (m) => (members.value = [...(m || [])]),
  { immediate: true },
)
const memberLabels = computed(() => {
  const out: Record<string, { title: string; sub?: string }> = { direct: { title: '直连', sub: '不经过代理，通常放在最后兜底' } }
  for (const p of allProxies.value) out[p.proxyId] = { title: p.name, sub: `${typeLabel(p.type)} · ${p.server || ''}${p.port ? `:${p.port}` : ''}` }
  for (const m of members.value) if (!out[m]) out[m] = { title: m, sub: '已不存在' }
  return out
})
const addable = computed(() => [
  ...allProxies.value.filter((p) => !members.value.includes(p.proxyId)).map((p) => ({ id: p.proxyId, label: p.name })),
  ...(members.value.includes('direct') ? [] : [{ id: 'direct', label: '直连' }]),
])
async function saveMembers(next: string[], text: string): Promise<void> {
  if (!group.value) return
  const prev = group.value.members
  members.value = next
  busy.value = 'members'
  try {
    group.value = (await api.patch<ProxyGroup>(`/proxy-groups/${enc(props.id)}`, { members: next }, { ifMatch: group.value.revision })).data
    toast(text, 'success')
    notifyOwn()
  } catch (err) {
    members.value = [...prev]
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
  }
}
function addMember(id: string): void {
  saveMembers([...members.value, id], `已加入 ${id === 'direct' ? '直连' : id}`)
}
async function removeMember(id: string): Promise<void> {
  if (members.value.length === 1) {
    const ok = await confirmAction({ title: '移除最后一个成员？', detail: '代理组没有成员时，用它作为目标的规则将无法连通。', confirmText: '移除', danger: true })
    if (!ok) return
  }
  saveMembers(members.value.filter((m) => m !== id), `已移除 ${id === 'direct' ? '直连' : id}`)
}
async function rename(): Promise<void> {
  const it = item.value
  if (!it) return
  const taken2 = taken.value.filter((t) => t !== it.name)
  const v = await promptText({
    title: isGroup.value ? '重命名代理组' : '重命名代理',
    detail: '分流规则和代理组里的引用会一起更新。',
    label: '新名称',
    initial: it.name,
    mono: true,
    validate: (t) => nameError(t, taken2) || null,
  })
  if (v === null || v.trim() === it.name) return
  try {
    const path = isGroup.value ? `/proxy-groups/${enc(props.id)}` : `/proxies/${enc(props.id)}`
    const res = await api.patch<Proxy | ProxyGroup>(path, { name: v.trim() }, { ifMatch: it.revision })
    toast('已重命名', 'success')
    notifyProxiesChanged()
    onSaved(res.data)
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  }
}

// ---- delete with reference cleanup (same outcome as the Telegram menu) ----
interface Ref {
  text: string
  apply: () => Promise<unknown>
}
async function references(id: string): Promise<Ref[]> {
  const [groups, routing] = await Promise.all([
    api.get<ProxyGroup[]>('/proxy-groups', { pageSize: 200 }).then((r) => r.data),
    api.get<Routing>('/proxy-routing').then((r) => r.data),
  ])
  const out: Ref[] = []
  if (!isGroup.value) {
    for (const g of groups) {
      if (!g.members.includes(id)) continue
      out.push({
        text: `代理组 ${g.name}`,
        apply: () => api.patch(`/proxy-groups/${enc(g.groupId)}`, { members: g.members.filter((m) => m !== id) }),
      })
    }
  }
  const patch: Record<string, unknown> = {}
  const labels: string[] = []
  if (routing.default === id) {
    patch.default = 'direct'
    labels.push('默认路线（改为直连）')
  }
  const sections: [keyof Routing, string][] = [
    ['functions', '功能'],
    ['providers', '上游类型'],
    ['accounts', '账号'],
    ['channels', '渠道'],
    ['models', '模型'],
  ]
  for (const [sec, label] of sections) {
    const map = routing[sec] as Record<string, string>
    const keys = Object.keys(map).filter((k) => map[k] === id)
    if (!keys.length) continue
    patch[sec] = Object.fromEntries(keys.map((k) => [k, null]))
    const names = keys.map((k) => (sec === 'channels' ? channelName(k) : sec === 'accounts' ? channelName(`oauth:${k}`) : k))
    labels.push(`${label}规则：${names.join('、')}`)
  }
  if (labels.length) out.push({ text: `分流规则 · ${labels.join('；')}`, apply: () => api.patch('/proxy-routing', patch) })
  return out
}

async function remove(): Promise<void> {
  const it = item.value
  if (!it) return
  busy.value = 'delete'
  let refs: Ref[] = []
  try {
    refs = await references(props.id)
  } catch (err) {
    busy.value = ''
    toast(errorTitle(err), 'error', errorDetail(err))
    return
  }
  busy.value = ''
  const what = isGroup.value ? '代理组' : '代理'
  const ok = await confirmAction({
    title: `删除${what}「${it.name}」？`,
    detail: refs.length
      ? `删除后会同时从以下位置移除：\n${refs.map((r) => `· ${r.text}`).join('\n')}`
      : `删除后会同时从所有${isGroup.value ? '' : '代理组和'}分流规则中移除。当前没有被引用。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  busy.value = 'delete'
  try {
    for (const r of refs) await r.apply()
    const fresh = isGroup.value
      ? (await api.get<ProxyGroup>(`/proxy-groups/${enc(props.id)}`)).data.revision
      : (await api.get<Proxy>(`/proxies/${enc(props.id)}`)).data.revision
    await api.del(isGroup.value ? `/proxy-groups/${enc(props.id)}` : `/proxies/${enc(props.id)}`, { ifMatch: fresh })
    toast(`已删除${what} ${it.name}`, 'success')
    notifyProxiesChanged()
    closeInspector()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    notifyProxiesChanged()
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="d">
    <div v-if="error" class="err"><TriangleAlert /> <span>{{ error }}</span></div>
    <template v-else-if="item && stats">
      <section class="hero">
        <div class="top">
          <p class="name mono">{{ item.name }}</p>
          <button type="button" class="ren" title="重命名" @click="rename"><Pencil /></button>
          <Badge v-if="proxy && !isGroup" tone="teal">{{ typeLabel(proxy.type) }}</Badge>
          <Badge v-else tone="purple">代理组 · {{ group?.members.length }} 个成员</Badge>
        </div>
        <div class="actions">
          <Btn variant="primary" :loading="busy === 'test'" @click="test"><FlaskConical /> 测试{{ isGroup ? '代理组' : '代理' }}</Btn>
          <Btn v-if="!isGroup" @click="editOpen = true"><Pencil /> 编辑</Btn>
          <Btn variant="danger" :loading="busy === 'delete'" @click="remove"><Trash2 /> 删除</Btn>
        </div>
      </section>

      <DetailSection v-if="testResult || testError" title="测试结果" :note="testSummary">
        <p v-if="testError" class="c-red small">{{ testError }}</p>
        <ProbeResult v-else-if="testResult" :items="testResult" />
      </DetailSection>

      <KeyValue
        v-if="proxy && !isGroup"
        :items="[
          { label: '地址', value: proxy.maskedUrl, mono: true, full: true },
          { label: '服务器', value: proxy.server ? `${proxy.server}${proxy.port ? `:${proxy.port}` : ''}` : null, mono: true },
          { label: '加密方式', value: proxy.cipher, mono: true },
        ]"
      />
      <DetailSection v-else-if="group" title="成员" :note="busy === 'members' ? '正在保存…' : '拖动调整顺序，改完立即保存'">
        <p class="faint small">按顺序故障转移：前一个连不上时自动切换到下一个。</p>
        <SortList v-if="members.length" v-model="members" :labels="memberLabels" @commit="(v) => saveMembers(v, '已保存成员顺序')">
          <template #extra="{ id: mid }">
            <button v-if="mid !== 'direct'" type="button" class="mbtn" title="查看代理" @click="openProxy('proxy', mid)"><ExternalLink /></button>
            <button type="button" class="mbtn danger" title="移出代理组" :disabled="busy === 'members'" @click="removeMember(mid)"><X /></button>
          </template>
        </SortList>
        <p v-else class="faint small">还没有成员。</p>
        <div v-if="addable.length" class="add">
          <span class="add-l">添加成员</span>
          <button v-for="a in addable" :key="a.id" type="button" class="chip" :disabled="busy === 'members'" @click="addMember(a.id)"><Plus /> <span class="mono">{{ a.label }}</span></button>
        </div>
      </DetailSection>

      <DetailSection :title="isGroup ? '组内总计' : '使用统计'">
        <p v-if="!stats.requests" class="faint small">暂无经过{{ isGroup ? '组内代理' : '这个代理' }}的请求。</p>
        <div v-else class="mini">
          <div><p class="k">请求</p><p class="v">{{ integer(stats.requests) }}</p></div>
          <div><p class="k">成功率</p><p class="v">{{ percent(stats.successes, stats.requests) }}</p></div>
          <div><p class="k">失败</p><p class="v" :class="{ 'c-orange': stats.failures }">{{ integer(stats.failures) }}</p></div>
          <div><p class="k">Token</p><p class="v">{{ compact(stats.totalTokens) }}</p></div>
          <div><p class="k">上行 / 下行</p><p class="v sm">{{ bytes(stats.bytesUp) }} / {{ bytes(stats.bytesDown) }}</p></div>
          <div><p class="k">代理流量</p><p class="v">{{ bytes(stats.totalBytes) }}</p></div>
          <div><p class="k">平均连接</p><p class="v">{{ ms(stats.avgConnectMilliseconds) }}</p></div>
          <div><p class="k">平均首字</p><p class="v">{{ ms(stats.avgFirstByteMilliseconds) }}</p></div>
          <div><p class="k">平均总耗时</p><p class="v">{{ ms(stats.avgTotalMilliseconds) }}</p></div>
        </div>
      </DetailSection>

      <ProxyEditModal v-if="proxy && !isGroup" :open="editOpen" :proxy="proxy" :taken="taken" @close="editOpen = false" @saved="onSaved" />
    </template>
    <div v-else class="sk"><Skeleton h="24px" /><Skeleton h="60px" /><Skeleton h="120px" /></div>
  </div>
</template>

<style scoped>
.d {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.hero {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.top {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.name {
  flex: 0 1 auto;
  min-width: 0;
  font-size: 16px;
  font-weight: 650;
  word-break: break-all;
}
.top > :last-child {
  margin-left: auto;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.members {
  list-style: none;
  margin: 0;
  padding: 0;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.members li {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 36px;
  padding: 0 12px;
  border-bottom: 0.5px solid var(--hairline);
  font-size: 12.5px;
}
.members li:last-child {
  border-bottom: 0;
}
.idx {
  width: 16px;
  font-size: 11px;
  color: var(--text-3);
}
.link:hover {
  color: var(--accent);
}
.mini {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  overflow: hidden;
}
.mini > div {
  padding: 9px 12px;
  border-right: 0.5px solid var(--hairline);
  border-bottom: 0.5px solid var(--hairline);
}
.mini > div:nth-child(3n) {
  border-right: 0;
}
.mini > div:nth-child(n + 7) {
  border-bottom: 0;
}
.k {
  font-size: 11px;
  color: var(--text-3);
}
.v {
  margin-top: 2px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.v.sm {
  font-size: 12px;
}
.small {
  font-size: 12px;
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
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ren {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  color: var(--text-3);
}
.ren:hover {
  background: var(--fill-2);
  color: var(--text);
}
.ren svg,
.mbtn svg,
.chip svg {
  width: 13px;
  height: 13px;
}
.mbtn {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 6px;
  color: var(--text-2);
  flex-shrink: 0;
}
.mbtn:hover:not(:disabled) {
  background: var(--fill-2);
  color: var(--text);
}
.mbtn.danger:hover:not(:disabled) {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 12%, transparent);
}
.add {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.add-l {
  font-size: 12px;
  color: var(--text-3);
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 10px;
  border-radius: 8px;
  font-size: 12px;
  background: var(--fill-2);
}
.chip:hover:not(:disabled) {
  background: var(--accent-soft);
  color: var(--accent);
}
</style>
