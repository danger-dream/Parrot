<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CircleCheck, TriangleAlert } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import type { Operation } from '@/api/operations'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { bytes, fullTime, integer, newIdempotencyKey } from '@/utils/format'
import Modal from '@/components/ui/Modal.vue'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Pager from '@/components/ui/Pager.vue'
import Badge from '@/components/ui/Badge.vue'
import OpProgress from '@/components/ui/OpProgress.vue'
import { useOperation } from '@/composables/useOperation'

/** Per-database breakdown added in R2-12 (typed locally until api/schema.d.ts is regenerated). */
export interface PlanItem {
  month: string
  database: string
  action: 'delete_file' | 'trim_and_vacuum' | 'purge_bodies'
  totalRows: number
  deletedRows: number
  deletedCallRows: number
  currentBytes: number
  releasableBytes: number | null
  willCompact: boolean
  compactionRequiredBytes: number | null
  note: string | null
  active: boolean
}
type Plan = Schemas['RetentionPlanData'] & { items?: PlanItem[] }
export interface PlanRequest {
  scope: 'requests' | 'bodies'
  days: number
}
interface CommitResult {
  deletedRows?: number
  affectedFiles?: number
  logicalBytesRemoved?: number
  actualFreeBytes?: number
  days?: number
  policyActivated?: boolean
  scope?: string
  backgroundCleanupStarted?: boolean
  notCompacted?: { month: string; reason: string }[]
}

const props = defineProps<{ request: PlanRequest | null }>()
const emit = defineEmits<{ done: [committed: boolean] }>()

const plan = ref<Plan | null>(null)
const creating = ref(false)
const createError = ref('')
const cancelling = ref(false)
const commit = useOperation()
const result = computed<CommitResult | null>(() => (commit.op.value?.status === 'succeeded' ? (commit.op.value.result as CommitResult) : null))
const phase = computed<'scan' | 'review' | 'running' | 'done' | 'failed'>(() => {
  if (commit.op.value) {
    if (commit.running.value) return 'running'
    return commit.op.value.status === 'succeeded' ? 'done' : 'failed'
  }
  if (commit.running.value) return 'running'
  if (commit.error.value) return 'failed'
  return plan.value ? 'review' : 'scan'
})
const isBodies = computed(() => props.request?.scope === 'bodies')

// ── per-database breakdown ──
const ITEM_PAGE = 5
const itemPage = ref(1)
const items = computed(() => plan.value?.items || [])
const pagedItems = computed(() => items.value.slice((itemPage.value - 1) * ITEM_PAGE, itemPage.value * ITEM_PAGE))
watch(plan, () => (itemPage.value = 1))
const ACTION: Record<PlanItem['action'], string> = {
  delete_file: '整库删除',
  trim_and_vacuum: '删除过期记录',
  purge_bodies: '清理正文',
}
const what = computed(() => (isBodies.value ? '完整正文' : '请求日志'))

const now = ref(Date.now())
const timer = window.setInterval(() => (now.value = Date.now()), 1000)
onBeforeUnmount(() => window.clearInterval(timer))
const expiresIn = computed(() => {
  if (!plan.value) return ''
  const s = Math.max(0, Math.round((new Date(plan.value.expiresAt).getTime() - now.value) / 1000))
  return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : '已过期'
})

async function create(req: PlanRequest): Promise<void> {
  plan.value = null
  createError.value = ''
  commit.reset()
  creating.value = true
  try {
    plan.value = (await api.post<Plan>('/logs/retention/plans', { days: req.days, scope: req.scope })).data
  } catch (err) {
    createError.value = errorTitle(err)
    const d = errorDetail(err)
    if (d) createError.value += `：${d}`
  } finally {
    creating.value = false
  }
}
watch(
  () => props.request,
  (r) => {
    if (r) create(r)
  },
  { immediate: true },
)

async function cancelPlan(): Promise<void> {
  const p = plan.value
  if (p && p.state === 'prepared' && phase.value === 'review') {
    cancelling.value = true
    try {
      await api.del(`/logs/retention/plans/${enc(p.planId)}`)
      toast('已取消清理计划，没有删除任何数据', 'info')
    } catch (err) {
      toast(errorTitle(err), 'error', errorDetail(err))
    } finally {
      cancelling.value = false
    }
  }
  plan.value = null
  emit('done', false)
}

async function confirmCommit(): Promise<void> {
  const p = plan.value
  if (!p) return
  const ok = await confirmAction({
    title: p.affectedRows ? `确认删除 ${integer(p.affectedRows)} 条${what.value}？` : '确认保存新策略？',
    detail: p.affectedRows
      ? `将保存“${what.value}只保留最近 ${p.days} 天”，并删除 ${fullTime(p.cutoff)} 之前的数据，涉及 ${p.affectedFiles} 个数据库（共 ${bytes(p.affectedBytes)}）。删除后无法恢复。`
      : `将保存“${what.value}只保留最近 ${p.days} 天”。当前没有需要删除的数据。`,
    confirmText: p.affectedRows ? '确认删除' : '保存',
    danger: Boolean(p.affectedRows),
    requireText: p.affectedRows ? `删除${p.days}天前的${what.value}` : undefined,
  })
  if (!ok) return
  const op = await commit.run(() =>
    api.post<Operation>(`/logs/retention/plans/${enc(p.planId)}/commit`, undefined, {
      ifMatch: p.revision,
      headers: { 'Idempotency-Key': newIdempotencyKey() },
    }),
  )
  if (op?.status === 'succeeded') toast('数据保留策略已生效', 'success')
}

function close(): void {
  if (phase.value === 'running') return
  if (phase.value === 'review' || phase.value === 'scan') cancelPlan()
  else emit('done', phase.value === 'done')
}

const STEPS: Record<string, string> = {
  'retention.running': '执行中',
  'retention.item_start': '准备数据库',
  'retention.trim_delete': '删除过期记录',
  'retention.trim_vacuum': '压缩数据库以释放空间',
  'retention.item_done': '已完成一个数据库',
}
</script>

<template>
  <Modal :open="Boolean(request)" :title="`${what}清理预览`" :subtitle="request ? `新策略：只保留最近 ${request.days} 天` : ''" width="md" :dismissible="phase !== 'running'" @close="close">
    <div v-if="phase === 'scan'">
      <template v-if="createError">
        <p class="bad"><TriangleAlert /> 无法生成清理计划：{{ createError }}</p>
        <p class="hint">扫描未完成时不会保存策略，也不会删除数据。</p>
      </template>
      <div v-else class="scan">
        <p class="hint">正在扫描各月份的日志数据库，只统计数量，不读取正文、不删除任何数据…</p>
        <Skeleton h="16px" /><Skeleton h="16px" w="70%" /><Skeleton h="16px" w="85%" />
      </div>
    </div>

    <template v-else-if="plan && phase === 'review'">
      <p class="note">尚未保存策略，也没有删除任何数据。确认后才会执行。</p>
      <dl class="facts">
        <div><dt>清理范围</dt><dd>{{ isBodies ? '请求 Headers、请求 / 响应正文和 MCP 返回内容；摘要、Token、费用等统计永久保留' : '请求摘要、原始请求 / 响应、重试、代理与本地调用明细' }}</dd></div>
        <div><dt>清理临界点</dt><dd>{{ fullTime(plan.cutoff) }} 之前（北京时间）</dd></div>
        <div class="big"><dt>将删除</dt><dd><b>{{ integer(plan.affectedRows) }}</b> 条记录 · {{ plan.affectedFiles }} 个数据库（这些数据库共 {{ bytes(plan.affectedBytes) }}）</dd></div>
        <div><dt>已扫描</dt><dd>{{ plan.scannedFiles }} 个数据库 · {{ integer(plan.scannedRows) }} 条 · {{ bytes(plan.scannedBytes) }}</dd></div>
        <div><dt>预检</dt><dd :class="plan.preflightOk ? 'c-green' : 'c-red'">{{ plan.preflightOk ? '通过' : '未通过' }}</dd></div>
      </dl>
      <div v-if="items.length" class="items">
        <p class="items-h">按数据库明细（{{ items.length }} 个）</p>
        <div v-for="it in pagedItems" :key="it.database" class="it">
          <span class="it-m">
            <b class="mono">{{ it.month }}</b>
            <Badge v-if="it.active" tone="blue">当前写入月</Badge>
          </span>
          <span class="it-a">{{ ACTION[it.action] || '清理' }}</span>
          <span class="it-n num">
            <span>删除 <b>{{ integer(it.deletedRows) }}</b> / {{ integer(it.totalRows) }} 条</span>
            <small v-if="it.deletedCallRows">另含 {{ integer(it.deletedCallRows) }} 条{{ isBodies ? ' MCP 结果' : '关联明细' }}</small>
          </span>
          <span class="it-b">
            <span v-if="it.releasableBytes !== null">可释放 <b class="num">{{ bytes(it.releasableBytes) }}</b></span>
            <span v-else>压缩后释放</span>
            <small>{{ it.releasableBytes !== null ? `当前 ${bytes(it.currentBytes)}` : it.willCompact ? `计划压缩，需可用空间 ${bytes(it.compactionRequiredBytes)}` : it.active ? '当前写入月不压缩，空间由后续写入复用' : '本次暂不压缩' }}</small>
          </span>
          <span v-if="it.note" class="it-note">{{ it.note }}</span>
        </div>
        <Pager v-model:page="itemPage" :page-size="ITEM_PAGE" :total="items.length" />
      </div>
      <ul v-if="plan.errors.length" class="errs">
        <li v-for="e in plan.errors" :key="e">{{ e }}</li>
      </ul>
      <p v-if="!plan.affectedRows" class="ok"><CircleCheck /> 当前没有早于临界点的数据，确认后只保存新策略，之后由每日维护按期清理。</p>
      <p v-else class="warn"><TriangleAlert /> 删除不可恢复{{ isBodies ? '；正文清理在后台分批进行，不影响请求转发' : '；对应的历史统计和日志详情将不再可查' }}。</p>
      <p class="hint">此预览 {{ expiresIn }} 后失效。{{ isBodies ? '' : '数据库删除后，如空间足够会自动压缩旧月份以释放磁盘。' }}</p>
    </template>

    <template v-else-if="phase === 'running'">
      <OpProgress :op="commit.op.value" :title="`正在清理${what}`" :steps="STEPS" />
      <p class="hint">请勿关闭页面。大数据库的压缩可能需要几分钟。</p>
    </template>

    <template v-else-if="phase === 'done' && result">
      <p class="ok big-ok"><CircleCheck /> 策略已生效：{{ what }}只保留最近 {{ result.days || request?.days }} 天</p>
      <dl class="facts">
        <template v-if="result.scope === 'bodies'">
          <div><dt>涉及数据库</dt><dd>{{ result.affectedFiles ?? 0 }} 个</dd></div>
          <div><dt>后台清理</dt><dd>{{ result.backgroundCleanupStarted ? '已开始，进度见页面上的「正文清理进度」' : '已在运行或无需清理' }}</dd></div>
        </template>
        <template v-else>
          <div><dt>已删除</dt><dd>{{ integer(result.deletedRows ?? 0) }} 条记录 · {{ result.affectedFiles ?? 0 }} 个数据库</dd></div>
          <div><dt>逻辑数据减少</dt><dd>{{ bytes(result.logicalBytesRemoved ?? 0) }}</dd></div>
          <div><dt>实际释放磁盘</dt><dd>{{ bytes(result.actualFreeBytes ?? 0) }}</dd></div>
        </template>
      </dl>
      <ul v-if="result.notCompacted?.length" class="errs">
        <li v-for="n in result.notCompacted" :key="n.month">{{ n.month }} 已删除旧记录、暂未压缩：{{ n.reason }}</li>
      </ul>
    </template>

    <template v-else-if="phase === 'failed'">
      <OpProgress v-if="commit.op.value" :op="commit.op.value" :title="`正在清理${what}`" />
      <p class="bad"><TriangleAlert /> 清理未完成：{{ commit.error.value }}</p>
      <p class="hint">如果策略已经保存，系统会在后续维护周期继续清理；也可以刷新页面查看当前状态后重试。</p>
    </template>

    <template #footer>
      <template v-if="phase === 'scan'">
        <Btn variant="ghost" :loading="cancelling" @click="cancelPlan">{{ createError ? '关闭' : '取消' }}</Btn>
      </template>
      <template v-else-if="phase === 'review'">
        <Btn variant="ghost" :loading="cancelling" @click="cancelPlan">取消计划</Btn>
        <Btn :variant="plan?.affectedRows ? 'danger' : 'primary'" :disabled="!plan?.preflightOk || expiresIn === '已过期'" @click="confirmCommit">
          {{ plan?.affectedRows ? '确认保存并清理' : '确认保存策略' }}
        </Btn>
      </template>
      <Btn v-else-if="phase !== 'running'" variant="primary" @click="emit('done', phase === 'done')">完成</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.scan {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.note {
  font-size: 12.5px;
  color: var(--text-2);
}
.facts {
  display: flex;
  flex-direction: column;
  margin: 10px 0;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.facts > div {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 0.5px solid var(--hairline);
}
.facts > div:last-child {
  border-bottom: 0;
}
dt {
  font-size: 12px;
  color: var(--text-3);
}
dd {
  margin: 0;
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
.big dd b {
  font-size: 15px;
  color: var(--red);
}
.items {
  margin: 10px 0;
}
.items-h {
  margin-bottom: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
}
.it {
  display: grid;
  grid-template-columns: minmax(96px, 0.8fr) 84px minmax(0, 1.4fr) minmax(0, 1.4fr);
  align-items: center;
  gap: 4px 10px;
  padding: 7px 0;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.it-m {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 6px;
  min-width: 0;
}
.it-m b {
  white-space: nowrap;
}
.it-a {
  font-size: 12px;
  color: var(--text-2);
}
.it-n,
.it-b {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.it-n b {
  color: var(--red);
}
small {
  font-size: 11px;
  color: var(--text-3);
}
.it-note {
  grid-column: 1 / -1;
  font-size: 11.5px;
  color: var(--orange);
}
@media (max-width: 640px) {
  .it {
    grid-template-columns: minmax(0, 1fr) auto;
  }
}
.errs {
  margin: 6px 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--orange);
}
.ok,
.warn,
.bad {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12.5px;
  line-height: 1.5;
}
.ok svg,
.warn svg,
.bad svg {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  margin-top: 2px;
}
.ok {
  color: var(--green);
}
.big-ok {
  font-weight: 600;
}
.warn {
  color: var(--orange);
}
.bad {
  color: var(--red);
}
.hint {
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--text-3);
}
</style>
