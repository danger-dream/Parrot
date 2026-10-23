<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Archive, BellOff, Bell, CircleArrowUp, ExternalLink, FileWarning, RefreshCw, Trash2, TriangleAlert, X } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { runAction, useResource } from '@/composables/useResource'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { dateTime, fullTime, humanSeconds, relative } from '@/utils/format'
import Page from '@/components/ui/Page.vue'
import Card from '@/components/ui/Card.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import CopyText from '@/components/ui/CopyText.vue'
import Pager from '@/components/ui/Pager.vue'
import UpdateFlow from './UpdateFlow.vue'
import { MODE_LABEL, showVersion, STAGE_LABEL, TRANSIENT_STAGES, type UpdateState } from './version'

type Settings = Schemas['UpdateSettingsData']
type Check = Schemas['UpdateCheckData']
type Backup = Schemas['UpdateBackupData']

// ── settings ──
const res = useResource<Settings>('/updates/settings')
const { data: settings, draft, dirty, saving, loading, error } = res

function applySettings(v: Settings): void {
  const wasDirty = dirty.value
  settings.value = v
  if (!wasDirty) res.reset()
}

// ── version / state / check ──
const upd = ref<UpdateState | null>(null)
const stateError = ref('')
const check = ref<Check | null>(null)
const checkLoaded = ref(false)
const checking = ref(false)

let pollTimer = 0
async function loadState(): Promise<void> {
  try {
    upd.value = (await api.get<UpdateState>('/updates/state')).data
    stateError.value = ''
  } catch (err) {
    stateError.value = errorTitle(err)
  }
  window.clearTimeout(pollTimer)
  // Follow updates started elsewhere (Telegram, auto update) while they are in progress.
  if (upd.value && (TRANSIENT_STAGES.has(upd.value.stage) || upd.value.busy)) pollTimer = window.setTimeout(loadState, 3000)
}
async function loadCachedCheck(): Promise<void> {
  try {
    check.value = (await api.get<Check>('/updates/check')).data
  } catch {
    /* shown as “尚未检查” */
  } finally {
    checkLoaded.value = true
  }
}
async function runCheck(): Promise<void> {
  checking.value = true
  try {
    check.value = (await api.post<Check>('/updates/actions/check')).data
    if (!check.value.candidateVersion) toast('没有找到可用的发布版本', 'warning')
    else toast(check.value.newer ? `发现新版本 ${showVersion(check.value.candidateVersion)}` : '已是最新版本', check.value.newer ? 'info' : 'success')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    checking.value = false
  }
}

const current = computed(() => upd.value?.currentVersion || check.value?.currentVersion || '')
const latest = computed(() => check.value?.candidateVersion || null)
const ignored = computed(() => Boolean(latest.value && settings.value?.ignoredVersions.includes(latest.value)))
const newer = computed(() => Boolean(check.value?.newer))
const state = computed<{ label: string; tone: 'green' | 'blue' | 'neutral' }>(() => {
  if (!latest.value) return { label: '尚未检查', tone: 'neutral' }
  if (newer.value && ignored.value) return { label: '新版本已忽略', tone: 'neutral' }
  if (newer.value) return { label: '有新版本', tone: 'blue' }
  return { label: '已是最新', tone: 'green' }
})
const stageBadge = computed(() => (upd.value && upd.value.stage !== 'idle' ? STAGE_LABEL[upd.value.stage] || { label: '更新状态未知', tone: 'neutral' as const } : null))

const ignoring = ref(false)
async function ignore(v: string): Promise<void> {
  ignoring.value = true
  const r = await runAction(() => api.put<Settings>(`/updates/ignored-versions/${enc(v)}`, undefined, { ifMatch: settings.value?.revision }), `已忽略 ${showVersion(v)}，不再提醒`)
  ignoring.value = false
  if (r) applySettings(r)
}
async function unignore(v: string): Promise<void> {
  ignoring.value = true
  const r = await runAction(() => api.del(`/updates/ignored-versions/${enc(v)}`, { ifMatch: settings.value?.revision }), `已取消忽略 ${showVersion(v)}`)
  if (r) {
    try {
      applySettings((await api.get<Settings>('/updates/settings')).data)
    } catch {
      res.load()
    }
  }
  ignoring.value = false
}
async function clearIgnored(): Promise<void> {
  const n = settings.value?.ignoredVersions.length || 0
  const ok = await confirmAction({ title: `清空忽略列表（${n} 个版本）？`, detail: '这些版本之后会重新出现在更新提醒中。', confirmText: '清空' })
  if (!ok) return
  ignoring.value = true
  try {
    const r = await api.patch<Settings>('/updates/settings', { ignoredVersions: [] }, { ifMatch: settings.value?.revision })
    applySettings(r.data)
    toast('已清空忽略列表', 'success')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    ignoring.value = false
  }
}

// ── backups ──
const backups = ref<Backup[]>([])
const bTotal = ref(0)
const bPage = ref(1)
const bLoading = ref(true)
const bError = ref('')
async function loadBackups(p = bPage.value): Promise<void> {
  bLoading.value = true
  try {
    const r = await api.get<{ items: Backup[] }>('/updates/backups', { page: p, pageSize: 10 })
    backups.value = r.data.items
    bTotal.value = r.meta.total ?? r.data.items.length
    bPage.value = p
    bError.value = ''
  } catch (err) {
    bError.value = errorTitle(err)
  } finally {
    bLoading.value = false
  }
}

// ── failure log ──
const log = ref<string | null>(null)
const logLoading = ref(false)
const logError = ref('')
const logCard = ref<HTMLElement | null>(null)
async function loadLog(): Promise<void> {
  logLoading.value = true
  try {
    log.value = (await api.get<Schemas['UpdateFailureLogData']>('/updates/failure-log')).data.content
    logError.value = ''
  } catch (err) {
    logError.value = errorTitle(err)
  } finally {
    logLoading.value = false
  }
}
async function showLog(): Promise<void> {
  await loadLog()
  await nextTick()
  logCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function onFlowChanged(): void {
  loadState()
  loadCachedCheck()
  loadBackups(1)
}

onMounted(() => {
  loadState()
  loadCachedCheck()
  loadBackups(1)
  loadLog()
})
onBeforeUnmount(() => window.clearTimeout(pollTimer))
</script>

<template>
  <Page title="版本更新" subtitle="检查新版本、一键备份并更新，失败自动回滚。" width="normal">
    <Card>
      <div class="ver">
        <div class="vcol">
          <span class="lab">当前版本</span>
          <b v-if="current" class="mono big">{{ showVersion(current) }}</b>
          <Skeleton v-else w="80px" h="26px" />
        </div>
        <CircleArrowUp class="arrow" :class="{ hot: newer && !ignored }" />
        <div class="vcol">
          <span class="lab">最新版本</span>
          <b class="mono big">{{ latest ? showVersion(latest) : '—' }}</b>
        </div>
        <div class="vstate">
          <Badge :tone="state.tone" dot>{{ state.label }}</Badge>
          <Badge v-if="check?.prerelease" tone="purple">预发布</Badge>
          <Badge v-if="stageBadge" :tone="stageBadge.tone">{{ stageBadge.label }}</Badge>
        </div>
        <div class="vacts">
          <Btn :loading="checking" @click="runCheck"><RefreshCw /> 检查更新</Btn>
          <Btn v-if="latest && newer && !ignored" variant="ghost" :loading="ignoring" @click="ignore(latest)"><BellOff /> 忽略此版本</Btn>
          <Btn v-if="latest && ignored" variant="ghost" :loading="ignoring" @click="unignore(latest)"><Bell /> 取消忽略</Btn>
        </div>
      </div>
      <div v-if="upd" class="meta">
        <span>运行形态 <b>{{ MODE_LABEL[upd.mode] || upd.mode }}</b></span>
        <span>仓库 <a :href="`https://github.com/${upd.repository}`" target="_blank" rel="noopener noreferrer" class="mono">{{ upd.repository }}</a></span>
        <span v-if="check?.candidateVersion && check.publishedAt">最新发布 <b :title="dateTime(check.publishedAt)">{{ relative(check.publishedAt) }}</b></span>
      </div>
      <p v-else-if="stateError" class="err"><TriangleAlert /> 无法读取更新状态：{{ stateError }}</p>

      <div v-if="check?.candidateVersion" class="rel">
        <p class="rel-h">
          <b>{{ check.candidateName || showVersion(check.candidateVersion) }}</b>
          <span v-if="check.publishedAt" class="muted" :title="dateTime(check.publishedAt)">发布于 {{ fullTime(check.publishedAt) }}</span>
          <a v-if="check.releaseUrl" :href="check.releaseUrl" target="_blank" rel="noopener noreferrer" class="ext">发布页 <ExternalLink /></a>
        </p>
        <pre v-if="check.changelog" class="log-body">{{ check.changelog }}</pre>
        <p v-else class="muted small">这个版本没有提供更新说明。</p>
      </div>
      <p v-else-if="checkLoaded" class="muted small tip">还没有检查过新版本，点击「检查更新」联网获取。</p>

      <div class="divider" />
      <UpdateFlow :state="upd" :candidate="latest" :can-update="newer && !ignored" @changed="onFlowChanged" @show-log="showLog" />
    </Card>

    <SettingsSection
      title="更新设置"
      description="后台定时检查 GitHub 上的发布版本；发现新版本时通过 Telegram 通知。"
      :dirty="dirty"
      :saving="saving"
      :loading="loading && !draft"
      :error="error ? errorTitle(error) : ''"
      @save="res.save()"
      @reset="res.reset()"
    >
      <template v-if="draft">
        <Field label="自动检查更新" hint="定时检查是否有新版本并通过 Telegram 提醒。关闭后不再后台检查，但页面上的「检查更新」仍会联网获取（不推送通知）。" inline>
          <Switch v-model="draft.enabled" label="自动检查更新" />
        </Field>
        <Field label="包含预发布版本" hint="开启后也会提示 beta / rc 等预发布版本；关闭只提示正式版。" inline>
          <Switch v-model="draft.includePrerelease" label="包含预发布版本" />
        </Field>
        <Field label="自动下载新版本" hint="发现新版本时自动备份并下载（暂存）。重启仍需人工确认（网页或 Telegram），不会自动重启。" inline>
          <Switch v-model="draft.autoUpdate" label="自动下载新版本" />
        </Field>
        <Field label="检查间隔" :hint="`多久检查一次（300–604800 秒，即 5 分钟到 7 天）。当前约 ${humanSeconds(draft.intervalSeconds)}，推荐 3600 秒。`" inline>
          <NumberInput :model-value="draft.intervalSeconds" :min="300" :max="604800" integer unit="秒" @update:model-value="$event !== null && (draft!.intervalSeconds = $event)" />
        </Field>
        <Field label="已忽略的版本" hint="这些版本不会再提醒或出现在一键更新中。点 × 取消忽略。">
          <div class="ign">
            <span v-if="!settings?.ignoredVersions.length" class="muted small">没有忽略的版本</span>
            <span v-for="v in settings?.ignoredVersions || []" :key="v" class="chip">
              <span class="mono">{{ showVersion(v) }}</span>
              <button type="button" :disabled="ignoring" :aria-label="`取消忽略 ${v}`" @click="unignore(v)"><X /></button>
            </span>
            <Btn v-if="(settings?.ignoredVersions.length || 0) > 1" size="sm" variant="ghost" :loading="ignoring" @click="clearIgnored"><Trash2 /> 清空</Btn>
          </div>
        </Field>
      </template>
    </SettingsSection>

    <Card title="备份" subtitle="每次更新前自动创建的备份，回滚时使用。">
      <template #icon><Archive /></template>
      <template #actions>
        <Btn size="sm" variant="ghost" :loading="bLoading" @click="loadBackups()"><RefreshCw /> 刷新</Btn>
      </template>
      <p v-if="bError" class="err"><TriangleAlert /> {{ bError }}</p>
      <div v-else-if="bLoading && !backups.length" class="sk"><Skeleton v-for="i in 2" :key="i" h="30px" /></div>
      <Empty v-else-if="!backups.length" title="还没有备份" detail="第一次通过这里或 Telegram 更新时会自动创建。" />
      <template v-else>
        <div v-for="b in backups" :key="b.ref" class="brow">
          <span class="bv mono">{{ showVersion(b.version) }} → {{ showVersion(b.targetVersion) }}</span>
          <Badge>{{ MODE_LABEL[b.mode] || '其他方式' }}</Badge>
          <span class="bref"><CopyText :text="b.ref" /></span>
          <span class="bt muted" :title="dateTime(b.createdAt)">{{ b.createdAt ? relative(b.createdAt) : '—' }}</span>
        </div>
        <Pager :page="bPage" :page-size="10" :total="bTotal" :disabled="bLoading" @update:page="loadBackups" />
      </template>
    </Card>

    <div ref="logCard">
      <Card title="更新失败日志" subtitle="最近一次更新失败或回滚时的日志（已脱敏）。">
        <template #icon><FileWarning /></template>
        <template #actions>
          <Btn size="sm" variant="ghost" :loading="logLoading" @click="loadLog"><RefreshCw /> 刷新</Btn>
        </template>
        <p v-if="logError" class="err"><TriangleAlert /> {{ logError }}</p>
        <div v-else-if="log === null" class="sk"><Skeleton h="60px" /></div>
        <Empty v-else-if="!log.trim()" title="没有失败记录" detail="更新一直很顺利。" />
        <pre v-else class="log-body">{{ log }}</pre>
      </Card>
    </div>
  </Page>
</template>

<style scoped>
.ver {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 20px;
}
.vcol {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.lab {
  font-size: 11.5px;
  color: var(--text-3);
}
.big {
  font-size: 22px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.arrow {
  width: 22px;
  height: 22px;
  color: var(--text-3);
  transform: rotate(90deg);
}
.arrow.hot {
  color: var(--accent);
}
.vstate {
  display: flex;
  gap: 6px;
}
.vacts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-left: auto;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 18px;
  margin-top: 12px;
  font-size: 12px;
  color: var(--text-3);
}
.meta b {
  font-weight: 500;
  color: var(--text-2);
}
.meta a {
  color: var(--accent);
}
.rel {
  margin-top: 14px;
}
.rel-h {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  margin-bottom: 8px;
  font-size: 13px;
}
.ext {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  color: var(--accent);
}
.ext svg {
  width: 12px;
  height: 12px;
}
.log-body {
  max-height: 340px;
  overflow: auto;
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--text-2);
}
.small {
  font-size: 12px;
}
.tip {
  margin-top: 12px;
}
.divider {
  height: 0.5px;
  margin: 16px 0 14px;
  background: var(--hairline);
}
.ign {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 28px;
  padding: 0 4px 0 10px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--fill-2);
}
.chip button {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  color: var(--text-3);
}
.chip button:hover {
  color: var(--red);
  background: var(--fill);
}
.chip button svg {
  width: 12px;
  height: 12px;
}
.brow {
  display: grid;
  grid-template-columns: minmax(140px, auto) auto minmax(0, 1fr) 90px;
  align-items: center;
  gap: 12px;
  min-height: 36px;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.bref {
  min-width: 0;
  font-size: 11.5px;
  color: var(--text-2);
}
.bt {
  font-size: 12px;
  text-align: right;
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
@container (max-width: 640px) {
  .vacts {
    margin-left: 0;
    width: 100%;
  }
  .brow {
    grid-template-columns: minmax(0, 1fr) auto;
    padding: 6px 0;
  }
  .bref {
    grid-column: 1 / -1;
  }
  .bt {
    display: none;
  }
}
</style>
