<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'
import { CircleCheck, Download, LoaderCircle, Power, RotateCcw, TriangleAlert, Undo2 } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import type { Operation } from '@/api/operations'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { newIdempotencyKey } from '@/utils/format'
import OpProgress from '@/components/ui/OpProgress.vue'
import Btn from '@/components/ui/Btn.vue'
import { operationFailureText, useOperation } from '@/composables/useOperation'
import { normVersion, showVersion, STAGE_LABEL, TRANSIENT_STAGES, type ActivationPlan, type UpdateState } from './version'

// Two-step self-update: ① backup + download (“stage”), ② explicit confirm → restart.
// The staged state comes from `getUpdateState`, so a reload or another browser can continue:
// activation always asks the server for a fresh, actor-bound plan (`createUpdateActivationPlan`).
// The service restarts during ②, so the page follows it by polling until it answers again.

const props = defineProps<{ state: UpdateState | null; candidate: string | null; canUpdate: boolean }>()
const emit = defineEmits<{ changed: []; showLog: [] }>()

type Local = 'none' | 'staging' | 'activating' | 'reconnecting' | 'done' | 'failed'
const local = ref<Local>('none')
const stage = useOperation()
const activateOp = shallowRef<Operation | null>(null)
const message = ref('')
const outcome = ref<'updated' | 'rolledBack' | null>(null)
const busy = ref(false)
const waitedSeconds = ref(0)
const targetVersion = ref('')
const fromVersion = ref('')

const serverStage = computed(() => props.state?.stage || 'idle')
const staged = computed(() => Boolean(props.state?.activationAvailable))
/** What the panel shows: a local flow in progress wins over the server snapshot. */
const view = computed(() => {
  if (local.value !== 'none') return local.value
  if (staged.value) return 'staged'
  if (TRANSIENT_STAGES.has(serverStage.value) || props.state?.busy) return 'working'
  return 'idle'
})
const lastResult = computed(() => (['success', 'failed', 'rolled_back'].includes(serverStage.value) ? STAGE_LABEL[serverStage.value] : null))

const STAGE_STEPS: Record<string, string> = {
  UPDATE_BACKING_UP: '备份当前版本',
  UPDATE_PULLING: '下载新版本',
  UPDATE_STAGED: '已暂存，等待确认',
}
const ACTIVATE_STEPS: Record<string, string> = {
  UPDATE_RESTARTING: '正在重启服务',
  UPDATE_HEALTH_VERIFIED: '健康检查通过',
}

let alive = true
onBeforeUnmount(() => {
  alive = false
})

async function startStage(): Promise<void> {
  const v = props.candidate
  if (!v) return
  const ok = await confirmAction({
    title: `下载并暂存 ${showVersion(v)}？`,
    detail:
      `当前版本 ${showVersion(props.state?.currentVersion)}。\n\n` +
      '流程：① 自动备份当前版本 → ② 下载新版本 → ③ 停下来等你确认 → ④ 重启生效。\n\n' +
      '这一步只做备份和下载，不会重启服务，之后可以随时取消。',
    confirmText: '开始备份并下载',
  })
  if (!ok) return
  local.value = 'staging'
  message.value = ''
  targetVersion.value = v
  const op = await stage.run(() => api.post<Schemas['StageUpdateOperationData']>(`/updates/${enc(v)}/actions/stage`), { quiet: true })
  if (op?.status === 'succeeded') {
    toast(`${showVersion(v)} 已暂存，确认后即可重启生效`, 'success')
    local.value = 'none'
  } else {
    local.value = 'failed'
    message.value = op ? operationFailureText(op) : stage.error.value || '暂存失败'
  }
  emit('changed')
}

async function cancelStaged(): Promise<void> {
  const ok = await confirmAction({
    title: '取消已暂存的更新？',
    detail: '会放弃已下载的新版本并恢复到当前版本的文件，服务不会重启。',
    confirmText: '取消暂存',
    danger: true,
  })
  if (!ok) return
  busy.value = true
  try {
    await api.del('/updates/staged', { ifMatch: props.state?.revision })
    toast('已取消暂存的更新', 'success')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = false
    emit('changed')
  }
}

async function activate(): Promise<void> {
  const st = props.state
  if (!st?.activationAvailable) return
  const target = st.targetVersion || st.toVersion || ''
  const ok = await confirmAction({
    title: `重启并激活 ${showVersion(target)}？`,
    detail:
      '服务会重启，正在进行的请求可能中断，网页会短暂断开，之后自动重连。\n\n' +
      '重启后会自动做健康检查；新版本启动失败会自动回滚到当前版本。',
    confirmText: '重启并激活',
    danger: true,
  })
  if (!ok) return
  busy.value = true
  let uptime = 0
  try {
    uptime = (await api.get<{ uptimeSeconds: number }>('/overview')).data.uptimeSeconds
  } catch {
    /* best effort */
  }
  const bootBefore = Date.now() - uptime * 1000
  try {
    // A fresh plan works after a reload or in another browser; issuing it retires older tokens.
    const plan = (await api.post<ActivationPlan>('/updates/staged/actions/activation-plan', undefined, { ifMatch: st.revision })).data
    const res = await api.post<Operation>('/updates/staged/actions/restart', { planToken: plan.planToken }, {
      ifMatch: plan.revision,
      headers: { 'Idempotency-Key': newIdempotencyKey() },
    })
    activateOp.value = res.data
    targetVersion.value = plan.targetVersion
    fromVersion.value = st.currentVersion
    follow(res.data.id, bootBefore)
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 8000)
    if (err instanceof ApiError && (err.code === 'REVISION_CONFLICT' || err.code === 'INVALID_OPERATION_STATE')) emit('changed')
  } finally {
    busy.value = false
  }
}

/** Poll the activation Operation, then the service itself, until a new process answers. */
async function follow(operationId: string, bootBefore: number): Promise<void> {
  local.value = 'activating'
  const started = Date.now()
  let sawDown = false
  while (alive && Date.now() - started < 5 * 60_000) {
    waitedSeconds.value = Math.round((Date.now() - started) / 1000)
    if (!sawDown) {
      try {
        const op = (await api.get<Operation>(`/operations/${enc(operationId)}`)).data
        activateOp.value = op
        if (op.status === 'failed' || op.status === 'cancelled') {
          // A rejection before the restart means nothing changed.
          await new Promise((r) => setTimeout(r, 3000))
          try {
            const ov = (await api.get<{ uptimeSeconds: number }>('/overview')).data
            if (Date.now() - ov.uptimeSeconds * 1000 < bootBefore + 5000) {
              local.value = 'failed'
              message.value = `重启未执行：${operationFailureText(op)}`
              emit('changed')
              return
            }
          } catch {
            sawDown = true
          }
        }
      } catch (err) {
        if (err instanceof ApiError && (err.status === 0 || err.status === 404 || err.status >= 500)) sawDown = true
      }
    }
    if (sawDown) local.value = 'reconnecting'
    try {
      const ov = (await api.get<{ uptimeSeconds: number; version: string }>('/overview')).data
      if (Date.now() - ov.uptimeSeconds * 1000 > bootBefore + 5000) {
        outcome.value = normVersion(ov.version) === normVersion(targetVersion.value) ? 'updated' : 'rolledBack'
        message.value =
          outcome.value === 'updated'
            ? `已更新到 ${showVersion(ov.version)}，服务运行正常。`
            : `服务已恢复，但版本仍是 ${showVersion(ov.version)}，新版本可能未通过健康检查并已自动回滚。`
        local.value = 'done'
        emit('changed')
        return
      }
    } catch {
      sawDown = true
      local.value = 'reconnecting'
    }
    await new Promise((r) => setTimeout(r, 2000))
  }
  if (alive) {
    local.value = 'failed'
    message.value = '等待服务恢复超过 5 分钟。请检查服务器状态，或稍后刷新本页。'
  }
}

function reset(): void {
  local.value = 'none'
  message.value = ''
  outcome.value = null
  stage.reset()
  activateOp.value = null
  emit('changed')
}

const stagedVersion = computed(() => props.state?.targetVersion || props.state?.toVersion || '')
</script>

<template>
  <div class="flow">
    <div class="steps">
      <span :class="{ on: view === 'staging' || view === 'working', done: ['staged', 'activating', 'reconnecting', 'done'].includes(view) }">① 备份并下载</span>
      <span :class="{ on: view === 'staged', done: ['activating', 'reconnecting', 'done'].includes(view) }">② 确认重启</span>
      <span :class="{ on: view === 'activating' || view === 'reconnecting', done: view === 'done' && outcome === 'updated' }">③ 健康检查</span>
    </div>

    <template v-if="view === 'idle'">
      <p v-if="lastResult" class="last" :class="`t-${lastResult.tone}`">
        {{ lastResult.label }}<template v-if="state?.toVersion || state?.targetVersion">：{{ showVersion(state?.fromVersion) }} → {{ showVersion(state?.toVersion || state?.targetVersion) }}</template>
        <span v-if="state?.message" class="muted">（{{ state.message }}）</span>
        <button v-if="state?.stage !== 'success'" type="button" class="lnk" @click="emit('showLog')">查看失败日志</button>
      </p>
      <p class="txt">
        {{ canUpdate && candidate ? `可以更新到 ${showVersion(candidate)}。先备份并下载，确认后才会重启。` : '当前没有可安装的新版本。' }}
      </p>
      <div v-if="canUpdate && candidate" class="acts">
        <Btn variant="primary" @click="startStage"><Download /> 更新到 {{ showVersion(candidate) }}</Btn>
      </div>
    </template>

    <template v-else-if="view === 'staging'">
      <OpProgress :op="stage.op.value" :title="`正在准备 ${showVersion(targetVersion)}`" :steps="STAGE_STEPS" />
      <p class="txt muted">这一步不会重启服务，可以离开本页，稍后回来继续。</p>
    </template>

    <template v-else-if="view === 'working'">
      <p class="wait"><LoaderCircle class="spin" /> {{ STAGE_LABEL[serverStage]?.label || '更新进行中' }}<template v-if="stagedVersion">：{{ showVersion(stagedVersion) }}</template>…</p>
      <p class="txt muted">更新正在进行（可能由 Telegram 或自动更新发起），完成后这里会自动刷新。</p>
    </template>

    <template v-else-if="view === 'staged'">
      <p class="ok"><CircleCheck /> {{ showVersion(stagedVersion) }} 已备份并下载完成，等待你确认重启。</p>
      <p class="txt muted">在任何浏览器或 Telegram 里都可以继续这一步；确认时会重新签发一次性的激活凭据。</p>
      <div class="acts">
        <Btn variant="danger" :loading="busy" @click="activate"><Power /> 重启并激活</Btn>
        <Btn variant="ghost" :loading="busy" @click="cancelStaged"><Undo2 /> 取消已暂存的更新</Btn>
      </div>
    </template>

    <template v-else-if="view === 'activating' || view === 'reconnecting'">
      <OpProgress v-if="view === 'activating' && activateOp" :op="activateOp" title="正在激活" :steps="ACTIVATE_STEPS" />
      <p v-if="view === 'reconnecting'" class="wait"><LoaderCircle class="spin" /> 服务正在重启，网页暂时断开，正在自动重连…（{{ waitedSeconds }} 秒）</p>
      <p class="txt muted">如果新版本启动失败，会自动回滚到 {{ showVersion(fromVersion) }}。请不要关闭本页。</p>
    </template>

    <template v-else-if="view === 'done'">
      <p :class="outcome === 'updated' ? 'ok' : 'warn'">
        <CircleCheck v-if="outcome === 'updated'" /><TriangleAlert v-else /> {{ message }}
      </p>
      <div class="acts">
        <Btn v-if="outcome !== 'updated'" size="sm" @click="emit('showLog')">查看失败日志</Btn>
        <Btn size="sm" variant="ghost" @click="reset"><RotateCcw /> 完成</Btn>
      </div>
    </template>

    <template v-else-if="view === 'failed'">
      <OpProgress v-if="stage.op.value && stage.op.value.status !== 'succeeded'" :op="stage.op.value" title="" />
      <p class="warn"><TriangleAlert /> {{ message || '更新未完成' }}</p>
      <div class="acts">
        <Btn size="sm" @click="emit('showLog')">查看失败日志</Btn>
        <Btn size="sm" variant="ghost" @click="reset"><RotateCcw /> 返回</Btn>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.steps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.steps span {
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11.5px;
  color: var(--text-3);
  background: var(--fill-3);
}
.steps span.on {
  color: var(--accent);
  background: var(--accent-soft);
  font-weight: 500;
}
.steps span.done {
  color: var(--green);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}
.txt {
  font-size: 12.5px;
  color: var(--text-2);
}
.last {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  font-size: 12.5px;
}
.last.t-green {
  color: var(--green);
}
.last.t-red {
  color: var(--red);
}
.last.t-orange {
  color: var(--orange);
}
.lnk {
  font-size: 12px;
  color: var(--accent);
}
.acts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.ok,
.warn,
.wait {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 13px;
  line-height: 1.5;
}
.ok svg,
.warn svg,
.wait svg {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  margin-top: 1px;
}
.ok {
  color: var(--green);
}
.warn {
  color: var(--orange);
}
.wait {
  color: var(--accent);
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
