<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Brain, Check, Eraser, ListChecks, ListOrdered, RefreshCw, RotateCcw, Scale, Shuffle, TriangleAlert } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { openModel } from '@/links'
import { catalog, refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Page from '@/components/ui/Page.vue'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Badge from '@/components/ui/Badge.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Empty from '@/components/ui/Empty.vue'
import { displayStatus, loadModels, modelStore, statusRank } from '@/pages/models/shared'
import OrderEditor from './OrderEditor.vue'
import BulkOrderModal from './BulkOrderModal.vue'

type LoadBalancing = Schemas['LoadBalancingData']
type Mode = LoadBalancing['mode']
type OrderData = Schemas['OrderData']
type Affinity = Schemas['AffinityClearData']

const route = useRoute()
const router = useRouter()

const MODES: { value: Mode; title: string; icon: typeof Brain; what: string; when: string }[] = [
  {
    value: 'smart',
    title: '智能调度',
    icon: Brain,
    what: '按最近一段时间的成功率和速度给每个渠道打分，优先用分高的，同时留 20% 的流量探索其他渠道。',
    when: '适合渠道多、质量参差不齐，希望自动避开慢或出错渠道的情况。',
  },
  {
    value: 'order',
    title: '顺序调度',
    icon: ListOrdered,
    what: '按渠道注册的先后顺序依次尝试，前一个失败或不可用时才用下一个。',
    when: '适合主备关系明确、只想在主渠道出问题时才切换的情况。',
  },
  {
    value: 'priority',
    title: '优先级调度',
    icon: ListChecks,
    what: '按你设定的顺序使用：模型专属顺序优先，没有专属顺序的模型使用统一渠道顺序。',
    when: '适合想先用包月或免费账号、额度用完再用按量付费渠道等需要精确控制的情况。',
  },
]

// ---- mode ----
const mode = ref<Mode | null>(null)
const lbRev = ref('')
const modeBusy = ref<Mode | ''>('')
const loadError = ref('')

function bump(rev: string | undefined): void {
  if (rev) lbRev.value = rev
}

async function loadMode(): Promise<void> {
  try {
    const res = await api.get<LoadBalancing>('/load-balancing')
    mode.value = res.data.mode
    bump(res.data.revision)
    loadError.value = ''
  } catch (err) {
    loadError.value = errorTitle(err)
  }
}

async function setMode(m: Mode): Promise<void> {
  if (m === mode.value || modeBusy.value) return
  modeBusy.value = m
  try {
    const res = await api.patch<LoadBalancing>('/load-balancing', { mode: m }, { ifMatch: lbRev.value })
    mode.value = res.data.mode
    bump(res.data.revision)
    toast(`已切换到${MODES.find((x) => x.value === m)?.title}`, 'success')
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('负载均衡配置已在别处被修改', 'warning', '已载入最新设置，请重新选择。', 7000)
      loadMode()
    } else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    modeBusy.value = ''
  }
}

// ---- global order ----
const globalOrder = ref<OrderData | null>(null)
const globalSaving = ref(false)

async function loadGlobal(): Promise<void> {
  try {
    const res = await api.get<OrderData>('/load-balancing/channel-order')
    globalOrder.value = res.data
    bump(res.data.revision)
  } catch (err) {
    loadError.value = errorTitle(err)
  }
}

async function saveGlobal(order: string[]): Promise<void> {
  globalSaving.value = true
  try {
    const res = await api.put<OrderData>('/load-balancing/channel-order', { order }, { ifMatch: lbRev.value })
    globalOrder.value = res.data
    bump(res.data.revision)
    toast('已保存统一渠道顺序', 'success')
    if (selectedModel.value) loadModelOrder(selectedModel.value)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('渠道顺序已在别处被修改', 'warning', '已载入最新顺序，请重新调整。', 7000)
      loadGlobal()
    } else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    globalSaving.value = false
  }
}

// ---- per-model ----
const modelQ = ref('')
const modelFilter = ref<'all' | 'custom'>('all')
const selectedModel = ref('')
const modelOrder = ref<OrderData | null>(null)
const modelOrderError = ref('')
const modelSaving = ref(false)
const modelDeleting = ref(false)
const sourceOf = ref<Record<string, string>>({})
const bulkOpen = ref(false)
const modelCard = ref<HTMLElement | null>(null)
const mscroll = ref<HTMLElement | null>(null)

/** Every chat model in the Model Center's order (status, then name); off ones stay listed but muted. */
const chatModels = computed(() =>
  modelStore.models
    .filter((m) => m.identity.type === 'chat')
    .map((m) => ({ id: m.modelId, status: displayStatus(m), rank: statusRank(m), sources: (m.sources || []).length }))
    .sort((a, b) => a.rank - b.rank || a.status.label.localeCompare(b.status.label, 'zh-CN') || a.id.localeCompare(b.id, 'en', { numeric: true })),
)
const routingModels = computed(() => chatModels.value.filter((m) => m.sources).map((m) => m.id))
const visibleModels = computed(() => {
  const s = modelQ.value.trim().toLowerCase()
  return chatModels.value.filter((m) => (!s || m.id.toLowerCase().includes(s)) && (modelFilter.value === 'all' || sourceOf.value[m.id] === 'modelOverride'))
})
const customCount = computed(() => Object.values(sourceOf.value).filter((s) => s === 'modelOverride').length)

/** Order source of every model in one call (listModelChannelOrders), to mark custom orders. */
async function loadOrderSources(): Promise<void> {
  try {
    const res = await api.get<Schemas['BulkOrderData']>('/load-balancing/model-orders')
    const out: Record<string, string> = {}
    for (const o of res.data.orders) if (o.modelId) out[o.modelId] = o.source
    sourceOf.value = out
    bump(res.data.revision)
  } catch {
    /* badges are optional; the editor still loads each model's own order */
  }
}

async function loadModelOrder(id: string): Promise<void> {
  modelOrderError.value = ''
  try {
    const res = await api.get<OrderData>(`/load-balancing/model-orders/${enc(id)}`)
    if (selectedModel.value !== id) return
    modelOrder.value = res.data
    bump(res.data.revision)
    sourceOf.value = { ...sourceOf.value, [id]: res.data.source }
  } catch (err) {
    if (selectedModel.value !== id) return
    modelOrder.value = null
    modelOrderError.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND' ? '这个模型目前没有可调度的渠道。' : errorTitle(err)
  }
}

async function selectModel(id: string, scroll = false): Promise<void> {
  if (id === selectedModel.value) return
  const editor = modelEditor.value
  if (editor?.dirty) {
    const ok = await confirmAction({ title: '放弃未保存的顺序？', detail: `${selectedModel.value} 的顺序调整还没有保存。`, confirmText: '放弃', danger: true })
    if (!ok) return
  }
  selectedModel.value = id
  modelOrder.value = null
  if ((route.query.model || '') !== id) router.replace({ query: { ...route.query, model: id } })
  loadModelOrder(id)
  if (scroll) {
    await nextTick()
    modelCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    const box = mscroll.value
    const item = box?.querySelector<HTMLElement>('.mitem.on')
    if (box && item) box.scrollTop = item.offsetTop - box.clientHeight / 2 + item.clientHeight / 2
  }
}
const modelEditor = ref<InstanceType<typeof OrderEditor> | null>(null)

async function saveModel(order: string[]): Promise<void> {
  const id = selectedModel.value
  modelSaving.value = true
  try {
    const res = await api.put<OrderData>(`/load-balancing/model-orders/${enc(id)}`, { order }, { ifMatch: lbRev.value })
    modelOrder.value = res.data
    bump(res.data.revision)
    sourceOf.value = { ...sourceOf.value, [id]: res.data.source }
    toast(`已保存 ${id} 的专属顺序`, 'success')
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('渠道顺序已在别处被修改', 'warning', '已载入最新顺序，请重新调整。', 7000)
      loadModelOrder(id)
    } else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    modelSaving.value = false
  }
}

async function deleteModel(): Promise<void> {
  const id = selectedModel.value
  const ok = await confirmAction({
    title: `恢复 ${id} 的默认顺序？`,
    detail: '会删除这个模型的专属顺序，之后按统一渠道顺序调度。',
    confirmText: '恢复默认',
    danger: true,
  })
  if (!ok) return
  modelDeleting.value = true
  try {
    await api.del(`/load-balancing/model-orders/${enc(id)}`, { ifMatch: lbRev.value })
    toast('已恢复统一渠道默认', 'success', id)
    await loadModelOrder(id)
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    modelDeleting.value = false
  }
}

async function onBulkSaved(rev: string): Promise<void> {
  bulkOpen.value = false
  bump(rev)
  loadOrderSources()
  if (selectedModel.value) loadModelOrder(selectedModel.value)
}

// ---- affinity ----
const affinityBusy = ref('')
const FAMILY: Record<string, string> = { anthropic: 'Claude 协议', openai: 'OpenAI 协议' }

async function clearAffinity(family?: 'anthropic' | 'openai'): Promise<void> {
  const summary = catalog.status?.affinitySummary as { server?: number; client?: number } | undefined
  const ok = await confirmAction({
    title: family ? `清除${FAMILY[family]}的会话亲和？` : '清除全部会话亲和？',
    detail:
      (family ? `只清除${FAMILY[family]}家族渠道上的绑定。` : '会同时清除会话指纹亲和与客户端软亲和。') +
      (summary && !family ? `当前约有 ${summary.server ?? 0} 条会话绑定、${summary.client ?? 0} 条客户端绑定。` : '') +
      '清除后，正在进行的对话下一次请求可能换到别的渠道，缓存命中率会暂时下降。',
    confirmText: '确认清除',
    danger: true,
  })
  if (!ok) return
  affinityBusy.value = family || 'all'
  try {
    const res = await api.post<Affinity>(family ? `/affinity/families/${family}/actions/clear` : '/affinity/actions/clear')
    toast('已清除会话亲和', 'success', `会话指纹 ${res.data.fingerprintCount} 条，客户端 ${res.data.clientCount} 条`)
    refreshCatalog(true)
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    affinityBusy.value = ''
  }
}
const affinity = computed(() => catalog.status?.affinitySummary as { server?: number; client?: number } | undefined)

// ---- lifecycle ----
async function refreshAll(): Promise<void> {
  refreshCatalog(true)
  await Promise.all([loadMode(), loadGlobal(), loadModels(true)])
  loadOrderSources()
  if (selectedModel.value) loadModelOrder(selectedModel.value)
}

function applyQuery(): void {
  const m = route.query.model
  if (typeof m === 'string' && m && m !== selectedModel.value) selectModel(m, true)
}

onMounted(async () => {
  refreshCatalog()
  await Promise.all([loadMode(), loadGlobal(), loadModels()])
  applyQuery()
  if (!selectedModel.value && routingModels.value.length) selectModel(routingModels.value[0])
  loadOrderSources()
})
watch(() => route.query.model, applyQuery)

const notPriority = computed(() => mode.value !== null && mode.value !== 'priority')
</script>

<template>
  <Page title="负载均衡" subtitle="决定一个请求有多个可用账户或渠道时先用哪个。" width="normal">
    <template #actions>
      <Btn icon title="刷新" @click="refreshAll"><RefreshCw /></Btn>
    </template>

    <p v-if="loadError" class="err"><TriangleAlert /> {{ loadError }}</p>

    <section class="modes">
      <template v-if="mode === null && !loadError">
        <Skeleton v-for="i in 3" :key="i" h="150px" r="14px" />
      </template>
      <button
        v-for="m in MODES"
        v-else
        :key="m.value"
        type="button"
        class="mode"
        :class="{ on: mode === m.value }"
        :aria-pressed="mode === m.value"
        :disabled="Boolean(modeBusy)"
        @click="setMode(m.value)"
      >
        <span class="mode-head">
          <span class="mode-ic"><component :is="m.icon" /></span>
          <span class="mode-t">{{ m.title }}</span>
          <span v-if="mode === m.value" class="mode-ck"><Check /> 当前</span>
          <span v-else-if="modeBusy === m.value" class="faint small">切换中…</span>
        </span>
        <span class="mode-what">{{ m.what }}</span>
        <span class="mode-when">{{ m.when }}</span>
      </button>
    </section>

    <p v-if="notPriority" class="hint"><TriangleAlert /> 下面的渠道顺序只在「优先级调度」下生效。现在可以先调好保存，切换到优先级调度后立即使用。</p>

    <Card title="统一渠道顺序" subtitle="所有模型默认使用的顺序；模型没有专属顺序时按这里调度。越靠前越优先。">
      <template #icon><Scale /></template>
      <OrderEditor v-if="globalOrder" :order="globalOrder.order" :saving="globalSaving" @save="saveGlobal" />
      <div v-else class="sk"><Skeleton v-for="i in 5" :key="i" h="36px" r="8px" /></div>
    </Card>

    <div ref="modelCard" class="anchor">
      <Card title="按模型的渠道顺序" :subtitle="`模型专属顺序优先于统一渠道顺序 · ${customCount} 个模型已自定义`">
        <template #icon><Shuffle /></template>
        <template #actions>
          <Btn size="sm" @click="bulkOpen = true"><ListChecks /> 批量设置</Btn>
        </template>
        <div class="per">
          <aside class="mlist">
            <SearchField v-model="modelQ" placeholder="搜索模型" />
            <Segmented
              v-model="modelFilter"
              size="sm"
              :options="[
                { value: 'all', label: `全部 ${chatModels.length}` },
                { value: 'custom', label: `已自定义 ${customCount}` },
              ]"
            />
            <div ref="mscroll" class="mscroll">
              <template v-if="modelStore.loading && !chatModels.length">
                <Skeleton v-for="i in 6" :key="i" h="30px" r="7px" />
              </template>
              <button
                v-for="m in visibleModels"
                :key="m.id"
                type="button"
                class="mitem"
                :class="{ on: m.id === selectedModel, off: m.status.tone === 'neutral' }"
                :title="m.status.tone === 'neutral' ? `${m.status.label}：当前不参与调度` : undefined"
                @click="selectModel(m.id)"
              >
                <span class="mono truncate">{{ m.id }}</span>
                <Badge v-if="sourceOf[m.id] === 'modelOverride'" tone="blue">专属</Badge>
                <Badge :tone="m.status.tone" dot>{{ m.status.label }}</Badge>
              </button>
              <p v-if="!modelStore.loading && !visibleModels.length" class="faint small pad">没有匹配的模型</p>
            </div>
          </aside>
          <div class="medit">
            <template v-if="selectedModel">
              <div class="mhead">
                <button class="mono mname" @click="openModel(selectedModel)">{{ selectedModel }}</button>
                <Badge v-if="modelOrder" :tone="modelOrder.source === 'modelOverride' ? 'blue' : 'neutral'">
                  {{ modelOrder.source === 'modelOverride' ? '使用专属顺序' : '继承统一顺序' }}
                </Badge>
                <span class="spacer" />
                <Btn v-if="modelOrder?.source === 'modelOverride'" size="sm" variant="ghost" :loading="modelDeleting" @click="deleteModel"><RotateCcw /> 恢复默认</Btn>
              </div>
              <p class="faint small">只列出能提供这个模型的账户和渠道。保存后即成为这个模型的专属顺序。</p>
              <p v-if="modelOrderError" class="err small">{{ modelOrderError }}</p>
              <OrderEditor v-else-if="modelOrder" ref="modelEditor" :order="modelOrder.order" :saving="modelSaving" empty-text="这个模型目前没有可调度的渠道。" @save="saveModel" />
              <div v-else class="sk"><Skeleton v-for="i in 4" :key="i" h="36px" r="8px" /></div>
            </template>
            <Empty v-else title="选择一个模型" detail="从左侧选择模型，调整它专属的渠道顺序。" />
          </div>
        </div>
      </Card>
    </div>

    <Card title="会话亲和" subtitle="同一段对话尽量留在同一个渠道上，以提高缓存命中、保持上下文一致">
      <template #icon><Eraser /></template>
      <div class="aff">
        <p class="small">
          调整顺序或渠道出问题后，如果希望正在进行的对话立刻按新顺序分配，可以清除亲和绑定。
          <template v-if="affinity">当前约有 <b class="mono">{{ affinity.server ?? 0 }}</b> 条会话绑定、<b class="mono">{{ affinity.client ?? 0 }}</b> 条客户端绑定。</template>
        </p>
        <div class="aff-acts">
          <Btn variant="danger" :loading="affinityBusy === 'all'" @click="clearAffinity()"><Eraser /> 清除全部亲和</Btn>
          <Btn :loading="affinityBusy === 'anthropic'" @click="clearAffinity('anthropic')">仅清除 Claude 协议</Btn>
          <Btn :loading="affinityBusy === 'openai'" @click="clearAffinity('openai')">仅清除 OpenAI 协议</Btn>
        </div>
      </div>
    </Card>

    <BulkOrderModal
      :open="bulkOpen"
      :models="routingModels.map((id) => ({ value: id, hint: sourceOf[id] === 'modelOverride' ? '已有专属顺序' : undefined }))"
      :global-order="globalOrder?.order || []"
      :revision="lbRev"
      :initial="selectedModel ? [selectedModel] : []"
      @close="bulkOpen = false"
      @saved="onBulkSaved"
    />
  </Page>
</template>

<style scoped>
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg,
.hint svg {
  width: 15px;
  height: 15px;
  color: var(--orange);
  flex-shrink: 0;
}
.modes {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  align-items: stretch;
  gap: 12px;
}
.mode {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  padding: 14px 16px 16px;
  border-radius: var(--radius-lg);
  text-align: left;
  background: var(--surface);
  border: 0.5px solid var(--border);
  box-shadow: var(--card-highlight), var(--shadow-sm);
  transition: background 0.2s, border-color 0.2s, box-shadow 0.2s;
}
.mode:hover:not(:disabled) {
  background: var(--surface-hover);
  border-color: var(--border-strong);
}
.mode.on {
  border-color: transparent;
  box-shadow: var(--card-highlight), inset 0 0 0 1.5px var(--accent), var(--shadow-sm);
  background: color-mix(in srgb, var(--accent) 7%, var(--surface));
}
.mode-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.mode-ic {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  color: var(--accent);
  background: var(--accent-soft);
}
.mode-ic svg {
  width: 16px;
  height: 16px;
}
.mode-t {
  flex: 1;
  font-size: 14px;
  font-weight: 650;
}
.mode-ck {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
}
.mode-ck svg {
  width: 13px;
  height: 13px;
}
.mode-what {
  font-size: 12.5px;
  line-height: 1.6;
}
.mode-when {
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-3);
}
.hint {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 12.5px;
  color: var(--text-2);
  background: color-mix(in srgb, var(--orange) 11%, transparent);
}
.hint svg {
  margin-top: 1px;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.anchor {
  scroll-margin-top: 16px;
}
.per {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 16px;
  min-height: 360px;
}
.mlist {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.mscroll {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 1px;
  max-height: 460px;
  overflow-y: auto;
  padding-right: 2px;
}
.mitem {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 8px;
  border-radius: 7px;
  font-size: 12.5px;
  text-align: left;
}
.mitem .mono {
  flex: 1;
  min-width: 0;
}
.mitem:hover {
  background: var(--fill-3);
}
.mitem.on {
  background: var(--accent-soft);
  color: var(--accent);
}
.mitem.off .mono {
  color: var(--text-3);
  font-style: italic;
}
.mitem.off.on .mono {
  color: var(--accent);
}
.medit {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.mhead {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.mname {
  font-size: 14px;
  font-weight: 650;
  text-align: left;
  word-break: break-all;
}
.mname:hover {
  color: var(--accent);
  text-decoration: underline;
}
.spacer {
  flex: 1;
}
.aff {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.aff-acts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.small {
  font-size: 12px;
  line-height: 1.6;
}
.pad {
  padding: 8px;
}
@container (max-width: 760px) {
  .modes {
    grid-template-columns: minmax(0, 1fr);
  }
  .per {
    grid-template-columns: minmax(0, 1fr);
  }
  .mscroll {
    max-height: 220px;
  }
}
</style>
