<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { CircleCheck, CircleX, Eraser, FlaskConical, Layers, Link2Off, ListFilter, LoaderCircle, Pencil, Play, RefreshCw, SlidersHorizontal, Trash2, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import type { Operation } from '@/api/operations'
import { runAction } from '@/composables/useResource'
import { goLogs, openChannel, openModel } from '@/links'
import { refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { closeInspector, toast } from '@/stores/ui'
import { compact, dateTime, duration, integer, ms } from '@/utils/format'
import { channelProvider } from '@/utils/providers'
import { errorDetail, errorTitle } from '@/utils/errors'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import KeyValue from '@/components/ui/KeyValue.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import StatusDot from '@/components/ui/StatusDot.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import DetailSection from '@/components/DetailSection.vue'
import ModelList from '@/components/ModelList.vue'
import ModelListRow from '@/components/ModelListRow.vue'
import SettingRow from '@/components/SettingRow.vue'
import UsageTiles, { type UsageTile } from '@/components/UsageTiles.vue'
import { deltas, loadCompare, type Compare } from '@/utils/compare'
import { localUsageLine } from '@/pages/oauth/channelStats'
import ChannelEditModal from './ChannelEditModal.vue'
import ChannelModelsModal from './ChannelModelsModal.vue'
import CompatModal from './CompatModal.vue'
import ProtocolTag from './ProtocolTag.vue'
import UsageBlock from './UsageBlock.vue'
import {
  channelStatus,
  cost3,
  dotTone,
  loadCatalog,
  notifyChannelsChanged,
  protocolInfo,
  rateTone,
  runOperation,
  toUsage,
  tps,
  type Catalog,
  type Channel,
  type ChannelDetailData,
  type ProbeResult,
  type Tone,
} from './meta'

const props = defineProps<{ id: string }>()

const data = ref<ChannelDetailData | null>(null)
const error = ref('')
const notFound = ref(false)
const busy = ref('')
const editOpen = ref(false)
const modelsOpen = ref(false)
const compatOpen = ref(false)
let timer: number | undefined
const now = ref(Date.now())
let ticker: number | undefined

async function load(): Promise<void> {
  try {
    data.value = (await api.get<ChannelDetailData>(`/channels/${enc(props.id)}`)).data
    error.value = ''
    loadCompare({ period: 'month', channel: props.id })
      .then((x) => (compare.value = x))
      .catch(() => undefined)
    notFound.value = false
  } catch (err) {
    notFound.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND'
    error.value = notFound.value ? '这个渠道不存在，可能已被删除或改名。' : errorTitle(err)
  }
}
onMounted(() => {
  load()
  ticker = window.setInterval(() => (now.value = Date.now()), 1000)
  timer = window.setInterval(() => {
    if (document.visibilityState === 'visible' && !editOpen.value && !modelsOpen.value && !compatOpen.value && !busy.value) load()
  }, 20_000)
})
onBeforeUnmount(() => {
  window.clearInterval(timer)
  window.clearInterval(ticker)
})
watch(() => props.id, load)

const c = computed(() => data.value)
const status = computed(() => (c.value ? channelStatus(c.value) : { text: '', tone: 'neutral' as Tone }))
const catalog = ref<Catalog | null>(null)
loadCatalog()
  .then((x) => (catalog.value = x))
  .catch(() => undefined)
const providerInfo = computed(() => {
  const d = c.value
  if (!d?.providerId) return '自定义地址'
  const p = catalog.value?.providers.find((x) => x.id === d.providerId)
  const preset = p?.presets.find((x) => x.id === d.providerPresetId)
  return [p?.name || d.providerId, preset?.name || d.providerPresetId].filter(Boolean).join(' · ')
})

function merge(next: Channel): void {
  if (!data.value) return
  data.value = { ...data.value, ...next }
}
async function afterSave(next: Channel): Promise<void> {
  notifyChannelsChanged()
  if (next.id !== props.id) {
    await refreshCatalog(true).catch(() => undefined)
    openChannel(next.id)
    return
  }
  merge(next)
  load()
}

async function patch(body: Schemas['ChannelUpdateRequest'], okText: string): Promise<void> {
  if (!data.value) return
  busy.value = Object.keys(body)[0]
  try {
    const res = await api.patch<Channel>(`/channels/${enc(props.id)}`, body, { ifMatch: data.value.revision })
    merge(res.data)
    notifyChannelsChanged()
    toast(okText, 'success')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    load()
  } finally {
    busy.value = ''
  }
}

function compatText(f: { mode: string; models: string[]; allModels: boolean } | undefined): string {
  if (!f || f.mode !== 'force') return '自动'
  return f.allModels ? '强制 · 全部模型' : `强制 · ${f.models.length} 个模型`
}
const compatSummary = computed(() => {
  const d = c.value
  if (!d) return ''
  const parts = [`Fast 模式 ${compatText(d.compatibility.fast)}`]
  if (d.protocol === 'anthropic') parts.unshift(`1M 上下文 ${compatText(d.compatibility.context1m)}`)
  return parts.join(' · ')
})

const connection = computed(() => {
  const d = c.value
  if (!d) return []
  const p = protocolInfo(d.protocol)
  return [
    { label: '服务商', value: providerInfo.value },
    { label: '协议', value: p ? `${p.label} · ${p.path}` : '未知协议' },
    { label: '地址', value: d.url || d.baseUrl, mono: true, full: true },
    { label: '密钥', value: d.apiKeyConfigured ? d.apiKeyMaskedHint || '已配置' : '未配置', mono: true, tone: d.apiKeyConfigured ? undefined : ('red' as const) },
  ]
})

// ---- 最大并发, edited in place ----
const ccDraft = ref<number | null>(0)
watch(
  () => c.value?.maxConcurrent,
  (v) => (ccDraft.value = v ?? 0),
  { immediate: true },
)
const ccDirty = computed(() => c.value !== null && ccDraft.value !== null && ccDraft.value !== (c.value.maxConcurrent ?? 0))
async function saveConcurrency(): Promise<void> {
  if (!ccDirty.value || ccDraft.value === null) return
  const v = ccDraft.value
  await patch({ maxConcurrent: v }, v ? `最大并发已设为 ${v}` : '已改为默认并发')
}

const monthUsage = computed(() => toUsage(c.value?.monthStats))
/** This month against last month up to the same point. */
const compare = ref<Compare | null>(null)
const change = computed(() => deltas(compare.value, 'month'))
const tiles = computed<UsageTile[]>(() => {
  const u = monthUsage.value
  if (!u || !u.total) return []
  const share = u.cacheReadRatio != null ? u.cacheReadRatio * 100 : u.prompt ? (u.cacheRead / u.prompt) * 100 : null
  const d = change.value
  return [
    { k: '读入 ↑（含缓存）', v: compact(u.prompt), delta: d.prompt },
    { k: '写出 ↓', v: compact(u.output), delta: d.output },
    { k: '金额', v: cost3(u.costTicks), delta: d.cost },
    { k: '缓存读取', v: compact(u.cacheRead), s: share !== null ? `占读入 ${share >= 99.95 && share < 100 ? '99.9' : share.toFixed(1)}%` : '', delta: d.cacheRead },
    { k: '平均速度', v: tps(u.avgTps), s: u.maxTps !== null ? `峰值 ${tps(u.maxTps)} · 最低 ${tps(u.minTps)}` : '' },
    { k: '请求', v: integer(u.total), s: u.error ? `失败 ${integer(u.error)}` : '全部成功', bad: Boolean(u.error), delta: d.requests },
  ]
})
const statsByModel = computed(() => Object.fromEntries((c.value?.modelStats || []).map((m) => [m.finalModel, m])))
const otherModelStats = computed(() => {
  const reals = new Set((c.value?.models || []).map((m) => m.real))
  return (c.value?.modelStats || []).filter((m) => !reals.has(m.finalModel))
})
const hasFaults = computed(() => (c.value?.runtimeModels || []).some((m) => m.cooldownKind || m.errorCount))

function remaining(until: string | null | undefined): string {
  if (!until) return ''
  const left = Math.round((new Date(until).getTime() - now.value) / 1000)
  return left > 0 ? ` · 还剩 ${duration(left)}` : ' · 即将恢复'
}
type RuntimeModel = ChannelDetailData['runtimeModels'][number]
/** Only problems get a badge, like the OAuth account model list. */
function modelProblem(m: RuntimeModel): { text: string; tone: Tone } | null {
  if (m.cooldownKind === 'permanent') return { text: '永久冷却', tone: 'red' }
  if (m.cooldownKind === 'quota') return { text: '额度冷却', tone: 'orange' }
  if (m.cooldownKind === 'temporary') return { text: `冷却中${remaining(m.cooldownUntil)}`, tone: 'orange' }
  if (m.recentRequests > 0 && m.recentSuccessRate != null && m.recentSuccessRate < 95) return { text: `近期成功 ${m.recentSuccessRate.toFixed(0)}%`, tone: rateTone(m.recentSuccessRate) }
  return null
}
function modelMeta(m: RuntimeModel): string {
  const parts: string[] = []
  if (m.contextWindow) parts.push(`上下文 ${compact(m.contextWindow, 0)}`)
  if (m.maxOutputTokens) parts.push(`输出 ${compact(m.maxOutputTokens, 0)}`)
  return parts.join(' · ')
}
// Usable models first (problems on top), disabled ones last; same order as the OAuth account list.
const sortedModels = computed(() =>
  [...(c.value?.runtimeModels || [])].sort((a, b) =>
    Number(a.disabled || a.globallyDisabled) - Number(b.disabled || b.globallyDisabled)
    || Number(Boolean(modelProblem(b))) - Number(Boolean(modelProblem(a)))),
)
const provider = computed(() => (c.value ? channelProvider(c.value.id, c.value.providerId) : ''))

// ---- diagnostic probes, run from the model rows (connectivity only; cooldowns stay as they are) ----
interface ProbeState {
  status: 'running' | 'ok' | 'fail'
  ms?: number
  reason?: string
  note?: string
}
const probes = reactive<Record<string, ProbeState>>({})
const probingAll = ref(false)
const probing = computed(() => probingAll.value || Object.values(probes).some((x) => x.status === 'running'))
const probeSummary = computed(() => {
  const vals = Object.values(probes)
  return { ok: vals.filter((x) => x.status === 'ok').length, fail: vals.filter((x) => x.status === 'fail').length }
})
async function probe(model: string): Promise<void> {
  probes[model] = { status: 'running' }
  const out = await runOperation<ProbeResult>(() => api.post<Operation>(`/channels/${enc(props.id)}/diagnostic-probes`, { model }))
  if (!out.ok) probes[model] = { status: 'fail', reason: out.error }
  else if (out.result?.ok) {
    const note = out.result.permanent_cooldown_cleared ? '已自动解除永久冷却' : out.result.cooldown_cleared ? '已自动清除冷却与失败计数' : undefined
    probes[model] = { status: 'ok', ms: out.result.elapsed_ms, note }
    if (note) load()
  } else probes[model] = { status: 'fail', ms: out.result?.elapsed_ms, reason: out.result?.reason || '未知错误' }
}
async function probeAll(): Promise<void> {
  probingAll.value = true
  try {
    for (const m of c.value?.runtimeModels || []) await probe(m.real)
  } finally {
    probingAll.value = false
  }
}

async function refreshUsage(): Promise<void> {
  busy.value = 'usage'
  const out = await runOperation<{ affected: number; queued: boolean | null }>(() => api.post<Operation>(`/channels/${enc(props.id)}/actions/refresh-usage`))
  busy.value = ''
  if (!out.ok) {
    toast('刷新额度失败', 'error', out.error)
    return
  }
  toast(out.result?.queued ? '已请求更新上游额度' : '暂时无需重复更新', 'success')
  for (const wait of [1500, 4000, 8000]) window.setTimeout(load, wait)
}

async function clearErrors(): Promise<void> {
  busy.value = 'errors'
  const res = await runAction(() => api.post<{ affected: number }>(`/channels/${enc(props.id)}/actions/clear-errors`))
  busy.value = ''
  if (res) toast(`已清除 ${res.affected} 条冷却记录`, 'success')
  notifyChannelsChanged()
  load()
}
async function clearAffinity(): Promise<void> {
  busy.value = 'affinity'
  const res = await runAction(() => api.post<{ affected: number }>(`/channels/${enc(props.id)}/actions/clear-affinity`))
  busy.value = ''
  if (res) toast(`已清空 ${res.affected} 条亲和记录`, 'success')
  load()
}

async function remove(): Promise<void> {
  const d = data.value
  if (!d) return
  const ok = await confirmAction({
    title: `删除渠道「${d.name}」？`,
    detail: `这个渠道有 ${d.modelCount} 个模型。删除后会一并清理这个渠道的冷却和亲和记录，并从负载均衡队列中移除，不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  busy.value = 'delete'
  try {
    await api.del(`/channels/${enc(props.id)}`, { ifMatch: d.revision })
    toast(`已删除 ${d.name}`, 'success')
    notifyChannelsChanged()
    refreshCatalog(true).catch(() => undefined)
    closeInspector()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    load()
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="d">
    <div v-if="error" class="err">
      <TriangleAlert />
      <span>{{ error }}</span>
      <Btn v-if="!notFound" size="sm" @click="load">重试</Btn>
    </div>
    <template v-else-if="c">
      <section class="hero">
        <div class="top">
          <ProviderIcon :provider="provider" :size="36" />
          <div class="titles">
            <p class="name">{{ c.name }}</p>
            <p class="state">
              <StatusDot :tone="dotTone(status.tone)" />
              <span :class="`t-${status.tone}`">{{ status.text }}</span>
              <span class="faint">·</span>
              <ProtocolTag :protocol="c.protocol" />
              <span class="faint">· {{ c.modelCount }} 个模型</span>
            </p>
          </div>
        </div>
      </section>

      <!-- 设置: everything switched or typed in place -->
      <DetailSection title="设置">
        <div class="settings">
          <SettingRow title="启用渠道" hint="停用后不再调度到这个渠道">
            <Switch :model-value="c.enabled" :loading="busy === 'enabled'" label="启用渠道" @update:model-value="patch({ enabled: $event }, $event ? '已启用' : '已停用')" />
          </SettingRow>
          <SettingRow title="最大并发" hint="同时处理的请求上限，0 表示使用默认值">
            <span @keydown.enter="saveConcurrency"><NumberInput v-model="ccDraft" :min="0" :max="10000" integer width="92px" /></span>
            <Btn v-if="ccDirty" size="sm" variant="primary" :loading="busy === 'maxConcurrent'" @click="saveConcurrency">保存</Btn>
          </SettingRow>
          <SettingRow v-if="c.protocol === 'anthropic' || c.ccMimicry" title="CC 伪装" hint="以 Claude Code 客户端身份请求上游">
            <Switch :model-value="c.ccMimicry" :loading="busy === 'ccMimicry'" label="CC 伪装" @update:model-value="patch({ ccMimicry: $event }, $event ? '已开启 CC 伪装' : '已关闭 CC 伪装')" />
          </SettingRow>
          <SettingRow title="剔除 temperature" hint="转发前去掉 temperature 参数，用于不接受它的上游">
            <Switch :model-value="c.omitTemperature" :loading="busy === 'omitTemperature'" label="剔除 temperature" @update:model-value="patch({ omitTemperature: $event }, '已更新')" />
          </SettingRow>
          <SettingRow title="剔除 thinking" hint="转发前去掉 thinking 参数，用于不支持思考的上游">
            <Switch :model-value="c.omitThinking" :loading="busy === 'omitThinking'" label="剔除 thinking" @update:model-value="patch({ omitThinking: $event }, '已更新')" />
          </SettingRow>
          <SettingRow title="兼容性" :hint="compatSummary">
            <Btn size="sm" @click="compatOpen = true"><SlidersHorizontal /> 设置…</Btn>
          </SettingRow>
        </div>
      </DetailSection>

      <!-- 连接 -->
      <DetailSection title="连接">
        <template #actions>
          <Btn size="sm" variant="ghost" @click="editOpen = true"><Pencil /> 编辑连接</Btn>
        </template>
        <KeyValue :items="connection" />
      </DetailSection>

      <!-- 额度 -->
      <DetailSection v-if="c.providerUsage.supported" title="额度" note="上游账户">
        <template #actions>
          <Btn size="sm" variant="ghost" :loading="busy === 'usage'" @click="refreshUsage"><RefreshCw /> 刷新额度</Btn>
        </template>
        <div class="panel"><UsageBlock :usage="c.providerUsage" /></div>
      </DetailSection>

      <!-- 本月用量 -->
      <DetailSection title="本月用量" :note="compare?.previousComplete ? '右上角对比上月同期' : ''">
        <UsageTiles v-if="tiles.length" :tiles="tiles" />
        <p v-else class="faint small">本月还没有经过这个渠道的请求。</p>
      </DetailSection>

      <!-- 模型: list, test and fault clearing in one place -->
      <DetailSection title="模型" :note="probeSummary.ok || probeSummary.fail ? `${c.modelCount} 个 · 测试通过 ${probeSummary.ok} · 失败 ${probeSummary.fail}` : `${c.modelCount} 个`">
        <template #actions>
          <Btn v-if="hasFaults" size="sm" variant="ghost" :loading="busy === 'errors'" @click="clearErrors"><Eraser /> 清除模型故障</Btn>
          <Btn size="sm" variant="ghost" :loading="probingAll" :disabled="probing && !probingAll" title="逐个测试连通性，不会修改冷却状态" @click="probeAll"><Play /> 测试全部</Btn>
          <Btn size="sm" variant="ghost" @click="modelsOpen = true"><Layers /> 管理模型</Btn>
        </template>
        <ModelList>
          <ModelListRow v-for="m in sortedModels" :id="m.alias" :key="m.real" :off="m.disabled || m.globallyDisabled" :meta="modelMeta(m)">
            <template #badges>
              <Badge v-if="m.globallyDisabled" tone="neutral" title="模型中心已全局停用这个模型">全局停用</Badge>
              <Badge v-else-if="m.disabled" tone="neutral" title="模型中心里对这个渠道停用了这个模型">本渠道停用</Badge>
              <Badge v-if="modelProblem(m)" :tone="modelProblem(m)!.tone">{{ modelProblem(m)!.text }}</Badge>
              <span v-if="probes[m.real]" class="probe" :class="probes[m.real].status">
                <LoaderCircle v-if="probes[m.real].status === 'running'" class="spin" />
                <CircleCheck v-else-if="probes[m.real].status === 'ok'" />
                <CircleX v-else />
                <span v-if="probes[m.real].ms != null" class="mono">{{ probes[m.real].ms }}ms</span>
              </span>
            </template>
            <p v-if="m.real !== m.alias" class="sub mono">上游名 {{ m.real }}</p>
            <p v-if="probes[m.real]?.reason" class="sub c-red">{{ probes[m.real].reason }}</p>
            <p v-if="probes[m.real]?.note" class="sub c-green">{{ probes[m.real].note }}</p>
            <div v-if="m.cooldownKind === 'quota'" class="quota">
              <p>原因：周/月额度已用尽</p>
              <p v-if="m.cooldownUntil">恢复：{{ dateTime(m.cooldownUntil) }}（{{ remaining(m.cooldownUntil).replace(' · ', '') }}）</p>
              <p>调度：恢复前自动跳过本渠道这个模型</p>
            </div>
            <p class="sub">
              <template v-if="statsByModel[m.real]">本月 {{ integer(statsByModel[m.real].total) }} 次 · {{ localUsageLine(statsByModel[m.real]) }}</template>
              <template v-else>本月暂无调用</template>
            </p>
            <p v-if="m.totalRequests" class="sub">
              近期 {{ integer(m.totalRequests) }} 次 · 连接 {{ ms(m.averageConnectMilliseconds) }} · 首字 {{ ms(m.averageFirstByteMilliseconds) }}<template v-if="m.score != null"> · 评分 {{ m.score.toFixed(2) }}</template>
              <template v-if="m.errorCount"> · <span class="c-orange">连续失败 {{ m.errorCount }}</span></template>
            </p>
            <template #side>
              <Btn icon size="sm" variant="ghost" :disabled="probing" :title="`测试 ${m.alias} 的连通性`" @click="probe(m.real)"><FlaskConical /></Btn>
            </template>
          </ModelListRow>
        </ModelList>
        <p v-if="otherModelStats.length" class="faint small">
          本月还有 {{ otherModelStats.length }} 个已不在列表中的模型产生过调用：
          <button v-for="m in otherModelStats" :key="m.finalModel" type="button" class="inline-link mono" @click="openModel(m.finalModel)">{{ m.finalModel }}</button>
        </p>
      </DetailSection>

      <!-- 维护 -->
      <DetailSection title="维护">
        <p class="small">亲和绑定：{{ c.affinityCount }} 个会话<template v-if="c.clientAffinityCount"> · 客户端 {{ c.clientAffinityCount }} 个</template></p>
        <div class="maint">
          <Btn size="sm" :loading="busy === 'affinity'" title="让已绑定到这个渠道的会话重新选择渠道" @click="clearAffinity"><Link2Off /> 清除亲和绑定</Btn>
          <Btn size="sm" variant="ghost" @click="goLogs({ channel: id })"><ListFilter /> 查看相关请求</Btn>
        </div>
      </DetailSection>

      <section class="danger">
        <div>
          <p>删除渠道</p>
          <p class="faint small">同时清理这个渠道的冷却与亲和记录，并从负载均衡队列中移除，无法撤销。</p>
        </div>
        <Btn size="sm" variant="danger" :loading="busy === 'delete'" @click="remove"><Trash2 /> 删除渠道</Btn>
      </section>

      <ChannelEditModal :open="editOpen" :channel="c" @close="editOpen = false" @saved="afterSave" />
      <ChannelModelsModal :open="modelsOpen" :channel="c" @close="modelsOpen = false" @saved="afterSave" />
      <CompatModal :open="compatOpen" :channel="c" @close="compatOpen = false" @saved="(notifyChannelsChanged(), load())" />
    </template>
    <div v-else class="sk"><Skeleton h="44px" /><Skeleton h="90px" /><Skeleton h="110px" /><Skeleton h="180px" /></div>
  </div>
</template>

<style scoped>
.d {
  padding: 16px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}
.hero {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.top {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.titles {
  flex: 1;
  min-width: 0;
}
.name {
  font-size: 17px;
  font-weight: 650;
  word-break: break-word;
}
.state {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  font-size: 12.5px;
  font-weight: 600;
}
.state .faint {
  font-weight: 400;
}
.t-green { color: var(--green); }
.t-yellow { color: var(--yellow); }
.t-orange { color: var(--orange); }
.t-red { color: var(--red); }
.t-neutral { color: var(--text-2); }
.maint {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.panel {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.inline-link:hover {
  color: var(--accent);
}
.quota {
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 10%, transparent);
}
.inline-link {
  margin-left: 6px;
  font-size: 11.5px;
  text-decoration: underline dotted;
}
.small {
  font-size: 11.5px;
  line-height: 1.5;
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
  flex-shrink: 0;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.settings {
  display: flex;
  flex-direction: column;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  overflow: hidden;
}
.danger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  box-shadow: inset 0 0 0 0.5px color-mix(in srgb, var(--red) 35%, transparent);
  background: color-mix(in srgb, var(--red) 4%, transparent);
}
.probe {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
}
.probe svg {
  width: 14px;
  height: 14px;
}
.probe.ok {
  color: var(--green);
}
.probe.fail {
  color: var(--red);
}
.probe.running {
  color: var(--blue);
}
.spin {
  animation: spin 0.9s linear infinite;
}
</style>
