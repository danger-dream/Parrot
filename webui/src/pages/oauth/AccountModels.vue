<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ListChecks, RefreshCw } from '@lucide/vue'
import { api, ApiError } from '@/api/client'
import { toast } from '@/stores/ui'
import { capacity, dateTime } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import DetailSection from '@/components/DetailSection.vue'
import ModelList from '@/components/ModelList.vue'
import ModelListRow from '@/components/ModelListRow.vue'
import { accountPath, errorToast, fetchAllPages, notifyChanged, runOperation } from './oauthApi'
import { effortText, metadataSourceLabel, TIER_LABEL, type ModelData } from './meta'
import { errorTitle } from '@/utils/errors'

const props = defineProps<{ accountId: string; lastSync?: string | null }>()

const items = ref<ModelData[]>([])
const revision = ref('')
const loading = ref(true)
const error = ref('')
const q = ref('')
const showAll = ref(false)
const busyId = ref('')
const editing = ref(false)
const selection = ref<string[]>([])
const savingBulk = ref(false)
const syncing = ref(false)
const syncNote = ref('')
const LIMIT = 8

async function load(): Promise<void> {
  try {
    const res = await fetchAllPages<ModelData>(accountPath(props.accountId, '/models'))
    items.value = res.items
    revision.value = res.revision
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}
watch(() => props.accountId, load, { immediate: true })

const unusable = (m: ModelData) => m.disabled || Boolean(m.globallyDisabled)
const cooling = (m: ModelData) => m.cooldownPermanent || Boolean(m.cooldownUntil && Date.parse(m.cooldownUntil) > Date.now())
// Usable models first (cooling ones on top), disabled ones last. The grouping is taken when the list
// loads, so flipping a switch does not make the row jump away from under the pointer.
const loadedOff = ref(new Set<string>())
watch(loading, (busy) => { if (!busy) loadedOff.value = new Set(items.value.filter(unusable).map((m) => m.modelId)) })
const sorted = computed(() =>
  [...items.value].sort((a, b) =>
    Number(loadedOff.value.has(a.modelId)) - Number(loadedOff.value.has(b.modelId))
    || Number(cooling(b)) - Number(cooling(a))
    || a.modelId.localeCompare(b.modelId)),
)
function coolText(m: ModelData): string {
  const t = dateTime(m.cooldownUntil!)
  return new Date(m.cooldownUntil!).toDateString() === new Date().toDateString() ? t.slice(6) : t
}
const filtered = computed(() => {
  const s = q.value.trim().toLowerCase()
  return s ? sorted.value.filter((m) => m.modelId.toLowerCase().includes(s) || m.name.toLowerCase().includes(s)) : sorted.value
})
const shown = computed(() => (showAll.value || q.value ? filtered.value : filtered.value.slice(0, LIMIT)))
const enabledCount = computed(() => items.value.filter((m) => !unusable(m)).length)

function applyList(data: { items: ModelData[]; revision: string }): void {
  // PATCH responses are paged; merge them into the full list we hold.
  const map = new Map(data.items.map((m) => [m.modelId, m]))
  items.value = items.value.map((m) => map.get(m.modelId) || m)
  revision.value = data.revision
}

async function patchModels(ids: string[], disabled: boolean): Promise<boolean> {
  try {
    const res = await api.patch<{ items: ModelData[]; revision: string }>(accountPath(props.accountId, '/models'), { modelIds: ids, disabled }, { ifMatch: revision.value })
    applyList(res.data)
    // The paged response may not include every changed id; reflect the request locally.
    const set = new Set(ids)
    items.value = items.value.map((m) => (set.has(m.modelId) ? { ...m, disabled } : m))
    return true
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('模型列表已在别处被修改', 'warning', '已重新载入，请再试一次。')
      await load()
    } else errorToast(err)
    return false
  }
}

async function toggle(m: ModelData, on: boolean): Promise<void> {
  busyId.value = m.modelId
  if (await patchModels([m.modelId], !on)) notifyChanged(props.accountId)
  busyId.value = ''
}

async function setMaxContext(m: ModelData, on: boolean): Promise<void> {
  busyId.value = m.modelId
  try {
    const res = await api.patch<{ items: ModelData[]; revision: string }>(
      accountPath(props.accountId, '/models/settings'),
      { modelId: m.modelId, maxContextDefault: on },
      { ifMatch: revision.value },
    )
    applyList(res.data)
    items.value = items.value.map((x) => (x.modelId === m.modelId ? { ...x, maxContextDefault: on } : x))
    toast(on ? '已默认使用最大上下文' : '已改为默认普通上下文', 'success')
  } catch (err) {
    errorToast(err)
    await load()
  } finally {
    busyId.value = ''
  }
}

function startBulk(): void {
  selection.value = items.value.filter((m) => !m.disabled).map((m) => m.modelId)
  editing.value = true
}
const bulkOptions = computed(() => sorted.value.map((m) => ({ value: m.modelId, hint: m.globallyDisabled ? '模型中心已全局停用' : m.name !== m.modelId ? m.name : undefined })))
const bulkDiff = computed(() => {
  const sel = new Set(selection.value)
  return {
    enable: items.value.filter((m) => m.disabled && sel.has(m.modelId)).map((m) => m.modelId),
    disable: items.value.filter((m) => !m.disabled && !sel.has(m.modelId)).map((m) => m.modelId),
  }
})
async function saveBulk(): Promise<void> {
  const { enable, disable } = bulkDiff.value
  savingBulk.value = true
  let ok = true
  if (disable.length) ok = await patchModels(disable, true)
  if (ok && enable.length) ok = await patchModels(enable, false)
  savingBulk.value = false
  if (ok) {
    toast('模型启停已保存', 'success', `启用 ${enable.length} 个，停用 ${disable.length} 个`)
    editing.value = false
    notifyChanged(props.accountId)
  }
}

async function sync(): Promise<void> {
  syncing.value = true
  const started = Date.now()
  syncNote.value = '正在同步模型目录…'
  const op = await runOperation(() => api.post(accountPath(props.accountId, '/models/actions/sync')), {
    success: '模型目录已同步',
    failure: '模型同步失败',
    onProgress: (o) => {
      syncNote.value = o.progress ? `正在同步… ${o.progress.current}/${o.progress.total}` : `正在同步模型目录… ${Math.round((Date.now() - started) / 1000)} 秒`
    },
  })
  syncing.value = false
  syncNote.value = ''
  if (op) {
    await load()
    notifyChanged(props.accountId)
  }
}

function ctx(m: ModelData): string {
  const parts: string[] = []
  const c = m.maxContextDefault && m.maxContextWindow ? m.maxContextWindow : m.contextWindow
  if (c) parts.push(`上下文 ${capacity(c)}`)
  if (m.maxContextWindow && m.contextWindow && m.maxContextWindow > m.contextWindow && !m.maxContextDefault) parts.push(`最大 ${capacity(m.maxContextWindow)}`)
  if (m.maxOutputTokens) parts.push(`输出 ${capacity(m.maxOutputTokens)}`)
  if (m.reasoningEfforts?.length) parts.push(`推理 ${effortText(m.reasoningEfforts)}`)
  if (m.serviceTier) parts.push(TIER_LABEL[m.serviceTier] || m.serviceTier)
  return parts.join(' · ')
}
const hasMaxCtx = (m: ModelData) => Boolean(m.maxContextWindow && m.contextWindow && m.maxContextWindow > m.contextWindow)
</script>

<template>
  <DetailSection title="模型" :note="items.length ? `${enabledCount}/${items.length} 可用` : ''">
    <template #actions>
      <Btn v-if="items.length && !editing" variant="ghost" size="sm" @click="startBulk"><ListChecks /> 批量</Btn>
      <Btn variant="ghost" size="sm" :loading="syncing" @click="sync"><RefreshCw /> 同步模型</Btn>
    </template>
    <p v-if="syncNote" class="progress"><span class="spin" /> {{ syncNote }}</p>
    <p v-if="error" class="faint small">模型列表加载失败：{{ error }}</p>
    <div v-else-if="loading" class="sk"><Skeleton v-for="i in 3" :key="i" h="34px" /></div>
    <p v-else-if="!items.length" class="faint small">还没有模型目录，点「同步模型」从上游拉取。</p>

    <template v-else-if="editing">
      <p class="faint small">勾选 = 启用。取消勾选的模型不会再调度到这个账号。</p>
      <CheckList v-model="selection" :options="bulkOptions" max-height="300px" />
      <div class="bulk-foot">
        <span class="faint small">将启用 {{ bulkDiff.enable.length }} 个，停用 {{ bulkDiff.disable.length }} 个</span>
        <Btn size="sm" variant="ghost" :disabled="savingBulk" @click="editing = false">取消</Btn>
        <Btn size="sm" variant="primary" :loading="savingBulk" :disabled="!bulkDiff.enable.length && !bulkDiff.disable.length" @click="saveBulk">保存</Btn>
      </div>
    </template>

    <template v-else>
      <SearchField v-if="items.length > LIMIT" v-model="q" placeholder="搜索模型" width="100%" />
      <ModelList>
        <ModelListRow
          v-for="m in shown"
          :id="m.modelId"
          :key="m.modelId"
          :off="unusable(m)"
          :meta="[m.name && m.name !== m.modelId ? m.name : '', ctx(m)].filter(Boolean).join(' · ')"
          :meta-title="metadataSourceLabel(m.metadataSource)"
        >
          <template #badges>
            <Badge v-if="m.globallyDisabled" tone="neutral" title="模型中心已全局停用这个模型，这里的开关只决定账号自己是否提供它">全局停用</Badge>
            <Badge v-if="m.cooldownPermanent" tone="red">永久冻结</Badge>
            <Badge v-else-if="cooling(m)" tone="orange">冷却至 {{ coolText(m) }}</Badge>
          </template>
          <button v-if="hasMaxCtx(m)" type="button" class="maxctx" :disabled="busyId === m.modelId" @click="setMaxContext(m, !m.maxContextDefault)">
            默认最大上下文：<b :class="m.maxContextDefault ? 'c-green' : ''">{{ m.maxContextDefault ? '开' : '关' }}</b>
            <span class="faint">（点击切换；请求里显式指定时以请求为准）</span>
          </button>
          <template #side>
            <Switch :model-value="!m.disabled" :loading="busyId === m.modelId" :label="m.disabled ? '启用模型' : '停用模型'" @update:model-value="toggle(m, $event)" />
          </template>
        </ModelListRow>
      </ModelList>
      <button v-if="!q && filtered.length > LIMIT" type="button" class="more" @click="showAll = !showAll">
        {{ showAll ? '收起' : `展开全部 ${filtered.length} 个模型` }}
      </button>
      <p v-if="lastSync" class="faint small">上次同步 {{ dateTime(lastSync) }}</p>
    </template>
  </DetailSection>
</template>

<style scoped>
.maxctx {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-2);
  text-align: left;
}
.more {
  align-self: flex-start;
  font-size: 12px;
  color: var(--accent);
}
.bulk-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.bulk-foot .faint {
  margin-right: auto;
}
.small {
  font-size: 11.5px;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.progress {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
}
.spin {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--accent);
  border-right-color: transparent;
  animation: spin 0.8s linear infinite;
}
</style>
