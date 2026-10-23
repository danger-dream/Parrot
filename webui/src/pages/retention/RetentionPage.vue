<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Database, Info, LoaderCircle, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import { goLogs } from '@/links'
import { errorTitle } from '@/utils/errors'
import { bytes, fullTime, integer, relative } from '@/utils/format'
import Page from '@/components/ui/Page.vue'
import Card from '@/components/ui/Card.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'
import Segmented from '@/components/ui/Segmented.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Btn from '@/components/ui/Btn.vue'
import Badge from '@/components/ui/Badge.vue'
import PlanModal, { type PlanRequest } from './PlanModal.vue'

type Retention = Schemas['RetentionSettingsData']
type Mode = 'forever' | 'days'

const res = useResource<Retention>('/logs/retention')
const { data, draft, dirty, saving, loading, error } = res

const MODES: { value: Mode; label: string }[] = [
  { value: 'forever', label: '永久保留' },
  { value: 'days', label: '按天保留' },
]

function setMode(kind: 'requests' | 'bodies', m: Mode): void {
  const d = draft.value
  if (!d) return
  if (kind === 'requests') {
    d.mode = m
    d.days = m === 'days' ? d.days ?? data.value?.days ?? 30 : null
  } else {
    d.bodyMode = m
    d.bodyDays = m === 'days' ? d.bodyDays ?? data.value?.bodyDays ?? 7 : null
  }
}

/** Enabling or shortening a policy widens the delete range and must go through a plan. */
function dangerous(curMode: Mode, curDays: number | null | undefined, mode: Mode, days: number | null | undefined): boolean {
  if (mode !== 'days' || !days) return false
  return curMode === 'forever' || (curDays ?? Infinity) > days
}
const reqDanger = computed(() => Boolean(data.value && draft.value && dangerous(data.value.mode, data.value.days, draft.value.mode, draft.value.days)))
const bodyDanger = computed(() =>
  Boolean(data.value && draft.value && dangerous(data.value.bodyMode ?? 'forever', data.value.bodyDays, draft.value.bodyMode ?? 'forever', draft.value.bodyDays)),
)

// ── plan flow ──
const queue = ref<PlanRequest[]>([])
const current = computed(() => queue.value[0] || null)

async function save(): Promise<void> {
  const cur = data.value
  const d = draft.value
  if (!cur || !d) return
  const body: Record<string, unknown> = {}
  if (d.logStoreBodies !== cur.logStoreBodies) body.logStoreBodies = d.logStoreBodies
  const reqChanged = d.mode !== cur.mode || (d.mode === 'days' && d.days !== cur.days)
  if (reqChanged && !reqDanger.value) {
    body.mode = d.mode
    if (d.mode === 'days') body.days = d.days
  }
  const bodyChanged = d.bodyMode !== cur.bodyMode || (d.bodyMode === 'days' && d.bodyDays !== cur.bodyDays)
  if (bodyChanged && !bodyDanger.value) {
    body.bodyMode = d.bodyMode
    if (d.bodyMode === 'days') body.bodyDays = d.bodyDays
  }
  const plans: PlanRequest[] = []
  if (reqDanger.value && d.days) plans.push({ scope: 'requests', days: d.days })
  if (bodyDanger.value && d.bodyDays) plans.push({ scope: 'bodies', days: d.bodyDays })
  const keep = { mode: d.mode, days: d.days, bodyMode: d.bodyMode, bodyDays: d.bodyDays }

  if (Object.keys(body).length) {
    const ok = await res.save(body)
    if (!ok) return
    // Keep the edits that still need a plan visible in the form.
    if (plans.length && draft.value) {
      if (plans.some((p) => p.scope === 'requests')) Object.assign(draft.value, { mode: keep.mode, days: keep.days })
      if (plans.some((p) => p.scope === 'bodies')) Object.assign(draft.value, { bodyMode: keep.bodyMode, bodyDays: keep.bodyDays })
    }
  }
  queue.value = plans
}

function onPlanDone(committed: boolean): void {
  if (!committed) {
    queue.value = []
    return
  }
  queue.value = queue.value.slice(1)
  if (!queue.value.length) reloadAll()
}

// ── live body-cleanup status (polled separately so the form draft is untouched) ──
const live = ref<Retention | null>(null)
const status = computed(() => live.value || data.value)
const bc = computed(() => {
  const b = status.value?.bodyCleanup
  return b ? { ...b, errors: b.errors || [], compaction: b.compaction || [] } : null
})
let timer = 0
async function poll(): Promise<void> {
  try {
    live.value = (await api.get<Retention>('/logs/retention')).data
  } catch {
    /* keep last */
  }
}
function reloadAll(): void {
  live.value = null
  res.load()
}
watch(
  () => status.value?.bodyCleanup?.running || status.value?.busy,
  (running) => {
    window.clearInterval(timer)
    if (running) timer = window.setInterval(poll, 4000)
  },
  { immediate: true },
)
onBeforeUnmount(() => window.clearInterval(timer))

const COMPACTION: Record<string, string> = {
  compacted: '已压缩',
  skipped: '暂未压缩',
  not_needed: '无需压缩',
  failed: '压缩失败',
}
</script>

<template>
  <Page title="日志保留" subtitle="请求日志和完整正文保存多久。缩短保留期前会先预览将删除的数据，确认后才执行。" width="normal">
    <template #actions>
      <Btn variant="ghost" @click="goLogs()">查看请求日志</Btn>
    </template>

    <div v-if="status" class="stats">
      <div class="kv">
        <span>当前请求日志</span>
        <b class="num">{{ integer(status.currentData.rows) }} 条</b>
      </div>
      <div class="kv">
        <span>请求日志</span>
        <b>{{ status.mode === 'days' ? `保留最近 ${status.days} 天` : '永久保留' }}</b>
      </div>
      <div class="kv">
        <span>完整正文</span>
        <b>{{ !status.logStoreBodies ? '不保存' : status.bodyMode === 'days' ? `保留最近 ${status.bodyDays} 天` : '永久保留' }}</b>
      </div>
      <div class="kv">
        <span>清理任务</span>
        <b v-if="status.busy" class="c-orange"><LoaderCircle class="spin" /> 正在执行</b>
        <b v-else class="c-green">空闲</b>
      </div>
    </div>

    <SettingsSection
      title="保留策略"
      description="延长保留期或改为永久保留会直接保存；开启按天保留或缩短天数会扩大删除范围，保存时先生成清理预览，由你确认后才删除。"
      :dirty="dirty"
      :saving="saving"
      :loading="loading && !draft"
      :error="error ? errorTitle(error) : ''"
      @save="save"
      @reset="res.reset()"
    >
      <template v-if="draft">
        <Field label="保存完整请求" hint="开启：保存请求 Headers、请求体和响应正文，便于排查。关闭：只保留摘要、Token、费用、重试和渠道统计。只影响之后的新请求，不会删除或补写历史正文。" inline>
          <Switch v-model="draft.logStoreBodies" label="保存完整请求" />
        </Field>

        <Field label="请求日志保留" hint="到期后删除整条请求记录（摘要、原始请求 / 响应、重试、代理明细），对应的历史统计也不再可查。不影响图片日志和翻译缓存。" inline>
          <div class="pol">
            <Segmented :model-value="draft.mode" :options="MODES" size="sm" @update:model-value="setMode('requests', $event)" />
            <NumberInput v-if="draft.mode === 'days'" :model-value="draft.days" :min="1" integer unit="天" width="110px" @update:model-value="$event !== null && (draft!.days = $event)" />
          </div>
        </Field>
        <p v-if="reqDanger" class="notice"><Info /> 请求日志将从{{ data?.mode === 'days' ? ` ${data?.days} 天缩短为` : '永久保留改为保留' }} {{ draft.days }} 天。点击保存会先扫描并预览要删除的数据，不会立即删除。</p>

        <Field label="完整正文保留" hint="到期后只清理请求 Headers、请求 / 响应正文和 MCP 返回内容；摘要、Token、费用、重试与代理统计永久保留。后台分批清理，不影响请求转发。" inline>
          <div class="pol">
            <Segmented :model-value="draft.bodyMode ?? 'forever'" :options="MODES" size="sm" @update:model-value="setMode('bodies', $event)" />
            <NumberInput v-if="draft.bodyMode === 'days'" :model-value="draft.bodyDays" :min="1" integer unit="天" width="110px" @update:model-value="$event !== null && (draft!.bodyDays = $event)" />
          </div>
        </Field>
        <p v-if="bodyDanger" class="notice"><Info /> 正文将从{{ data?.bodyMode === 'days' ? ` ${data?.bodyDays} 天缩短为` : '永久保留改为保留' }} {{ draft.bodyDays }} 天。点击保存会先预览要清理的正文，不会立即删除。</p>
        <p v-if="data?.busy" class="notice warn"><TriangleAlert /> 有清理任务正在执行，完成前不能修改保留策略。</p>
      </template>
    </SettingsSection>

    <Card v-if="bc" title="正文清理进度" subtitle="完整正文的后台清理状态（服务重启后清零）。">
      <template #icon><Database /></template>
      <template #actions>
        <Btn size="sm" variant="ghost" @click="poll"><RefreshCw /> 刷新</Btn>
      </template>
      <div class="bc">
        <p class="line">
          <Badge v-if="bc.running" tone="blue" dot>清理中{{ bc.month ? ` · ${bc.month}` : '' }}</Badge>
          <Badge v-else-if="bc.finishedAt" tone="green" dot>上次完成于 {{ relative(bc.finishedAt) }}</Badge>
          <Badge v-else dot>本次启动后尚未运行</Badge>
          <span v-if="bc.automatic" class="muted">每日自动维护</span>
        </p>
        <p v-if="bc.startedAt" class="line num">
          已清理 {{ integer(bc.purgedRequests) }} 条请求正文 · {{ integer(bc.purgedMcpResults) }} 条 MCP 结果 · 约 {{ bytes(bc.purgedBytes) }}
          <span v-if="bc.skippedActive" class="muted">（跳过 {{ bc.skippedActive }} 条进行中的请求）</span>
        </p>
        <p v-if="bc.startedAt" class="sub">开始于 {{ fullTime(bc.startedAt) }}{{ bc.finishedAt ? `，结束于 ${fullTime(bc.finishedAt)}` : '' }}</p>
        <p v-for="e in bc.errors" :key="e" class="bad"><TriangleAlert /> {{ e }}</p>
        <p v-if="bc.stopped" class="sub">{{ bc.stopped }}</p>
        <ul v-if="bc.compaction.length" class="comp">
          <li v-for="c in bc.compaction" :key="c.month">
            <span class="mono">{{ c.month }}</span>
            <Badge :tone="c.compacted ? 'green' : c.action === 'failed' ? 'red' : 'neutral'">{{ COMPACTION[c.action] || c.action }}</Badge>
            <span v-if="c.compacted" class="num">{{ bytes(c.beforeBytes) }} → {{ bytes(c.afterBytes) }}</span>
            <span v-else-if="c.reason" class="muted">{{ c.reason }}</span>
          </li>
        </ul>
      </div>
    </Card>

    <PlanModal :request="current" @done="onPlanDone" />
  </Page>
</template>

<style scoped>
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1px;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--hairline);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.kv {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 12px 16px;
  background: var(--surface);
  min-width: 0;
}
.kv span {
  font-size: 11.5px;
  color: var(--text-3);
}
.kv b {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 14px;
  font-weight: 600;
}
.spin {
  width: 14px;
  height: 14px;
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.pol {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
.notice {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 2px 0 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  line-height: 1.5;
  color: var(--accent);
  background: var(--accent-soft);
}
.notice.warn {
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 10%, transparent);
}
.notice svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.bc {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  font-size: 12.5px;
}
.sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.bad {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--red);
}
.bad svg {
  width: 13px;
  height: 13px;
}
.comp {
  list-style: none;
  margin: 4px 0 0;
  padding: 0;
}
.comp li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  padding: 5px 0;
  font-size: 12px;
  border-top: 0.5px solid var(--hairline);
}
</style>
