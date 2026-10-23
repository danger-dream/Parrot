<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ChartBar, ChevronLeft, ChevronRight, Coins, Film, Image as ImageIcon, ImagePlay, RefreshCw, Settings2, Users, X } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { inspector, openInspector } from '@/stores/ui'
import { dateTime, integer, ms, relative, usd } from '@/utils/format'
import { errorTitle } from '@/utils/errors'
import { goPage, openAccount, openModel } from '@/links'
import Page from '@/components/ui/Page.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import MediaLogDetail from './MediaLogDetail.vue'
import { actionLabel, mediaLabel, providerLabel, statusLabel, statusTone } from './labels'

type Log = Schemas['MediaLogData']
type Settings = Schemas['ImageSettingsData']

const route = useRoute()
const PAGE = 30

const f = reactive({
  action: '' as '' | 'generate' | 'edit' | 'extend',
  status: '' as string,
  provider: '' as string,
  model: '' as string,
  when: 'all' as 'all' | 'today' | '7d',
  sort: 'createdAt' as 'createdAt' | 'cost' | 'duration' | 'status',
})
const page = ref(1)
const items = ref<Log[]>([])
const total = ref(0)
const hasNext = ref(false)
const loading = ref(false)
const error = ref('')
const now = ref(Date.now())

// getMediaLogSummary (round 2) is not in the generated schema yet.
interface Summary {
  total: number
  openaiImages: number
  xaiImages: number
  videos: number
  successCount: number
  failedCount: number
  pendingCount: number
  expiredCount: number
  costUsdTicks: number
  topAccounts: { accountKey: string; accountEmail?: string | null; provider?: string | null; total: number; costUsdTicks: number }[]
}
const summary = ref<Summary | null>(null)
const summaryError = ref('')

const modelOptions = ref<string[]>([])

function since(): string | undefined {
  if (f.when === 'all') return undefined
  const d = new Date()
  if (f.when === '7d') return new Date(d.getTime() - 7 * 86400_000).toISOString()
  const bjt = new Date(d.getTime() + 8 * 3600_000)
  return new Date(Date.UTC(bjt.getUTCFullYear(), bjt.getUTCMonth(), bjt.getUTCDate()) - 8 * 3600_000).toISOString()
}

function query(extra: Record<string, unknown> = {}) {
  const statuses = f.status === 'active' ? ['running', 'pending'] : f.status ? [f.status] : undefined
  return {
    action: f.action || undefined,
    status: statuses,
    provider: f.provider || undefined,
    model: f.model || undefined,
    startedAt: since(),
    sort: f.sort,
    descending: true,
    page: page.value,
    pageSize: PAGE,
    ...extra,
  } as Record<string, string | number | boolean | string[] | undefined>
}

let seq = 0
async function load(): Promise<void> {
  const my = ++seq
  loading.value = true
  try {
    const res = await api.get<Log[]>('/media-logs', query())
    if (my !== seq) return
    items.value = res.data
    total.value = res.meta.total ?? res.data.length
    hasNext.value = Boolean(res.meta.hasNext)
    error.value = ''
  } catch (err) {
    if (my === seq) error.value = errorTitle(err)
  } finally {
    if (my === seq) loading.value = false
  }
}

/** TG「📊 汇总」+「👤 OAuth Top 3」: one call to getMediaLogSummary. */
async function loadSummary(): Promise<void> {
  try {
    summary.value = (await api.get<Summary>('/media-logs/summary')).data
    summaryError.value = ''
  } catch (err) {
    summaryError.value = errorTitle(err)
  }
}

async function loadModels(): Promise<void> {
  try {
    const [img, vid] = await Promise.all([api.get<Settings>('/images/settings'), api.get<Settings>('/videos/settings')])
    const v = Object.values(vid.data.models).flat()
    modelOptions.value = [...new Set([...Object.values(img.data.models).flat(), ...v])].sort()
  } catch {
    modelOptions.value = []
  }
}

let filterTimer: number | undefined
watch(
  () => ({ ...f }),
  () => {
    window.clearTimeout(filterTimer)
    filterTimer = window.setTimeout(() => {
      page.value = 1
      load()
    }, 120)
  },
)

function go(d: number): void {
  page.value = Math.max(1, page.value + d)
  load()
}

function refresh(): void {
  load()
  loadSummary()
}

function open(log: Log): void {
  openInspector(`media:${log.id}`, `${mediaLabel(log.mediaType)}${actionLabel(log.action)} #${log.id}`, MediaLogDetail, { id: log.id }, log.model)
}

const selected = computed(() => (inspector.open && inspector.key.startsWith('media:') ? inspector.key.slice(6) : ''))
const hasFilter = computed(() => Boolean(f.action || f.status || f.provider || f.model || f.when !== 'all'))
function clear(): void {
  Object.assign(f, { action: '', status: '', provider: '', model: '', when: 'all' })
}

let timer: number | undefined
let tick: number | undefined
onMounted(async () => {
  if (typeof route.query.model === 'string') f.model = route.query.model
  await loadModels()
  load()
  loadSummary()
  if (typeof route.query.log === 'string') open({ id: route.query.log, mediaType: 'image', action: 'generate', model: '' } as Log)
  // Keep running jobs fresh while the page is visible.
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    if (items.value.some((x) => x.status === 'running' || x.status === 'pending')) {
      load()
      loadSummary()
    }
  }, 8000)
  tick = window.setInterval(() => (now.value = Date.now()), 1000)
})
onBeforeUnmount(() => {
  window.clearInterval(timer)
  window.clearInterval(tick)
})

function dims(l: Log): string {
  const parts: string[] = []
  if (l.mediaType !== 'video') {
    if (l.generatedCount) parts.push(`${l.generatedCount} 张`)
  } else if (l.durationSeconds) parts.push(`${l.durationSeconds} 秒`)
  if (l.aspectRatio) parts.push(l.aspectRatio)
  if (l.resolution) parts.push(l.resolution)
  return parts.join(' · ') || '—'
}

function elapsed(l: Log): string {
  if ((l.status === 'running' || l.status === 'pending') && l.createdAt) return `已等待 ${ms(now.value - new Date(l.createdAt).getTime())}`
  return ms(l.durationMilliseconds)
}

const pages = computed(() => Math.max(1, Math.ceil(total.value / PAGE)))
</script>

<template>
  <Page title="媒体日志" subtitle="图片与视频的生成、编辑、延长记录；点开一条可预览、播放和下载产物。">
    <template #actions>
      <Btn @click="goPage('/imaging')"><Settings2 /> 图片与视频设置</Btn>
      <Btn icon title="刷新" :loading="loading" @click="refresh"><RefreshCw /></Btn>
    </template>
    <template #toolbar>
      <Segmented
        v-model="f.action"
        :options="[
          { value: '', label: '全部' },
          { value: 'generate', label: '生成' },
          { value: 'edit', label: '编辑' },
          { value: 'extend', label: '延长' },
        ]"
      />
      <Select
        v-model="f.status"
        width="120px"
        :options="[
          { value: '', label: '全部状态' },
          { value: 'success', label: '成功' },
          { value: 'failed', label: '失败' },
          { value: 'active', label: '进行中' },
          { value: 'expired', label: '已过期' },
          { value: 'cancelled', label: '已取消' },
        ]"
      />
      <Select
        v-model="f.provider"
        width="120px"
        :options="[
          { value: '', label: '全部来源' },
          { value: 'openai', label: 'GPT' },
          { value: 'xai', label: 'Grok' },
          { value: 'antigravity', label: 'Gemini' },
        ]"
      />
      <Select v-model="f.model" width="190px" :options="[{ value: '', label: '全部模型' }, ...modelOptions.map((m) => ({ value: m, label: m }))]" />
      <Segmented
        v-model="f.when"
        size="sm"
        :options="[
          { value: 'all', label: '全部时间' },
          { value: 'today', label: '今天' },
          { value: '7d', label: '7 天' },
        ]"
      />
      <Btn v-if="hasFilter" variant="ghost" @click="clear"><X /> 清除</Btn>
      <span class="spacer" />
      <Select
        v-model="f.sort"
        width="128px"
        :options="[
          { value: 'createdAt', label: '按时间排序' },
          { value: 'cost', label: '按费用排序' },
          { value: 'duration', label: '按耗时排序' },
          { value: 'status', label: '按状态排序' },
        ]"
      />
    </template>

    <div class="sum">
      <Card title="汇总" :subtitle="summary ? `共 ${integer(summary.total)} 条记录` : undefined">
        <template #icon><ChartBar /></template>
        <p v-if="summaryError" class="faint small">{{ summaryError }}</p>
        <div v-else-if="!summary" class="skl"><Skeleton h="44px" r="10px" /><Skeleton h="30px" r="999px" /></div>
        <template v-else>
          <div class="kinds">
            <button type="button" class="kind" :class="{ on: f.provider === 'openai' }" title="只看 GPT" @click="f.provider = f.provider === 'openai' ? '' : 'openai'">
              <span class="k"><ImageIcon /> GPT 图片</span><span class="v">{{ integer(summary.openaiImages) }}</span>
            </button>
            <div class="kind">
              <span class="k"><ImageIcon /> Grok 图片</span><span class="v">{{ integer(summary.xaiImages) }}</span>
            </div>
            <div class="kind">
              <span class="k"><Film /> 视频</span><span class="v">{{ integer(summary.videos) }}</span>
            </div>
            <div class="kind">
              <span class="k"><Coins /> 已记录费用</span><span class="v">{{ summary.costUsdTicks ? usd(summary.costUsdTicks) : '—' }}</span>
            </div>
          </div>
          <div class="chips">
            <button type="button" class="chip" :class="{ on: f.status === 'success' }" @click="f.status = f.status === 'success' ? '' : 'success'">
              <i class="d green" /><span class="k">成功</span><span class="v num">{{ integer(summary.successCount) }}</span>
            </button>
            <button type="button" class="chip" :class="{ on: f.status === 'failed' }" @click="f.status = f.status === 'failed' ? '' : 'failed'">
              <i class="d red" /><span class="k">失败</span><span class="v num">{{ integer(summary.failedCount) }}</span>
            </button>
            <button type="button" class="chip" :class="{ on: f.status === 'active' }" @click="f.status = f.status === 'active' ? '' : 'active'">
              <i class="d blue" /><span class="k">进行中</span><span class="v num">{{ integer(summary.pendingCount) }}</span>
            </button>
            <button type="button" class="chip" :class="{ on: f.status === 'expired' }" @click="f.status = f.status === 'expired' ? '' : 'expired'">
              <i class="d gray" /><span class="k">已过期</span><span class="v num">{{ integer(summary.expiredCount) }}</span>
            </button>
          </div>
        </template>
      </Card>
      <Card title="OAuth 账号 Top 3" subtitle="按调用次数，只含当前仍存在的账号">
        <template #icon><Users /></template>
        <p v-if="summaryError" class="faint small">{{ summaryError }}</p>
        <div v-else-if="!summary" class="skl"><Skeleton h="16px" /><Skeleton h="16px" w="80%" /></div>
        <p v-else-if="!summary.topAccounts.length" class="faint small">还没有 OAuth 账号的媒体调用。</p>
        <ol v-else class="top">
          <li v-for="(a, i) in summary.topAccounts" :key="a.accountKey">
            <span class="rk mono">{{ i + 1 }}</span>
            <Badge :tone="a.provider === 'xai' ? 'purple' : 'teal'">{{ providerLabel(a.provider) }}</Badge>
            <button type="button" class="lk truncate" :title="a.accountKey" @click="openAccount(a.accountKey, a.accountEmail || undefined)">
              {{ a.accountEmail || a.accountKey }}
            </button>
            <span class="mono n">{{ integer(a.total) }} 次</span>
            <span class="mono c">{{ a.costUsdTicks ? usd(a.costUsdTicks) : '—' }}</span>
          </li>
        </ol>
      </Card>
    </div>

    <p v-if="error" class="err">加载失败：{{ error }}</p>
    <section class="table">
      <div class="head">
        <span class="c-time">时间</span><span class="c-status">状态</span><span class="c-type">类型</span><span class="c-model">模型</span>
        <span class="c-dims">规格</span><span class="c-dur">耗时</span><span class="c-cost">费用</span>
      </div>
      <template v-if="loading && !items.length">
        <div v-for="i in 8" :key="i" class="skl"><Skeleton h="14px" /></div>
      </template>
      <Empty
        v-else-if="!items.length"
        :title="hasFilter ? '没有符合条件的记录' : '还没有媒体记录'"
        :detail="hasFilter ? '换个筛选条件试试' : '客户端调用图片或视频接口后，记录会出现在这里'"
      >
        <template #icon><ImagePlay /></template>
      </Empty>
      <div v-else :class="{ dim: loading }">
        <div
          v-for="l in items"
          :key="l.id"
          class="row"
          :class="[`t-${statusTone(l.status)}`, { sel: selected === l.id }]"
          role="button"
          tabindex="0"
          @click="open(l)"
          @keydown.enter="open(l)"
        >
          <span class="c-time mono" :title="dateTime(l.createdAt)">{{ dateTime(l.createdAt) }}<span class="rel faint">{{ relative(l.createdAt, now) }}</span></span>
          <span class="c-status">
            <i class="dot" />{{ statusLabel(l.status) }}
            <span v-if="(l.status === 'running' || l.status === 'pending') && l.progress !== null && l.progress !== undefined" class="mono prog">{{ Math.round(l.progress) }}%</span>
          </span>
          <span class="c-type">
            <component :is="l.mediaType === 'video' ? Film : ImageIcon" class="ti" />
            {{ mediaLabel(l.mediaType) }}{{ actionLabel(l.action) }}
            <span class="prov faint">{{ providerLabel(l.provider) }}</span>
          </span>
          <span class="c-model">
            <button v-if="l.model" type="button" class="lk mono truncate" @click.stop="openModel(l.model)">{{ l.model }}</button>
            <span v-else class="faint">—</span>
          </span>
          <span class="c-dims mono">{{ dims(l) }}</span>
          <span class="c-dur mono">{{ elapsed(l) }}</span>
          <span class="c-cost mono">{{ l.costTicks ? usd(l.costTicks) : '—' }}</span>
          <p v-if="l.error" class="c-err truncate" :title="l.error">{{ l.error }}</p>
        </div>
      </div>
    </section>
    <div v-if="total" class="pager">
      <span class="faint">共 {{ integer(total) }} 条 · 第 {{ page }} / {{ pages }} 页</span>
      <Btn icon size="sm" :disabled="page <= 1" @click="go(-1)"><ChevronLeft /></Btn>
      <Btn icon size="sm" :disabled="!hasNext" @click="go(1)"><ChevronRight /></Btn>
    </div>
  </Page>
</template>

<style scoped>
.spacer {
  flex: 1;
}
.sum {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}
@container (max-width: 860px) {
  .sum {
    grid-template-columns: minmax(0, 1fr);
  }
}
.skl {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.small {
  font-size: 12px;
}
.kinds {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}
@container (max-width: 560px) {
  .kinds {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.kind {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border-radius: var(--radius-md);
  text-align: left;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
button.kind:hover {
  background: var(--fill-2);
}
.kind.on {
  box-shadow: inset 0 0 0 1px var(--accent);
  background: var(--accent-soft);
}
.kind .k {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}
.kind .k svg {
  width: 12px;
  height: 12px;
}
.kind .v {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.top {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.top li {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  font-size: 12.5px;
}
.rk {
  width: 14px;
  color: var(--text-3);
  font-size: 11.5px;
}
.top .lk {
  flex: 1;
  text-align: left;
}
.top .lk:hover {
  color: var(--accent);
}
.top .n {
  font-size: 12px;
}
.top .c {
  width: 64px;
  text-align: right;
  font-size: 12px;
  color: var(--text-2);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 12px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--surface);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
button.chip:hover {
  background: var(--surface-hover);
}
.chip.on {
  box-shadow: inset 0 0 0 1px var(--accent);
  background: var(--accent-soft);
}
.chip .k {
  color: var(--text-3);
}
.chip .v {
  font-weight: 600;
}
.d {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.d.green { background: var(--green); }
.d.red { background: var(--red); }
.d.blue { background: var(--blue); }
.d.gray { background: var(--text-3); }
.err {
  color: var(--red);
  font-size: 12.5px;
}
.table {
  container-type: inline-size;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--card-highlight), inset 0 0 0 0.5px var(--border);
  overflow: hidden;
  --cols: 150px 96px 150px minmax(150px, 1.6fr) minmax(120px, 1fr) 96px 76px;
  --areas: 'time status type model dims dur cost' 'err err err err err err err';
}
.head,
.row {
  display: grid;
  grid-template-columns: var(--cols);
  column-gap: 12px;
  align-items: center;
  padding: 0 14px;
}
.head {
  grid-template-areas: var(--areas);
  grid-template-rows: 32px 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 0.5px solid var(--border);
}
.row {
  grid-template-areas: var(--areas);
  min-height: 42px;
  padding-top: 6px;
  padding-bottom: 6px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
  cursor: pointer;
}
.row:last-child {
  border-bottom: 0;
}
.row:hover {
  background: var(--fill-3);
}
.row.sel {
  background: var(--accent-soft);
}
.t-green { --tone: var(--green); }
.t-red { --tone: var(--red); }
.t-orange { --tone: var(--orange); }
.t-blue { --tone: var(--blue); }
.t-neutral { --tone: var(--text-3); }
.c-time { grid-area: time; display: flex; flex-direction: column; font-size: 11.5px; }
.rel { font-family: var(--font-sans); font-size: 10.5px; }
.c-status { grid-area: status; display: inline-flex; align-items: center; gap: 6px; color: var(--tone); font-size: 12px; }
.c-type { grid-area: type; display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.c-model { grid-area: model; min-width: 0; display: flex; }
.c-dims { grid-area: dims; font-size: 11.5px; color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.c-dur { grid-area: dur; text-align: right; font-size: 11.5px; }
.c-cost { grid-area: cost; text-align: right; font-size: 11.5px; color: var(--text-2); }
.c-err { grid-area: err; margin: 2px 0 0; font-size: 11.5px; color: var(--red); }
.head .c-dur,
.head .c-cost {
  text-align: right;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--tone);
  flex-shrink: 0;
}
.prog {
  font-size: 11px;
}
.ti {
  width: 13px;
  height: 13px;
  color: var(--text-3);
  flex-shrink: 0;
}
.prov {
  font-size: 11px;
}
.lk {
  text-align: left;
}
.lk:hover {
  color: var(--accent);
}
.skl {
  padding: 13px 14px;
  border-bottom: 0.5px solid var(--hairline);
}
.dim {
  opacity: 0.55;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  font-size: 12px;
}
@container (max-width: 900px) {
  .table {
    --cols: 120px 90px 140px minmax(130px, 1fr) 90px;
    --areas: 'time status type model dur' 'err err err err err';
  }
  .c-dims,
  .c-cost {
    display: none;
  }
}
@container (max-width: 640px) {
  .head {
    display: none;
  }
  .table {
    --cols: minmax(0, 1fr) auto;
    --areas: 'type status' 'model dur' 'time time' 'err err';
  }
  .row {
    row-gap: 3px;
    padding: 10px 12px;
  }
  .c-time {
    flex-direction: row;
    gap: 8px;
    font-size: 11px;
    color: var(--text-3);
  }
  .c-status {
    justify-self: end;
  }
}
</style>
