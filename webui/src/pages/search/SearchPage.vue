<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowDown, ArrowUp, ChevronRight, FlaskConical, GripVertical, Plus, SearchX, Waypoints } from '@lucide/vue'
import { api } from '@/api/client'
import { useResource, useSection } from '@/composables/useResource'
import { inspector, openInspector } from '@/stores/ui'
import { fieldErrors } from '@/utils/errors'
import Page from '@/components/ui/Page.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import TextArea from '@/components/ui/TextArea.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { errorTitle } from '@/utils/errors'
import BackendDetail from './BackendDetail.vue'
import SearchActivity from './SearchActivity.vue'
import {
  BACKEND_TYPES,
  credentialText,
  KEY_TYPES,
  kindLabel,
  MODES,
  stateOf,
  TYPE_NAME,
  type Backend,
  type BackendType,
  type Settings,
} from './labels'
import { mutateSearch, searchRes } from './store'

const route = useRoute()
const res = useResource<Settings>('/search', { successText: '搜索设置已保存' })
searchRes.value = res
onBeforeUnmount(() => {
  if (searchRes.value === res) searchRes.value = null
})

const modes = useSection(res, ['functionMode', 'hostedMode'])
const params = useSection(res, [
  'maxAttempts',
  'timeoutSeconds',
  'maxToolRounds',
  'maxResults',
  'maxFetchChars',
  'maxConcurrentToolCalls',
  'requireKnownUrlForFetch',
  'maxFetchUrlChars',
  'minQueryChars',
  'language',
  'country',
  'freshness',
])

const loadError = computed(() => (res.error.value ? errorTitle(res.error.value) : ''))
const d = computed(() => res.draft.value)

const modeOptions = MODES.map((m) => ({ value: m.value, label: m.label }))
function modeHint(v: string | undefined): string {
  return MODES.find((m) => m.value === v)?.hint || ''
}

// ---- backends & priority ----
const order = ref<Backend[]>([])
watch(
  () => res.data.value?.backends,
  (b) => (order.value = [...(b || [])]),
  { immediate: true },
)
const counts = computed(() => {
  const b = res.data.value?.backends || []
  return { total: b.length, ready: b.filter((x) => x.enabled && x.available).length, off: b.filter((x) => !x.enabled).length }
})

function openBackend(b: Backend): void {
  openInspector(`search:${b.id}`, b.name, BackendDetail, { id: b.id }, `${TYPE_NAME[b.type] || b.type} · 搜索来源`)
}
const selected = computed(() => (inspector.open && inspector.key.startsWith('search:') ? inspector.key.slice(7) : ''))

const savingOrder = ref(false)
async function commitOrder(ids: string[]): Promise<void> {
  const cur = (res.data.value?.backends || []).map((b) => b.id)
  if (JSON.stringify(ids) === JSON.stringify(cur)) return
  savingOrder.value = true
  const ok = await mutateSearch((rev) => api.put<Settings>('/search/priority', { backendIds: ids }, { ifMatch: rev }), '已调整搜索顺序')
  savingOrder.value = false
  if (!ok) order.value = [...(res.data.value?.backends || [])]
}

function move(i: number, d: number): void {
  const j = i + d
  if (j < 0 || j >= order.value.length) return
  const next = [...order.value]
  const [x] = next.splice(i, 1)
  next.splice(j, 0, x)
  order.value = next
  commitOrder(next.map((b) => b.id))
}

const dragId = ref('')
function onDragStart(e: DragEvent, b: Backend): void {
  dragId.value = b.id
  e.dataTransfer?.setData('text/plain', b.id)
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
}
function onDragOver(e: DragEvent, target: Backend): void {
  if (!dragId.value || dragId.value === target.id) return
  e.preventDefault()
  const from = order.value.findIndex((b) => b.id === dragId.value)
  const to = order.value.findIndex((b) => b.id === target.id)
  if (from < 0 || to < 0) return
  const next = [...order.value]
  const [x] = next.splice(from, 1)
  next.splice(to, 0, x)
  order.value = next
}
function onDragEnd(): void {
  if (!dragId.value) return
  dragId.value = ''
  commitOrder(order.value.map((b) => b.id))
}

async function toggle(b: Backend, v: boolean): Promise<void> {
  const i = order.value.findIndex((x) => x.id === b.id)
  if (i >= 0) order.value[i] = { ...b, enabled: v }
  const ok = await mutateSearch(
    (rev) => api.patch<Settings>(`/search/backends/${encodeURIComponent(b.id)}`, { enabled: v }, { ifMatch: rev }),
    v ? `已启用 ${b.name}` : `已停用 ${b.name}`,
  )
  if (!ok) order.value = [...(res.data.value?.backends || [])]
}

// ---- create ----
const typeOptions = BACKEND_TYPES.map((bt) => ({ value: bt, label: `${TYPE_NAME[bt]} · ${kindLabel(bt)}` }))
const createOpen = ref(false)
const creating = ref(false)
const createErr = ref<Record<string, string>>({})
const form = ref({ type: 'tavily' as BackendType, name: '', keys: '', endpoint: '' })
function startCreate(): void {
  form.value = { type: 'tavily', name: '', keys: '', endpoint: '' }
  createErr.value = {}
  createOpen.value = true
}
async function create(): Promise<void> {
  const body: Record<string, unknown> = { type: form.value.type }
  if (form.value.name.trim()) body.name = form.value.name.trim()
  const keys = form.value.keys.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
  if (KEY_TYPES.has(form.value.type) && keys.length) body.apiKeys = keys
  if (KEY_TYPES.has(form.value.type) && form.value.endpoint.trim()) body.endpoint = form.value.endpoint.trim()
  creating.value = true
  const before = new Set((res.data.value?.backends || []).map((b) => b.id))
  let err: unknown = null
  const ok = await mutateSearch(async (rev) => {
    try {
      return await api.post<Settings>('/search/backends', body, { ifMatch: rev })
    } catch (e) {
      err = e
      throw e
    }
  }, '已新增搜索来源')
  creating.value = false
  if (!ok) {
    createErr.value = fieldErrors(err)
    return
  }
  createOpen.value = false
  const added = (res.data.value?.backends || []).find((b) => !before.has(b.id))
  if (added) openBackend(added)
}

onMounted(() => {
  const id = typeof route.query.source === 'string' ? route.query.source : ''
  if (id) {
    const stop = watch(
      () => res.data.value,
      (v) => {
        const b = v?.backends.find((x) => x.id === id)
        if (b) {
          openBackend(b)
          stop()
        }
      },
      { immediate: true },
    )
  }
})
</script>

<template>
  <Page title="搜索" subtitle="客户端请求联网搜索（WebSearch / WebFetch）时，由 Parrot 按顺序调用这些搜索来源。">
    <template #actions>
      <Btn variant="primary" @click="startCreate"><Plus /> 新增来源</Btn>
    </template>

    <p v-if="loadError" class="err">搜索设置加载失败：{{ loadError }}</p>

    <Card title="搜索来源" :subtitle="`共 ${counts.total} 个 · 可用 ${counts.ready} · 停用 ${counts.off} · 拖动或用箭头调整顺序，失败时按顺序切换下一个`" flush>
      <template #icon><Waypoints /></template>
      <template #actions>
        <Badge v-if="savingOrder" tone="blue">正在保存顺序…</Badge>
      </template>
      <div v-if="res.loading.value && !res.data.value" class="pad"><Skeleton v-for="i in 5" :key="i" h="36px" r="8px" /></div>
      <Empty v-else-if="!order.length" title="还没有搜索来源" detail="点「新增来源」添加 Tavily、Exa 等 API Key，或使用已有的 OAuth 账户。">
        <template #icon><SearchX /></template>
        <Btn variant="primary" @click="startCreate"><Plus /> 新增来源</Btn>
      </Empty>
      <ol v-else class="blist">
        <li
          v-for="(b, i) in order"
          :key="b.id"
          class="brow"
          :class="{ dragging: dragId === b.id, sel: selected === b.id, off: !b.enabled }"
          draggable="true"
          @dragstart="onDragStart($event, b)"
          @dragover="onDragOver($event, b)"
          @drop.prevent="onDragEnd"
          @dragend="onDragEnd"
          @click="openBackend(b)"
        >
          <span class="grip" title="拖动调整顺序"><GripVertical /></span>
          <span class="pos mono">{{ i + 1 }}</span>
          <span class="main">
            <span class="nm">
              <span class="truncate">{{ b.name }}</span>
              <Badge :tone="stateOf(b).tone" dot>{{ stateOf(b).label }}</Badge>
              <Badge v-if="!b.verified" tone="orange">未验证</Badge>
            </span>
            <span class="meta">{{ TYPE_NAME[b.type] || b.type }} · {{ kindLabel(b.type) }} · {{ credentialText(b) }}</span>
          </span>
          <span class="ops" @click.stop>
            <Btn size="sm" variant="ghost" icon title="上移" :disabled="i === 0 || savingOrder" @click="move(i, -1)"><ArrowUp /></Btn>
            <Btn size="sm" variant="ghost" icon title="下移" :disabled="i === order.length - 1 || savingOrder" @click="move(i, 1)"><ArrowDown /></Btn>
            <Btn size="sm" variant="ghost" class="test-btn" title="测试这个来源" @click="openBackend(b)"><FlaskConical /> 测试</Btn>
            <Switch :model-value="b.enabled" :label="b.enabled ? '停用' : '启用'" @update:model-value="(v: boolean) => toggle(b, v)" />
          </span>
          <ChevronRight class="chev" />
        </li>
      </ol>
    </Card>

    <div class="stack">
      <SettingsSection
        title="调用方式"
        description="客户端在请求里声明搜索工具时，交给谁来执行。"
        :loading="res.loading.value && !d"
        :dirty="modes.dirty.value"
        :saving="modes.saving.value"
        @save="modes.save()"
        @reset="modes.reset()"
      >
        <template v-if="d">
          <div class="modes">
            <Field label="普通 function 工具" :hint="modeHint(d.functionMode)">
              <Segmented v-model="d.functionMode" :options="modeOptions" />
            </Field>
            <Field label="原生 hosted 搜索" :hint="modeHint(d.hostedMode)">
              <Segmented v-model="d.hostedMode" :options="modeOptions" />
            </Field>
          </div>
          <p class="note">「普通 function」指客户端自己定义的 web_search / web_fetch 函数；「原生 hosted」指上游原生搜索工具（如 Anthropic web_search、OpenAI web_search_preview）。</p>
        </template>
      </SettingsSection>

      <SettingsSection
        title="默认参数"
        description="对所有搜索来源生效。"
        :loading="res.loading.value && !d"
        :dirty="params.dirty.value"
        :saving="params.saving.value"
        @save="params.save()"
        @reset="params.reset()"
      >
        <template v-if="d">
          <div class="pgrid">
            <div class="pcol">
              <p class="grp">执行</p>
              <Field inline label="总尝试次数" hint="含首次，失败后按顺序换下一个来源（1–10）">
                <NumberInput v-model="d.maxAttempts" :min="1" :max="10" integer unit="次" />
              </Field>
              <Field inline label="单次超时" hint="一次完整来源调用的上限；OAuth 原生搜索通常更慢（0.1–600）">
                <NumberInput v-model="d.timeoutSeconds" :min="0.1" :max="600" unit="秒" />
              </Field>
              <Field inline label="工具轮数上限" hint="一次对话内最多执行多少轮搜索（1–1000）">
                <NumberInput v-model="d.maxToolRounds" :min="1" :max="1000" integer unit="轮" />
              </Field>
              <p class="grp">返回内容</p>
              <Field inline label="结果数" hint="每次搜索返回的条数（1–20）">
                <NumberInput v-model="d.maxResults" :min="1" :max="20" integer unit="条" />
              </Field>
              <Field inline label="提取字符上限" hint="网页提取最多返回的字符数">
                <NumberInput v-model="d.maxFetchChars" :min="1" :max="10000000" integer unit="字符" />
              </Field>
              <Field inline label="工具并发" hint="同时执行的搜索调用数，0 表示不限">
                <NumberInput v-model="d.maxConcurrentToolCalls" :min="0" :max="1000" integer unit="个" />
              </Field>
            </div>
            <div class="pcol">
              <p class="grp">提取</p>
              <Field inline label="仅提取已知网址" hint="只允许提取先前搜索结果中出现过的网址">
                <Switch v-model="d.requireKnownUrlForFetch" />
              </Field>
              <Field inline label="网址长度上限" hint="超过此长度的网址会被拒绝">
                <NumberInput v-model="d.maxFetchUrlChars" :min="1" :max="100000" integer unit="字符" />
              </Field>
              <Field inline label="最短搜索词" hint="短于此长度的搜索词会被拒绝">
                <NumberInput v-model="d.minQueryChars" :min="1" :max="1000" integer unit="字符" />
              </Field>
              <p class="grp">区域偏好</p>
              <Field inline label="默认语言" hint="例如 zh-CN、en；留空表示不指定">
                <TextInput v-model="d.language" width="140px" placeholder="不指定" mono />
              </Field>
              <Field inline label="默认国家" hint="例如 CN、US；留空表示不指定">
                <TextInput v-model="d.country" width="140px" placeholder="不指定" mono />
              </Field>
              <Field inline label="默认时间范围">
                <Select
                  v-model="d.freshness"
                  width="140px"
                  :options="[
                    { value: '', label: '不限' },
                    { value: 'day', label: '最近一天' },
                    { value: 'week', label: '最近一周' },
                    { value: 'month', label: '最近一月' },
                    { value: 'year', label: '最近一年' },
                  ]"
                />
              </Field>
            </div>
          </div>
        </template>
      </SettingsSection>
    </div>

    <SearchActivity />

    <Modal :open="createOpen" title="新增搜索来源" subtitle="来源身份创建后固定，调整顺序不会改变身份。" @close="createOpen = false">
      <div class="cform">
        <Field label="类型" required :error="createErr.type">
          <Select v-model="form.type" :options="typeOptions" />
        </Field>
        <Field label="名称" :hint="`留空使用「${TYPE_NAME[form.type]}」`" :error="createErr.name">
          <TextInput v-model="form.name" :placeholder="TYPE_NAME[form.type]" />
        </Field>
        <template v-if="KEY_TYPES.has(form.type)">
          <Field label="API Key" hint="每行一个，可以稍后在来源详情里追加；保存后不再显示。" :error="createErr.apiKeys">
            <TextArea v-model="form.keys" :rows="3" mono placeholder="每行一个 Key" />
          </Field>
          <Field v-if="form.type !== 'zhipu'" label="接口地址" hint="留空使用默认地址" :error="createErr.endpoint">
            <TextInput v-model="form.endpoint" mono placeholder="默认" />
          </Field>
        </template>
        <p v-else class="note">OAuth 来源使用已登录的 {{ TYPE_NAME[form.type] }} 账户，不需要填写 Key。创建后可以指定使用哪些账户。</p>
      </div>
      <template #footer>
        <Btn variant="ghost" @click="createOpen = false">取消</Btn>
        <Btn variant="primary" :loading="creating" @click="create">创建</Btn>
      </template>
    </Modal>
  </Page>
</template>

<style scoped>
.err {
  padding: 10px 14px;
  border-radius: var(--radius-md);
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
  font-size: 12.5px;
}
.pad {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
}
.blist {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 0.5px solid var(--hairline);
}
.brow {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 52px;
  padding: 6px 12px 6px 6px;
  border-bottom: 0.5px solid var(--hairline);
  cursor: pointer;
  transition: background 0.15s;
}
.brow:last-child {
  border-bottom: 0;
}
.brow:hover {
  background: var(--fill-3);
}
.brow.sel {
  background: var(--accent-soft);
}
.brow.dragging {
  opacity: 0.5;
  background: var(--fill-2);
}
.brow.off .nm > .truncate {
  color: var(--text-2);
}
.grip {
  display: grid;
  place-items: center;
  width: 20px;
  height: 32px;
  color: var(--text-3);
  cursor: grab;
}
.grip svg {
  width: 14px;
  height: 14px;
}
.pos {
  width: 18px;
  font-size: 11.5px;
  color: var(--text-3);
  text-align: right;
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nm {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-weight: 550;
}
.meta {
  font-size: 11.5px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ops {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.ops :deep(.switch) {
  margin-left: 8px;
}
.chev {
  width: 14px;
  height: 14px;
  color: var(--text-3);
  flex-shrink: 0;
}
.stack {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.modes,
.pgrid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px 40px;
  align-items: start;
}
.pcol {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
@container (max-width: 980px) {
  .modes,
  .pgrid {
    grid-template-columns: minmax(0, 1fr);
  }
}
.gap {
  margin-top: 14px;
}
.grp {
  margin-top: 6px;
  font-size: 11px;
  font-weight: 650;
  color: var(--text-3);
}
.grp:first-child {
  margin-top: 0;
}
.note {
  margin-top: 12px;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-3);
}
.cform {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
@media (max-width: 767px) {
  .brow {
    gap: 6px;
    padding: 8px 10px;
  }
  .grip,
  .chev,
  .ops .test-btn {
    display: none;
  }
  .ops :deep(.switch) {
    margin-left: 4px;
  }
}
</style>
