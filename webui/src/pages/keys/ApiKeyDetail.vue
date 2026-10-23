<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Gauge, KeySquare, ListFilter, Pencil, RefreshCcw, RotateCcw, Trash2, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { onEvent } from '@/api/events'
import { goLogs, openChannel, openModel } from '@/links'
import { channelName, refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { closeInspector, toast } from '@/stores/ui'
import { dateTime, integer } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Meter from '@/components/ui/Meter.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import StatusDot from '@/components/ui/StatusDot.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { channelProvider } from '@/utils/providers'
import Switch from '@/components/ui/Switch.vue'
import DetailSection from '@/components/DetailSection.vue'
import UsageTiles, { type UsageTile } from '@/components/UsageTiles.vue'
import { cacheText, cost3, toUsage, tokens, tps } from '@/pages/channels/meta'
import { deltas, loadCompare, type Compare } from '@/utils/compare'
import KeyChannelsModal from './KeyChannelsModal.vue'
import KeyLimitsModal from './KeyLimitsModal.vue'
import KeyMcpModal from './KeyMcpModal.vue'
import KeyModelsModal from './KeyModelsModal.vue'
import KeyRegenModal from './KeyRegenModal.vue'
import KeySecret from './KeySecret.vue'
import SecretModal from './SecretModal.vue'
import { MCP_TOOLS, SOURCE_LABEL, keySecret, notifyKeysChanged, sourceLabel, waitText, type ApiKey } from './meta'

type Stats = Schemas['ApiKeyStatsData']

const props = defineProps<{ id: string }>()

const data = ref<ApiKey | null>(null)
const stats = ref<Stats | null>(null)
const error = ref('')
const notFound = ref(false)
const busy = ref('')
const modal = ref<'' | 'models' | 'channels' | 'mcp' | 'limits' | 'regen' | 'custom'>('')
const secret = ref<{ value: string; title: string } | null>(null)
let timer: number | undefined

async function load(): Promise<void> {
  try {
    data.value = (await api.get<ApiKey>(`/api-keys/${enc(props.id)}`)).data
    error.value = ''
    notFound.value = false
  } catch (err) {
    notFound.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND'
    error.value = notFound.value ? '这个 Key 不存在，可能已被删除。' : errorTitle(err)
    return
  }
  api
    .get<Stats>(`/api-keys/${enc(props.id)}/stats`)
    .then((r) => (stats.value = r.data))
    .catch(() => undefined)
  loadCompare({ period: 'month', apiKey: data.value.name })
    .then((x) => (compare.value = x))
    .catch(() => undefined)
}

const off = onEvent('runtime.snapshot', (s) => {
  const row = s.apiKeys.find((r) => r.keyName === data.value?.name)
  if (!row || !data.value) return
  data.value = { ...data.value, limiter: { ...data.value.limiter, inFlight: row.inFlight, waiting: row.waiting, oldestWaitSeconds: row.oldestWaitSeconds } }
})
onMounted(() => {
  load()
  refreshCatalog().catch(() => undefined)
  timer = window.setInterval(() => document.visibilityState === 'visible' && !modal.value && !busy.value && load(), 30_000)
})
onBeforeUnmount(() => {
  off()
  window.clearInterval(timer)
})
watch(() => props.id, load)

const k = computed(() => data.value)
const lim = computed(() => data.value?.limiter)
const limited = computed(() => Boolean(lim.value && lim.value.enabled && !lim.value.unlimited && lim.value.maxConcurrent > 0))
const byModel = computed(() => [...(stats.value?.byModel || data.value?.modelStats || [])].sort((a, b) => b.usage.total - a.usage.total))
const overall = computed(() => stats.value?.overall || data.value?.monthStats)
/** This month against last month up to the same point. */
const compare = ref<Compare | null>(null)
const tiles = computed<UsageTile[]>(() => {
  const u = toUsage(overall.value)
  if (!u || !u.total) return []
  const share = u.cacheReadRatio != null ? u.cacheReadRatio * 100 : u.prompt ? (u.cacheRead / u.prompt) * 100 : null
  const d = deltas(compare.value, 'month')
  return [
    { k: '读入 ↑（含缓存）', v: tokens(u.prompt), delta: d.prompt },
    { k: '写出 ↓', v: tokens(u.output), delta: d.output },
    { k: '金额', v: cost3(u.costTicks), delta: d.cost },
    { k: '缓存读取', v: tokens(u.cacheRead), s: share !== null ? `占读入 ${share >= 99.95 && share < 100 ? '99.9' : share.toFixed(1)}%` : '', delta: d.cacheRead },
    { k: '平均速度', v: tps(u.avgTps), s: u.maxTps !== null ? `峰值 ${tps(u.maxTps)} · 最低 ${tps(u.minTps)}` : '' },
    { k: '请求', v: integer(u.total), s: u.error ? `失败 ${integer(u.error)}` : '全部成功', bad: Boolean(u.error), delta: d.requests },
  ]
})

// Update responses carry neither the plaintext key (only list/get do) nor usage (monthStats
// comes back zeroed, historyTotal null). Keep what we already have — or the freshly generated
// key after a key change — so the panel never drops to the masked hint or to "闲置", then
// re-read the key in the background.
function saved(next: ApiKey, newSecret?: string): void {
  const prev = data.value
  data.value = {
    ...next,
    secret: newSecret ?? next.secret ?? prev?.secret ?? null,
    historyTotal: next.historyTotal ?? prev?.historyTotal ?? null,
    monthStats: prev?.monthStats ?? next.monthStats,
    modelStats: prev?.modelStats ?? next.modelStats,
  }
  notifyKeysChanged()
  load()
}

async function patch(body: Schemas['ApiKeyUpdateRequest'], text: string): Promise<void> {
  if (!data.value) return
  const field = Object.keys(body)[0]
  const prev = data.value
  data.value = { ...prev, ...(body as Partial<ApiKey>) } // optimistic
  busy.value = field
  try {
    const res = await api.patch<ApiKey>(`/api-keys/${enc(props.id)}`, body, { ifMatch: prev.revision })
    saved(res.data)
    toast(text, 'success')
  } catch (err) {
    data.value = prev
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') load()
  } finally {
    busy.value = ''
  }
}

async function resetLimiter(): Promise<void> {
  busy.value = 'reset'
  try {
    const res = await api.post<ApiKey['limiter']>(`/api-keys/${enc(props.id)}/actions/reset-limiter`)
    if (data.value) data.value = { ...data.value, limiter: res.data }
    toast('已重置限流状态', 'success', '正在进行的请求不受影响。')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
  }
}

function onSecret(next: ApiKey, value: string, generated: boolean): void {
  saved(next, value)
  secret.value = { value, title: generated ? '新密钥已生成' : '密钥已替换' }
}

async function remove(): Promise<void> {
  const d = data.value
  if (!d) return
  const ok = await confirmAction({
    title: `删除 API Key「${d.name}」？`,
    detail: `密钥末尾：${d.maskedHint}\n删除后，使用这个 Key 的下游客户端会立即无法访问，不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  busy.value = 'delete'
  try {
    await api.del(`/api-keys/${enc(props.id)}`, { ifMatch: d.revision })
    toast(`已删除 ${d.name}`, 'success')
    notifyKeysChanged()
    closeInspector()
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    load()
  } finally {
    busy.value = ''
  }
}

const statusInfo = computed(() => {
  const d = data.value
  if (!d) return { text: '', tone: 'gray' as const }
  if (!d.enabled) return { text: '已停用', tone: 'red' as const }
  if (d.limiter.inFlight || d.limiter.waiting) return { text: '使用中', tone: 'blue' as const }
  if (d.monthStats.total) return { text: '活跃', tone: 'green' as const }
  return { text: '闲置', tone: 'gray' as const }
})

const mcpText = computed(() => {
  const t = data.value?.mcpTools || []
  if (!t.length) return '跟随全局'
  return t.map((x) => MCP_TOOLS.find((m) => m.value === x)?.label || x).join('、')
})
</script>

<template>
  <div class="d">
    <div v-if="error" class="err">
      <TriangleAlert />
      <span>{{ error }}</span>
      <Btn v-if="!notFound" size="sm" @click="load">重试</Btn>
    </div>
    <template v-else-if="k && lim">
      <section class="hero">
        <div class="top">
          <StatusDot :tone="statusInfo.tone" :pulse="statusInfo.tone === 'blue'" />
          <p class="name mono">{{ k.name }}</p>
          <span class="st" :class="`t-${statusInfo.tone}`">{{ statusInfo.text }}</span>
          <span class="grow" />
          <Switch :model-value="k.enabled" :loading="busy === 'enabled'" label="启用 Key" @update:model-value="patch({ enabled: $event }, $event ? '已启用' : '已停用')" />
        </div>
        <KeySecret :secret="keySecret(k)" :masked="k.maskedHint" size="md" />
        <p class="sub">{{ SOURCE_LABEL[k.source] || '来源未知' }} · 第 {{ k.order }} 位 · {{ k.enabled ? '持有这个 Key 的客户端可以正常调用' : '已停用，使用这个 Key 的请求会被拒绝' }}</p>
        <div class="actions">
          <Btn @click="goLogs({ apiKey: k.name })"><ListFilter /> 查看相关请求</Btn>
          <Btn @click="modal = 'regen'"><RefreshCcw /> 重新生成</Btn>
          <Btn @click="modal = 'custom'"><KeySquare /> 替换为自定义密钥</Btn>
        </div>
      </section>

      <DetailSection title="权限">
        <div class="rows">
          <div class="row col">
            <div class="r-head">
              <span>允许的模型</span>
              <button type="button" class="edit" @click="modal = 'models'"><Pencil /> {{ k.allowedModels.length ? `${k.allowedModels.length} 个` : '不限制' }}</button>
            </div>
            <div v-if="k.allowedModels.length" class="chips">
              <button v-for="m in k.allowedModels" :key="m" type="button" class="chip mono" @click="openModel(m)">{{ m }}</button>
            </div>
          </div>
          <div class="row col">
            <div class="r-head">
              <span>允许的渠道</span>
              <button type="button" class="edit" @click="modal = 'channels'">
                <Pencil /> {{ k.channelBindingEnabled ? `已绑定 ${(k.allowedChannels || []).length} 个` : (k.allowedChannels || []).length ? `已选 ${(k.allowedChannels || []).length} 个（未启用）` : '不限制' }}
              </button>
            </div>
            <div v-if="(k.allowedChannels || []).length" class="chips" :class="{ dim: !k.channelBindingEnabled }">
              <button v-for="c in k.allowedChannels || []" :key="c" type="button" class="chip ch" @click="openChannel(c)"><ProviderIcon :provider="channelProvider(c)" :size="16" />{{ channelName(c) }}</button>
            </div>
          </div>
          <div class="row">
            <span>图片接口</span>
            <Switch :model-value="k.allowImages" :loading="busy === 'allowImages'" label="图片接口" @update:model-value="patch({ allowImages: $event }, $event ? '已允许图片接口' : '已禁用图片接口')" />
          </div>
          <div class="row">
            <span>视频接口</span>
            <Switch :model-value="k.allowVideos" :loading="busy === 'allowVideos'" label="视频接口" @update:model-value="patch({ allowVideos: $event }, $event ? '已允许视频接口' : '已禁用视频接口')" />
          </div>
          <div class="row">
            <span>MCP</span>
            <Switch :model-value="k.allowMcp" :loading="busy === 'allowMcp'" label="MCP" @update:model-value="patch({ allowMcp: $event }, $event ? '已启用 MCP' : '已禁用 MCP')" />
          </div>
          <div class="row">
            <span>MCP 工具 <span class="faint small">{{ mcpText }}</span></span>
            <button type="button" class="edit" :disabled="!k.allowMcp" @click="modal = 'mcp'"><Pencil /> 选择</button>
          </div>
        </div>
      </DetailSection>

      <DetailSection title="本月使用统计" :note="stats ? `自 ${dateTime(stats.since).slice(0, 5)} 起${compare?.previousComplete ? ' · 右上角对比上月同期' : ''}` : ''">
        <UsageTiles v-if="tiles.length" :tiles="tiles" />
        <p v-else class="faint small">本月暂无调用</p>
        <template v-if="byModel.length">
          <p class="sub-t">按模型</p>
          <ul class="models">
            <li v-for="m in byModel" :key="m.model">
              <div class="m-top">
                <button type="button" class="m-name mono truncate" :title="m.model" @click="openModel(m.model)">{{ m.model }}</button>
                <span class="m-v num">{{ cost3(m.usage.costTicks) }}</span>
              </div>
              <p class="m-line num">
                {{ integer(m.usage.total) }} 次 · <span class="c-green">✓ {{ integer(m.usage.successCount) }}</span> · <span :class="m.usage.errorCount ? 'c-red' : 'faint'">✗ {{ integer(m.usage.errorCount) }}</span>
                · ↑ {{ tokens(m.usage.promptTokens) }} · ↓ {{ tokens(m.usage.outputTokens) }}
                <template v-if="m.usage.cacheReadTokens"> · 缓存 {{ cacheText(m.usage.cacheReadTokens, m.usage.cacheReadRatio, m.usage.promptTokens) }}</template>
              </p>
            </li>
          </ul>
        </template>
        <Skeleton v-else-if="!stats" h="60px" r="10px" />
      </DetailSection>

      <DetailSection title="实时负载">
        <template #actions>
          <Btn size="sm" variant="ghost" @click="modal = 'limits'"><Gauge /> 限流设置</Btn>
          <Btn size="sm" variant="ghost" :loading="busy === 'reset'" @click="resetLimiter"><RotateCcw /> 重置</Btn>
        </template>
        <div class="load">
          <div>
            <p class="k">在途请求</p>
            <p class="v">{{ lim.inFlight }}<span class="of"> / {{ limited ? lim.maxConcurrent : '不限' }}</span></p>
            <Meter v-if="limited" :value="(lim.inFlight / lim.maxConcurrent) * 100" />
          </div>
          <div>
            <p class="k">排队</p>
            <p class="v" :class="{ 'c-orange': lim.waiting }">{{ lim.waiting }}<span class="of"> / {{ lim.maxQueue }}</span></p>
            <Meter v-if="lim.maxQueue" :value="(lim.waiting / lim.maxQueue) * 100" />
          </div>
          <div>
            <p class="k">最久已等待</p>
            <p class="v">{{ lim.oldestWaitSeconds ? waitText(lim.oldestWaitSeconds) : '—' }}</p>
          </div>
        </div>
        <p class="faint small">
          限流{{ lim.enabled ? '开启' : '关闭' }}（{{ sourceLabel(lim.enabledSource) }}）· 并发上限 {{ lim.unlimited ? '不限' : lim.maxConcurrent }}（{{ sourceLabel(lim.maxConcurrentSource) }}）· 队列 {{ lim.maxQueue }}（{{ sourceLabel(lim.maxQueueSource) }}）· 最长等待
          {{ waitText(lim.queueWaitSeconds) }}（{{ sourceLabel(lim.queueWaitSource) }}）
        </p>
      </DetailSection>

      <DetailSection title="危险操作">
        <div class="maint">
          <Btn variant="danger" :loading="busy === 'delete'" @click="remove"><Trash2 /> 删除这个 Key</Btn>
        </div>
      </DetailSection>

      <KeyModelsModal :open="modal === 'models'" :k="k" @close="modal = ''" @saved="saved" />
      <KeyChannelsModal :open="modal === 'channels'" :k="k" @close="modal = ''" @saved="saved" />
      <KeyMcpModal :open="modal === 'mcp'" :k="k" @close="modal = ''" @saved="saved" />
      <KeyLimitsModal :open="modal === 'limits'" :k="k" @close="modal = ''" @saved="saved" />
      <KeyRegenModal :open="modal === 'regen' || modal === 'custom'" :k="k" :mode="modal === 'custom' ? 'custom' : 'generate'" @close="modal = ''" @done="(n, s) => onSecret(n, s, modal === 'regen')" />
      <SecretModal :secret="secret?.value || null" :name="k.name" :title="secret?.title" note="下游客户端需要更新为这个新密钥，旧密钥已经失效。" @close="secret = null" />
    </template>
    <div v-else class="sk"><Skeleton h="24px" /><Skeleton h="90px" /><Skeleton h="80px" /><Skeleton h="140px" /></div>
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
  gap: 10px;
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
.sub {
  font-size: 12px;
  color: var(--text-2);
}
.enable {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.e-t {
  font-weight: 600;
}
.e-s {
  font-size: 11.5px;
  color: var(--text-3);
}
.actions,
.maint {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.load,
.mini {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  overflow: hidden;
}
.load > div,
.mini > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 9px 12px;
  border-right: 0.5px solid var(--hairline);
  border-bottom: 0.5px solid var(--hairline);
}
.load > div:last-child,
.mini > div:nth-child(3n) {
  border-right: 0;
}
.load > div,
.mini > div:nth-child(n + 4) {
  border-bottom: 0;
}
.k {
  font-size: 11px;
  color: var(--text-3);
}
.v {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.of {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-3);
}
.rows {
  display: flex;
  flex-direction: column;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 40px;
  padding: 6px 12px;
  border-bottom: 0.5px solid var(--hairline);
  font-size: 12.5px;
}
.row:last-child {
  border-bottom: 0;
}
.row.col {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  padding: 8px 12px;
}
.r-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.edit {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 28px;
  padding: 0 6px;
  border-radius: 6px;
  font-size: 12px;
  color: var(--accent);
}
.edit:hover:not(:disabled) {
  background: var(--accent-soft);
}
.edit:disabled {
  color: var(--text-3);
}
.edit svg {
  width: 12px;
  height: 12px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chips.dim {
  opacity: 0.6;
}
.chip {
  max-width: 100%;
  min-height: 24px;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  text-align: left;
  word-break: break-all;
  background: var(--surface);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.chip:hover,
.m-name:hover {
  color: var(--accent);
}
.models {
  list-style: none;
  margin: 0;
  padding: 0;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.models li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 7px 12px;
  border-bottom: 0.5px solid var(--hairline);
}
.models li:last-child {
  border-bottom: 0;
}
.m-top {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.m-top .m-name {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 600;
}
.m-line {
  font-size: 11.5px;
  color: var(--text-2);
  overflow-wrap: anywhere;
}
.sub-t {
  margin-top: 4px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
}
.st {
  font-size: 12.5px;
  font-weight: 600;
}
.t-green { color: var(--green); }
.t-blue { color: var(--blue); }
.t-red { color: var(--red); }
.t-gray { color: var(--text-3); }
.grow {
  flex: 1;
}
.m-name {
  text-align: left;
}
.m-v {
  text-align: right;
  white-space: nowrap;
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

.chip.ch {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
</style>
