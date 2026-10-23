<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Activity, RefreshCw, Snowflake, TriangleAlert } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { refreshCatalog } from '@/stores/catalog'
import { errorTitle } from '@/utils/errors'
import { dateTime, humanSeconds, relative } from '@/utils/format'
import Card from '@/components/ui/Card.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import EntityLink from '@/components/ui/EntityLink.vue'

type Job = Schemas['BackgroundJobData']
type Cooldown = Schemas['CooldownData']

const JOB: Record<string, { label: string; hint: string }> = {
  walCheckpoint: { label: '日志库整理', hint: '定期把日志数据库的写入合并落盘' },
  pendingCleanup: { label: '清理未完成请求', hint: '清理异常中断后遗留的“进行中”记录' },
  affinityCleanup: { label: '会话亲和清理', hint: '清除已过期的会话与渠道绑定' },
  oauthRefresh: { label: 'OAuth 授权续期', hint: '在授权过期前自动刷新令牌' },
  quotaMonitor: { label: 'OAuth 额度监控', hint: '定时读取账号用量并按阈值暂停 / 恢复' },
  oauthModelSync: { label: 'OAuth 模型同步', hint: '同步各账号可用的模型列表' },
  cooldownProbe: { label: '冷却恢复探测', hint: '定时探测冷却中的渠道是否已恢复' },
  providerUsage: { label: '供应商用量刷新', hint: '刷新各供应商的额度与用量信息' },
}
const JOB_STATUS: Record<Job['status'], { label: string; tone: 'green' | 'red' | 'blue' | 'neutral' | 'orange' }> = {
  running: { label: '运行中', tone: 'blue' },
  succeeded: { label: '上次成功', tone: 'green' },
  failed: { label: '上次失败', tone: 'red' },
  unknown: { label: '尚未运行', tone: 'neutral' },
  disabled: { label: '已停用', tone: 'neutral' },
}
/** Run history is in-process (R2-13): it restarts empty, and providerUsage has no loop of its own. */
function lastText(j: Job): string {
  if (j.lastRunAt) return relative(j.lastRunAt)
  if (j.status === 'disabled') return '—'
  return j.id === 'providerUsage' ? '随请求刷新' : '本次启动后未运行'
}
function nextText(j: Job): string {
  if (!j.nextRunAt) return j.status === 'disabled' ? '不会运行' : '—'
  const left = (new Date(j.nextRunAt).getTime() - Date.now()) / 1000
  if (left <= 1) return '即将运行'
  return `${j.lastRunAt ? '' : '首次 '}${humanSeconds(left)}后`
}
/** Configured interval, or the cadence implied by the last/next run times. */
function intervalText(j: Job): string {
  if (j.intervalSeconds) return `每 ${humanSeconds(j.intervalSeconds)}`
  if (j.lastRunAt && j.nextRunAt) {
    const gap = (new Date(j.nextRunAt).getTime() - new Date(j.lastRunAt).getTime()) / 1000
    if (gap > 0) return `约每 ${humanSeconds(gap)}`
  }
  return '按需'
}

const jobs = ref<Job[]>([])
const cooldowns = ref<Cooldown[]>([])
const cdTotal = ref(0)
const loading = ref(true)
const jobsError = ref('')
const cdError = ref('')

async function load(): Promise<void> {
  loading.value = true
  refreshCatalog().catch(() => undefined)
  const [j, c] = await Promise.allSettled([
    api.get<Job[]>('/runtime/background-jobs', { pageSize: 200 }),
    api.get<Cooldown[]>('/runtime/cooldowns', { pageSize: 200 }),
  ])
  if (j.status === 'fulfilled') {
    jobs.value = j.value.data
    jobsError.value = ''
  } else jobsError.value = errorTitle(j.reason)
  if (c.status === 'fulfilled') {
    cooldowns.value = c.value.data
    cdTotal.value = c.value.meta.total ?? c.value.data.length
    cdError.value = ''
  } else cdError.value = errorTitle(c.reason)
  loading.value = false
}

let timer = 0
onMounted(() => {
  load()
  timer = window.setInterval(load, 30_000)
})
onBeforeUnmount(() => window.clearInterval(timer))

function jobName(id: string): string {
  return JOB[id]?.label || id
}
</script>

<template>
  <Card title="运行状态" subtitle="后台任务的运行记录（服务重启后清空）与当前处于冷却中的渠道，每 30 秒自动刷新。">
    <template #icon><Activity /></template>
    <template #actions>
      <Btn size="sm" variant="ghost" :loading="loading" @click="load"><RefreshCw /> 刷新</Btn>
    </template>

    <h4 class="sub">后台任务</h4>
    <p v-if="jobsError" class="err"><TriangleAlert /> {{ jobsError }}</p>
    <div v-else-if="loading && !jobs.length" class="sk"><Skeleton v-for="i in 4" :key="i" h="30px" /></div>
    <Empty v-else-if="!jobs.length" title="没有后台任务" />
    <div v-else class="tbl jobs">
      <div class="tr th">
        <span>任务</span><span>状态</span><span>执行间隔</span><span>上次运行</span><span>下次运行</span>
      </div>
      <div v-for="j in jobs" :key="j.id" class="tr">
        <span class="name">
          <b>{{ jobName(j.id) }}</b>
          <small>{{ JOB[j.id]?.hint || '' }}</small>
        </span>
        <span><Badge :tone="JOB_STATUS[j.status]?.tone || 'neutral'" dot>{{ JOB_STATUS[j.status]?.label || '状态未知' }}</Badge></span>
        <span class="num c-int">{{ intervalText(j) }}</span>
        <span class="num c-last" :title="j.lastRunAt ? dateTime(j.lastRunAt) : ''">{{ lastText(j) }}</span>
        <span class="num c-next" :title="j.nextRunAt ? dateTime(j.nextRunAt) : ''">{{ nextText(j) }}</span>
        <span class="num c-mob">上次 {{ lastText(j) }} · 下次 {{ nextText(j) }}</span>
        <span v-if="j.error" class="jerr">上次失败原因：{{ j.error }}</span>
      </div>
    </div>

    <h4 class="sub"><Snowflake /> 冷却中的渠道 <span v-if="cdTotal" class="cnt">{{ cdTotal }}</span></h4>
    <p v-if="cdError" class="err"><TriangleAlert /> {{ cdError }}</p>
    <div v-else-if="loading && !cooldowns.length" class="sk"><Skeleton v-for="i in 2" :key="i" h="30px" /></div>
    <Empty v-else-if="!cooldowns.length" title="没有渠道在冷却" detail="渠道连续失败时会按「错误冷却」阶梯暂停，出现时会列在这里。" />
    <div v-else class="tbl cds">
      <div class="tr th">
        <span>渠道</span><span>模型</span><span>状态</span><span>连续失败</span><span>解除时间</span>
      </div>
      <div v-for="c in cooldowns" :key="c.channelId + c.model" class="tr">
        <span class="name"><EntityLink kind="channel" :id="c.channelId" /></span>
        <span class="mdl">
          <EntityLink v-if="c.model" kind="model" :id="c.model" />
          <span v-else class="muted">整个渠道</span>
        </span>
        <span><Badge :tone="c.state === 'permanent' ? 'red' : 'orange'" dot>{{ c.state === 'permanent' ? '永久冷却' : '冷却中' }}</Badge></span>
        <span class="num">{{ c.errorCount }} 次</span>
        <span class="num" :title="dateTime(c.until)">{{ c.state === 'permanent' ? '需手动解除' : c.until ? relative(c.until) : '—' }}</span>
        <span v-if="c.message" class="jerr muted">{{ c.message }}</span>
      </div>
    </div>
  </Card>
</template>

<style scoped>
.sub {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 4px 0 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
}
.sub + * {
  margin-bottom: 14px;
}
.sub svg {
  width: 13px;
  height: 13px;
  color: var(--teal);
}
.cnt {
  padding: 0 6px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 14%, transparent);
}
.tbl {
  display: flex;
  flex-direction: column;
}
.tr {
  display: grid;
  align-items: center;
  column-gap: 12px;
  min-height: 38px;
  padding: 6px 0;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.tr:last-child {
  border-bottom: 0;
}
.jobs .tr {
  grid-template-columns: minmax(0, 2.2fr) 90px 100px 100px 90px;
}
.cds .tr {
  grid-template-columns: minmax(0, 1.6fr) minmax(0, 1.6fr) 90px 80px 110px;
}
.th {
  min-height: 28px;
  font-size: 11px;
  color: var(--text-3);
}
.name {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.name b {
  font-weight: 500;
}
.name small {
  font-size: 11px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mdl {
  min-width: 0;
  display: flex;
}
.num {
  font-variant-numeric: tabular-nums;
  color: var(--text-2);
  font-size: 12px;
}
.c-mob {
  display: none;
}
.jerr {
  grid-column: 1 / -1;
  font-size: 11.5px;
  color: var(--red);
}
.err {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-2);
}
.err svg {
  width: 14px;
  height: 14px;
  color: var(--orange);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
@container (max-width: 720px) {
  .th {
    display: none;
  }
  .jobs .tr {
    grid-template-columns: minmax(0, 1fr) auto;
    row-gap: 2px;
  }
  .jobs .tr .c-int,
  .jobs .tr .c-last,
  .jobs .tr .c-next {
    display: none;
  }
  .jobs .tr .c-mob {
    display: block;
    grid-column: 1 / -1;
    font-size: 11px;
    color: var(--text-3);
  }
  .cds .tr {
    grid-template-columns: minmax(0, 1fr) auto;
    row-gap: 4px;
  }
}
</style>
