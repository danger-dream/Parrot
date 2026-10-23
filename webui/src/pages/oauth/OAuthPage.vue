<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Ban, CircleAlert, Eraser, FileUp, Gauge, Plus, RefreshCw, Settings2, ShieldCheck, Snowflake, Trash2, TriangleAlert, Users } from '@lucide/vue'
import { api, ApiError } from '@/api/client'
import { onEvent } from '@/api/events'
import { openAccount } from '@/links'
import { confirmAction } from '@/stores/dialogs'
import { inspector, toast, ui } from '@/stores/ui'
import Page from '@/components/ui/Page.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import AccountCard from './AccountCard.vue'
import AccountRow from './AccountRow.vue'
import StatTile from '@/components/ui/StatTile.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import type { Schemas } from '@/api/client'
import AddAccountModal from './AddAccountModal.vue'
import ImportModal from './ImportModal.vue'
import InvalidCleanupModal from './InvalidCleanupModal.vue'
import SettingsModal from './SettingsModal.vue'
import { accountPath, errorToast, fetchAllPages, notifyChanged, oauthBus, runOperation } from './oauthApi'
import { accountState, PROVIDER_ORDER, providerMeta, STATE_FILTERS, STATE_KEYS, type AccountSummary, type StateKey } from './meta'
import { errorTitle } from '@/utils/errors'

const route = useRoute()

const accounts = ref<AccountSummary[]>([])
const listRevision = ref('')
const loading = ref(true)
const error = ref('')
const now = ref(Date.now())

const q = ref('')
const providerFilter = ref<string>('')
const stateFilter = ref<'' | StateKey>('')

const addOpen = ref(false)
const importOpen = ref(false)
const invalidOpen = ref(false)
const settingsOpen = ref(false)
const refreshingAll = ref(false)
const refreshNote = ref('')
const clearingAll = ref(false)

let loadSeq = 0
let statsRetry: number | undefined
/**
 * One request renders every card: each list item carries the server-built `card` (plan line, usage,
 * local period stats, window stats, projection, cooldown summary). No per-account detail calls.
 */
const outcomes = ref<Record<string, Schemas['RecentOutcomeData']>>({})
const view = ref<'table' | 'cards'>(localStorage.getItem('parrot.oauth.view') === 'cards' ? 'cards' : 'table')
watch(view, (v) => localStorage.setItem('parrot.oauth.view', v))
// The table needs desktop width; phones always get the cards.
const effView = computed(() => (ui.width < 900 ? 'cards' : view.value))

async function load(): Promise<void> {
  const seq = ++loadSeq
  if (!accounts.value.length) loading.value = true
  try {
    const [res, recent] = await Promise.all([
      fetchAllPages<AccountSummary>('/oauth/accounts'),
      api.get<Schemas['RecentOutcomeData'][]>('/stats/recent-outcomes', { dimension: 'channel', limit: 10 }).catch(() => null),
    ])
    if (seq !== loadSeq) return
    if (recent) outcomes.value = Object.fromEntries(recent.data.map((o) => [o.key, o]))
    accounts.value = res.items
    listRevision.value = res.revision
    error.value = ''
    // Cold start: stats are computed in the background (TG「统计初始化中」); ask again shortly.
    window.clearTimeout(statsRetry)
    if (res.items.some((a) => a.card?.localPeriod.statsLoading || Object.values(a.card?.windowStats || {}).some((w) => w.statsLoading))) {
      statsRetry = window.setTimeout(load, 2500)
    }
  } catch (err) {
    if (seq === loadSeq) error.value = errorTitle(err)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

watch(
  () => oauthBus.version,
  () => load(),
)

// Monthly usage changes with traffic: refresh the affected card a few seconds after a request ends.
const pending = new Set<string>()
let liveTimer: number | undefined
const offFinished = onEvent('request.finished', ({ log }) => {
  const ch = log.channelId
  if (!ch || !ch.startsWith('oauth:') || drag.active) return
  pending.add(ch.slice(6))
  window.clearTimeout(liveTimer)
  liveTimer = window.setTimeout(() => {
    pending.clear()
    if (!drag.active && !savingOrder.value) load()
  }, 4000)
})
let ticker: number | undefined
function onFocus(): void {
  if (Date.now() - now.value > 60_000) load()
  now.value = Date.now()
}
onMounted(() => {
  load()
  ticker = window.setInterval(() => (now.value = Date.now()), 30_000)
  window.addEventListener('focus', onFocus)
})
onBeforeUnmount(() => {
  endDrag()
  offFinished()
  window.clearTimeout(liveTimer)
  window.clearTimeout(statsRetry)
  window.clearInterval(ticker)
  window.removeEventListener('focus', onFocus)
})

// ---- deep links: ?status=available|quota|invalid|disabled and ?account=ID ----
let lastAccountQuery = ''
watch(
  () => [route.query.status, route.query.account] as const,
  async ([status, account]) => {
    if (route.path !== '/oauth') return
    const s = typeof status === 'string' ? status : ''
    stateFilter.value = (STATE_KEYS as string[]).includes(s) ? (s as StateKey) : ''
    const id = typeof account === 'string' ? account : ''
    if (id && id !== lastAccountQuery) {
      // router.afterEach closes the inspector after every navigation; open once it has run.
      await nextTick()
      const name = accounts.value.find((a) => a.accountId === id)?.displayName
      openAccount(id, name)
    }
    lastAccountQuery = id
  },
  { immediate: true },
)

// ---- filtering ----
const providers = computed(() => {
  const counts = new Map<string, number>()
  for (const a of accounts.value) counts.set(a.provider, (counts.get(a.provider) || 0) + 1)
  return PROVIDER_ORDER.filter((p) => counts.has(p)).map((p) => ({ value: p, count: counts.get(p) || 0 }))
})

const byProvider = computed(() => accounts.value.filter((a) => !providerFilter.value || a.provider === providerFilter.value))
const stateCounts = computed(() => {
  const c: Record<string, number> = { '': 0, available: 0, quota: 0, invalid: 0, disabled: 0 }
  for (const a of byProvider.value) {
    c['']++
    c[accountState(a).key]++
  }
  return c
})
const stateOptions = computed(() =>
  STATE_FILTERS.map((f) => ({ value: f.value, label: stateCounts.value[f.value] ? `${f.label} ${stateCounts.value[f.value]}` : f.label })),
)
const visible = computed(() => {
  const s = q.value.trim().toLowerCase()
  return byProvider.value.filter((a) => {
    if (stateFilter.value && accountState(a).key !== stateFilter.value) return false
    if (!s) return true
    return [a.displayName, a.identity, a.accountId, providerMeta(a.provider).label].some((v) => v.toLowerCase().includes(s))
  })
})
const filtered = computed(() => Boolean(q.value.trim() || providerFilter.value || stateFilter.value))
const invalidCount = computed(() => accounts.value.filter((a) => a.invalid).length)
const selectedId = computed(() => (inspector.open && inspector.key.startsWith('account:') ? inspector.key.slice(8) : ''))

function clearFilters(): void {
  q.value = ''
  providerFilter.value = ''
  stateFilter.value = ''
}

function open(a: AccountSummary): void {
  openAccount(a.accountId, a.displayName)
}

// ---- TG list header: 共 N 个账户 | 正常 | 配额 | 用户禁用 | 认证失败 | 冷却 | 永久 ----
const summary = computed(() => {
  const s = { total: accounts.value.length, ok: 0, quota: 0, user: 0, auth: 0, restricted: 0, cooling: 0, permanent: 0, quotaPaused: 0 }
  for (const a of accounts.value) {
    if (a.disabledReason === 'auth_error' || a.invalid) s.auth++
    else if (a.disabledReason === 'quota') s.quota++
    else if (a.disabledReason === 'user' || !a.enabled) s.user++
    else if (a.disabledReason) s.restricted++
    else if (a.available) s.ok++
    s.cooling += a.card?.cooldown.coolingModels || 0
    s.permanent += a.card?.cooldown.permanentModels || 0
    s.quotaPaused += a.card?.cooldown.quotaPausedModels || 0
  }
  return s
})

// ---- drag to reorder (whole card; long-press on touch). Saves on drop, rolls back on failure ----
const drag = reactive({ id: '', active: false, x: 0, y: 0, pointerId: -1, touch: false, before: [] as string[] })
let pressTimer: number | undefined
let suppressClick = false
const savingOrder = ref(false)

function onCardPointerDown(e: PointerEvent, id: string): void {
  if (e.button !== 0 || savingOrder.value) return
  if ((e.target as HTMLElement).closest('button, a, input, select, textarea')) return
  Object.assign(drag, { id, active: false, x: e.clientX, y: e.clientY, pointerId: e.pointerId, touch: e.pointerType !== 'mouse' })
  if (drag.touch) pressTimer = window.setTimeout(startDrag, 380)
  window.addEventListener('pointermove', onPointerMove, { passive: false })
  window.addEventListener('pointerup', onPointerUp)
  window.addEventListener('pointercancel', onPointerCancel)
}
function startDrag(): void {
  if (!drag.id) return
  drag.active = true
  drag.before = accounts.value.map((a) => a.accountId)
  window.addEventListener('touchmove', blockTouchScroll, { passive: false })
  navigator.vibrate?.(12)
}
function blockTouchScroll(e: TouchEvent): void {
  if (drag.active) e.preventDefault()
}
function onPointerMove(e: PointerEvent): void {
  if (!drag.id || e.pointerId !== drag.pointerId) return
  const dist = Math.hypot(e.clientX - drag.x, e.clientY - drag.y)
  if (!drag.active) {
    // Touch: moving before the long-press fires means the user is scrolling.
    if (drag.touch) {
      if (dist > 8) endDrag()
      return
    }
    if (dist < 6) return
    startDrag()
  }
  e.preventDefault()
  const over = (document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null)?.closest<HTMLElement>('[data-account-id]')
  const overId = over?.dataset.accountId
  if (overId && overId !== drag.id) moveBefore(drag.id, overId)
  autoScroll(e.clientY)
}
function moveBefore(id: string, overId: string): void {
  const list = [...accounts.value]
  const from = list.findIndex((a) => a.accountId === id)
  const to = list.findIndex((a) => a.accountId === overId)
  if (from < 0 || to < 0) return
  const [item] = list.splice(from, 1)
  list.splice(to, 0, item)
  accounts.value = list
}
function autoScroll(y: number): void {
  const scroller = (document.querySelector('.content') as HTMLElement | null) || document.scrollingElement
  if (!scroller) return
  const edge = 70
  if (y < edge) scroller.scrollTop -= 14
  else if (y > window.innerHeight - edge) scroller.scrollTop += 14
}
function onPointerUp(): void {
  const wasActive = drag.active
  const before = drag.before
  endDrag()
  if (!wasActive) return
  suppressClick = true
  window.setTimeout(() => (suppressClick = false), 0)
  saveOrder(before)
}
function onPointerCancel(): void {
  if (drag.active) accounts.value = orderBy(drag.before)
  endDrag()
}
function endDrag(): void {
  window.clearTimeout(pressTimer)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('pointercancel', onPointerCancel)
  window.removeEventListener('touchmove', blockTouchScroll)
  Object.assign(drag, { id: '', active: false, pointerId: -1 })
}
function orderBy(ids: string[]): AccountSummary[] {
  const map = new Map(accounts.value.map((a) => [a.accountId, a]))
  return ids.map((id) => map.get(id)).filter((a): a is AccountSummary => Boolean(a))
}
/** Touch / keyboard: move one step relative to the neighbour that is visible under the current filter. */
function moveStep(id: string, delta: number): void {
  const list = visible.value
  const i = list.findIndex((a) => a.accountId === id)
  const neighbour = list[i + delta]
  if (!neighbour) return
  const before = accounts.value.map((a) => a.accountId)
  const full = accounts.value.map((a) => a.accountId)
  const from = full.indexOf(id)
  full.splice(from, 1)
  const at = full.indexOf(neighbour.accountId) + (delta > 0 ? 1 : 0)
  full.splice(at, 0, id)
  accounts.value = orderBy(full)
  saveOrder(before)
}
async function saveOrder(before: string[]): Promise<void> {
  const ids = accounts.value.map((a) => a.accountId)
  if (ids.join('\n') === before.join('\n')) return
  savingOrder.value = true
  try {
    const res = await api.put<{ revision: string }>('/oauth/account-order', { accountIds: ids }, { ifMatch: listRevision.value })
    listRevision.value = res.data.revision
    toast('已保存调度顺序', 'success')
  } catch (err) {
    accounts.value = orderBy(before)
    if (err instanceof ApiError && (err.code === 'REVISION_CONFLICT' || err.code === 'RESOURCE_CONFLICT')) {
      toast('顺序没有保存', 'warning', '账号顺序或账号列表刚在别处被改过，已恢复并载入最新顺序，请再拖一次。', 7000)
      load()
    } else {
      toast('顺序没有保存，已恢复原顺序', 'error', errorTitle(err), 7000)
    }
  } finally {
    savingOrder.value = false
  }
}
function onCardClick(a: AccountSummary): void {
  if (suppressClick) return
  open(a)
}
function onCardKey(e: KeyboardEvent, a: AccountSummary): void {
  if (e.key === 'Enter') open(a)
  else if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowLeft')) (e.preventDefault(), moveStep(a.accountId, -1))
  else if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowRight')) (e.preventDefault(), moveStep(a.accountId, 1))
}
const fullIndex = computed(() => new Map(visible.value.map((a, i) => [a.accountId, i])))

// ---- bulk actions ----
async function refreshAll(): Promise<void> {
  refreshingAll.value = true
  const started = Date.now()
  refreshNote.value = '正在刷新全部账号的用量…'
  const done = await runOperation(() => api.post('/oauth/actions/refresh-usage'), {
    failure: '刷新全部用量失败',
    onProgress: (op) => {
      const sec = Math.round((Date.now() - started) / 1000)
      refreshNote.value = op.progress
        ? `正在刷新用量… ${op.progress.current}/${op.progress.total}`
        : `${op.status === 'queued' ? '排队中' : '正在刷新全部账号的用量'}… 已用 ${sec} 秒`
    },
  })
  refreshingAll.value = false
  refreshNote.value = ''
  if (done?.status === 'succeeded') {
    const rows = ((done.result as { accounts?: { status: string }[] } | null)?.accounts || []) as { status: string }[]
    const ok = rows.filter((r) => r.status !== 'failed').length
    const bad = rows.length - ok
    toast('用量刷新完成', bad ? 'warning' : 'success', `成功 ${ok} 个，失败 ${bad} 个${bad ? '（失败的账号保留上次数据）' : ''}`, 7000)
  }
  load()
}

async function clearAllErrors(): Promise<void> {
  const ok = await confirmAction({
    title: '清除全部账号的错误？',
    detail: '会清除所有 OAuth 账号的模型故障记录和冷却状态，让它们立即重新参与调度。\n不会解除手动停用、认证失效或额度暂停。',
    confirmText: '全部清除',
    danger: true,
  })
  if (!ok) return
  clearingAll.value = true
  try {
    const res = await api.post<{ cleared: number }>('/oauth/actions/clear-errors')
    toast(res.data.cleared ? `已清除 ${res.data.cleared} 条错误/冷却记录` : '没有需要清除的错误', 'success')
    load()
  } catch (err) {
    errorToast(err)
  } finally {
    clearingAll.value = false
  }
}

// ---- per-row actions ----
const rowBusy = reactive<Record<string, string>>({})
async function toggleAccount(a: AccountSummary, on: boolean): Promise<void> {
  rowBusy[a.accountId] = 'toggle'
  try {
    await api.patch(accountPath(a.accountId), { enabled: on }, { ifMatch: a.revision })
    toast(on ? '账号已启用' : '账号已停用', 'success')
    notifyChanged(a.accountId)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('账号已在别处被修改', 'warning', '已载入最新数据，请再试一次。')
      load()
    } else errorToast(err)
  } finally {
    delete rowBusy[a.accountId]
  }
}
async function refreshAccount(a: AccountSummary): Promise<void> {
  rowBusy[a.accountId] = 'refresh'
  const op = await runOperation(() => api.post(accountPath(a.accountId, '/actions/refresh-usage')), { failure: '刷新用量失败' })
  delete rowBusy[a.accountId]
  if (op?.status === 'succeeded') toast(`${a.displayName} 用量已刷新`, 'success')
  load()
}
const priorityOf = computed(() => new Map(accounts.value.map((a, i) => [a.accountId, i + 1])))

const kpis = computed(() => {
  const s = summary.value
  return {
    total: s.total,
    ok: s.ok,
    attention: s.auth + s.restricted,
    quota: s.quota + accounts.value.filter((a) => a.quotaLimited && !a.disabledReason).length,
    disabled: s.user,
    models: s.cooling + s.quotaPaused + s.permanent,
  }
})

function onCreated(id: string | null): void {
  load()
  if (id) openAccount(id)
}
</script>

<template>
  <Page title="OAuth 账户" subtitle="订阅类账号池：查看额度、管理模型、登录新账号。排在前面的账号优先被调度。" width="wide">
    <template #actions>
      <Btn variant="primary" @click="addOpen = true"><Plus /> 新增账号</Btn>
      <Btn title="导入账号文件" @click="importOpen = true"><FileUp /><span class="lbl">导入</span></Btn>
      <Btn :loading="refreshingAll" title="刷新全部用量" @click="refreshAll"><RefreshCw /><span class="lbl">刷新全部用量</span></Btn>
      <Btn :loading="clearingAll" title="清除全部错误" @click="clearAllErrors"><Eraser /><span class="lbl">清除全部错误</span></Btn>
      <Btn title="失效账号清理" @click="invalidOpen = true">
        <Trash2 /><span class="lbl">失效清理</span><span v-if="invalidCount" class="count">{{ invalidCount }}</span>
      </Btn>
      <Btn title="设置" @click="settingsOpen = true"><Settings2 /><span class="lbl">设置</span></Btn>
    </template>

    <template #toolbar>
      <div class="ptabs">
        <button :class="{ on: !providerFilter }" @click="providerFilter = ''">全部 <i>{{ accounts.length }}</i></button>
        <button v-for="p in providers" :key="p.value" :class="{ on: providerFilter === p.value }" @click="providerFilter = p.value">
          <ProviderIcon :provider="p.value" :size="16" />{{ providerMeta(p.value).label }} <i>{{ p.count }}</i>
        </button>
      </div>
      <div class="frow">
        <SearchField v-model="q" placeholder="搜索名称、邮箱、账号 ID" class="search" />
        <div class="seg-scroll"><Segmented v-model="stateFilter" :options="stateOptions" /></div>
        <span class="grow" />
        <Segmented
          v-if="ui.width >= 900"
          v-model="view"
          :options="[
            { value: 'table', label: '表格' },
            { value: 'cards', label: '卡片' },
          ]"
        />
      </div>
    </template>

    <p v-if="refreshNote" class="progress"><span class="spin" /> {{ refreshNote }}</p>

    <section v-if="accounts.length" class="kpis">
      <StatTile dense label="账号总数" :icon="Users" tone="blue" :value="kpis.total" sub="已登录的订阅账号" />
      <StatTile dense label="正常可用" :icon="ShieldCheck" tone="green" :value="kpis.ok" value-tone="green" sub="参与调度" @click="stateFilter = 'available'" />
      <StatTile dense label="需要处理" :icon="CircleAlert" tone="red" :value="kpis.attention" :value-tone="kpis.attention ? 'red' : ''" sub="认证失效、平台限制" @click="stateFilter = 'invalid'" />
      <StatTile dense label="额度风险" :icon="Gauge" tone="orange" :value="kpis.quota" :value-tone="kpis.quota ? 'orange' : ''" sub="额度暂停或受限" @click="stateFilter = 'quota'" />
      <StatTile dense label="已停用" :icon="Ban" tone="indigo" :value="kpis.disabled" sub="手动停用" @click="stateFilter = 'disabled'" />
      <StatTile dense label="冷却中的模型" :icon="Snowflake" tone="cyan" :value="kpis.models" :sub="summary.permanent ? `永久冻结 ${summary.permanent}` : '冷却 / 额度暂停'" />
    </section>
    <p v-if="accounts.length" class="hint">{{ savingOrder ? '正在保存顺序…' : '拖动行或卡片调整调度顺序，排在前面的优先使用' }}</p>

      <div v-if="error" class="err">
        <TriangleAlert /> 账号列表加载失败：{{ error }}
        <Btn size="sm" @click="load">重试</Btn>
      </div>
      <div v-else-if="loading" class="grid">
        <Skeleton v-for="i in 6" :key="i" h="168px" r="14px" />
      </div>
      <Empty v-else-if="!accounts.length" title="还没有 OAuth 账号" detail="登录 Claude、OpenAI、智谱等订阅账号后，Parrot 会用它们转发请求。">
        <template #icon><ShieldCheck /></template>
        <Btn variant="primary" @click="addOpen = true"><Plus /> 新增账号</Btn>
      </Empty>
      <Empty v-else-if="!visible.length" title="没有符合条件的账号" detail="换个筛选条件试试">
        <template #icon><ShieldCheck /></template>
        <Btn v-if="filtered" @click="clearFilters">清除筛选</Btn>
      </Empty>
      <div v-else-if="effView === 'table'" class="atable">
        <div class="ahead">
          <span />
          <span>账号</span>
          <span>套餐</span>
          <span>状态</span>
          <span>最近请求</span>
          <span>本期用量</span>
          <span>额度</span>
          <span class="r">操作</span>
        </div>
        <TransitionGroup tag="div" name="reorder" class="alist" :class="{ sorting: drag.active }">
          <AccountRow
            v-for="a in visible"
            :key="a.accountId"
            :data-account-id="a.accountId"
            :account="a"
            :priority="priorityOf.get(a.accountId) ?? 0"
            :outcomes="outcomes[`oauth:${a.accountId}`]?.outcomes"
            :last-at="outcomes[`oauth:${a.accountId}`]?.lastAt"
            :now="now"
            :selected="a.accountId === selectedId"
            :dragging="drag.active && drag.id === a.accountId"
            :busy="rowBusy[a.accountId]"
            @pointerdown="onCardPointerDown($event, a.accountId)"
            @open="onCardClick(a)"
            @keydown="onCardKey($event, a)"
            @toggle="toggleAccount(a, $event)"
            @refresh="refreshAccount(a)"
          />
        </TransitionGroup>
      </div>
      <TransitionGroup v-else tag="div" name="reorder" class="grid" :class="{ sorting: drag.active }">
        <AccountCard
          v-for="a in visible"
          :key="a.accountId"
          :data-account-id="a.accountId"
          :account="a"
          :selected="a.accountId === selectedId"
          :dragging="drag.active && drag.id === a.accountId"
          :index="fullIndex.get(a.accountId) ?? 0"
          :count="visible.length"
          :now="now"
          @pointerdown="onCardPointerDown($event, a.accountId)"
          @click="onCardClick(a)"
          @keydown="onCardKey($event, a)"
          @move="moveStep(a.accountId, $event)"
          @contextmenu="drag.touch && drag.id && $event.preventDefault()"
        />
      </TransitionGroup>

    <AddAccountModal :open="addOpen" @close="addOpen = false" @created="onCreated" @import="(addOpen = false), (importOpen = true)" />
    <ImportModal :open="importOpen" @close="importOpen = false" @done="load" />
    <InvalidCleanupModal :open="invalidOpen" @close="invalidOpen = false" @done="load" />
    <SettingsModal :open="settingsOpen" @close="settingsOpen = false" />
  </Page>
</template>

<style scoped>
.ptabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  width: 100%;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--hairline);
}
.ptabs button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: 9px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
  border: 1px solid transparent;
}
.ptabs button:hover {
  background: var(--surface-hover);
}
.ptabs button.on {
  color: var(--accent);
  font-weight: 600;
  background: var(--accent-soft);
  border-color: var(--accent-line);
}
.ptabs i {
  min-width: 20px;
  padding: 0 6px;
  border-radius: 999px;
  font-style: normal;
  font-size: 11.5px;
  line-height: 18px;
  text-align: center;
  color: var(--text-2);
  background: var(--fill-2);
}
.ptabs button.on i {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
.frow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  width: 100%;
}
.frow .search {
  width: 280px;
}
.grow {
  flex: 1;
}
.kpis {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 12px;
}
.kpis > :not(:first-child) {
  cursor: pointer;
}
.hint {
  margin-top: -6px;
  font-size: 12px;
  color: var(--text-3);
}
.atable {
  --cols: 18px minmax(160px, 1.1fr) minmax(140px, 0.9fr) 76px 112px 160px minmax(270px, 2.1fr) 84px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ahead {
  display: grid;
  grid-template-columns: var(--cols);
  column-gap: 12px;
  padding: 0 17px 0 9px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
}
.ahead .r {
  text-align: right;
}
.alist {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.alist.sorting {
  cursor: grabbing;
}
/* Breakpoints follow the width actually left for the table (the page is the size container). */
@container (max-width: 1129px) {
  .atable {
    --cols: 18px minmax(150px, 1.1fr) minmax(130px, 0.9fr) 76px 156px minmax(240px, 2fr) 84px;
  }
  .ahead > :nth-child(5),
  .atable :deep(.c-recent) {
    display: none;
  }
}
@container (max-width: 959px) {
  .atable {
    --cols: 18px minmax(140px, 1fr) 76px 150px minmax(200px, 1.8fr) 84px;
  }
  .ahead > :nth-child(3),
  .atable :deep(.c-plan) {
    display: none;
  }
  .atable :deep(.plan-inline) {
    display: block;
  }
  .atable :deep(.who-sub) {
    display: none;
  }
}
@media (max-width: 1279px) {
  .kpis {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 767px) {
  .kpis {
    display: flex;
    overflow-x: auto;
    margin: 0 -10px;
    padding: 0 10px;
  }
  .kpis > * {
    flex: 0 0 140px;
  }
  .frow .search {
    width: 100%;
  }
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  align-items: stretch;
  gap: 12px;
}
.grid.sorting {
  cursor: grabbing;
}
.grid.sorting :deep(.ac) {
  cursor: grabbing;
}
.reorder-move {
  transition: transform 0.22s var(--ease-out);
}
.summary {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 14px;
  font-size: 12px;
  color: var(--text-2);
}
.summary b {
  font-size: 13px;
  font-weight: 650;
}
.summary .hint {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--text-3);
}
.seg-scroll {
  max-width: 100%;
  overflow-x: auto;
  scrollbar-width: none;
}
.seg-scroll::-webkit-scrollbar {
  display: none;
}
.count {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  font-size: 10.5px;
  line-height: 16px;
  text-align: center;
  color: #fff;
  background: var(--red);
}
.progress {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-radius: var(--radius-md);
  font-size: 12.5px;
  color: var(--text-2);
  background: var(--accent-soft);
}
.spin {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  border: 2px solid var(--accent);
  border-right-color: transparent;
  animation: spin 0.8s linear infinite;
}
.err {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
}
@container (max-width: 700px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 767px) {
  .lbl {
    display: none;
  }
  .search,
  .prov {
    flex: 1 1 140px;
    width: auto !important;
  }
}
</style>
