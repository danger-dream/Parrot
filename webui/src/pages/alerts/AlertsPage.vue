<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ArrowUpRight, BellOff, BellRing, ExternalLink, RefreshCw, TriangleAlert } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import type { Operation } from '@/api/operations'
import { runAction, useResource } from '@/composables/useResource'
import { goPage } from '@/links'
import { toast } from '@/stores/ui'
import { errorTitle } from '@/utils/errors'
import { dateTime, humanSeconds, relative } from '@/utils/format'
import Page from '@/components/ui/Page.vue'
import Card from '@/components/ui/Card.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Select from '@/components/ui/Select.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Pager from '@/components/ui/Pager.vue'
import OpProgress from '@/components/ui/OpProgress.vue'
import { useOperation } from '@/composables/useOperation'
import { IMPACT, MIN_IMPACT_OPTIONS, PROVIDERS, providerLabel, statusLabel, type Provider } from './labels'

type Settings = Schemas['StatusAlertSettingsData']
type Incident = Schemas['StatusIncidentData']
type View = 'active' | 'history' | 'muted'

// ── settings ──
const res = useResource<Settings>('/status-alerts/settings')
const { draft, dirty, saving, loading, error } = res

function hasTarget(p: Provider): boolean {
  return Boolean(draft.value?.targets.includes(p))
}
function setTarget(p: Provider, on: boolean): void {
  if (!draft.value) return
  const set = new Set(draft.value.targets)
  if (on) set.add(p)
  else set.delete(p)
  draft.value.targets = PROVIDERS.map((x) => x.value).filter((v) => set.has(v))
}

// ── incidents ──
const view = ref<View>('active')
const provider = ref<'' | Provider>('')
const items = ref<Incident[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const listLoading = ref(true)
const listError = ref('')
const busy = ref('')

const VIEWS: { value: View; label: string }[] = [
  { value: 'active', label: '当前事件' },
  { value: 'history', label: '历史事件' },
  { value: 'muted', label: '已静音' },
]
const providerOptions = computed(() => [{ value: '' as '' | Provider, label: '全部服务' }, ...PROVIDERS.map((p) => ({ value: p.value as '' | Provider, label: p.label }))])

let seq = 0
async function loadList(p = page.value): Promise<void> {
  const mine = ++seq
  listLoading.value = true
  try {
    const r = await api.get<{ items: Incident[] }>('/status-alerts/incidents', {
      view: view.value,
      provider: provider.value || undefined,
      page: p,
      pageSize,
    })
    if (mine !== seq) return
    items.value = r.data.items
    total.value = r.meta.total ?? r.data.items.length
    page.value = p
    listError.value = ''
  } catch (err) {
    if (mine !== seq) return
    listError.value = errorTitle(err)
    items.value = []
  } finally {
    if (mine === seq) listLoading.value = false
  }
}
watch([view, provider], () => loadList(1))
onMounted(() => loadList(1))

const refresher = useOperation()
async function refresh(): Promise<void> {
  const op = await refresher.run(() => api.post<Operation>('/status-alerts/actions/refresh'))
  if (op?.status === 'succeeded') {
    const list = ((op.result as { refreshedProviders?: string[] } | null)?.refreshedProviders || []).map(providerLabel)
    toast(list.length ? `已刷新：${list.join('、')}` : '没有需要刷新的服务', list.length ? 'success' : 'warning', list.length ? undefined : '请先在订阅设置里选择监控目标。')
    loadList(1)
  }
}

async function mute(i: Incident): Promise<void> {
  busy.value = i.id
  const r = await runAction(() => api.post<Incident>(`/status-alerts/incidents/${enc(i.id)}/actions/mute`, undefined, { ifMatch: i.revision }), '已静音，此事件后续更新不再推送')
  busy.value = ''
  if (r) loadList()
}
async function unmute(i: Incident): Promise<void> {
  busy.value = i.id
  const r = await runAction(() => api.del(`/status-alerts/incidents/${enc(i.id)}/mute`, { ifMatch: i.revision }), '已取消静音')
  busy.value = ''
  if (r) loadList()
}

const emptyText = computed(() =>
  view.value === 'active'
    ? { title: '所有服务运行正常', detail: '已订阅的服务当前没有进行中的故障事件。' }
    : view.value === 'muted'
      ? { title: '没有静音的事件', detail: '在事件上点击「静音」后，它的后续更新不会再推送通知。' }
      : { title: '没有历史事件', detail: '' },
)
</script>

<template>
  <Page title="状态告警" subtitle="订阅 Claude、OpenAI、Cloudflare 官方状态页，发生故障时通过 Telegram 通知你。" width="normal">
    <SettingsSection
      title="订阅设置"
      description="Parrot 会定时读取各官方状态页；新事件或事件更新达到推送门槛时发送通知。"
      :dirty="dirty"
      :saving="saving"
      :loading="loading && !draft"
      :error="error ? errorTitle(error) : ''"
      @save="res.save()"
      @reset="res.reset()"
    >
      <template v-if="draft">
        <Field label="启用状态告警" hint="关闭后不再读取官方状态页，也不会推送故障通知。" inline>
          <Switch v-model="draft.enabled" label="启用状态告警" />
        </Field>
        <Field label="监控目标" hint="要订阅哪些服务的官方状态页，可多选。" inline>
          <div class="targets">
            <button
              v-for="p in PROVIDERS"
              :key="p.value"
              type="button"
              class="tg"
              :class="{ on: hasTarget(p.value) }"
              :aria-pressed="hasTarget(p.value)"
              @click="setTarget(p.value, !hasTarget(p.value))"
            >
              {{ p.label }}
            </button>
          </div>
        </Field>
        <Field label="检查间隔" :hint="`多久读取一次官方状态页（10–86400 秒），建议 30–300 秒。当前约 ${humanSeconds(draft.intervalSeconds)}。`" inline>
          <NumberInput :model-value="draft.intervalSeconds" :min="10" :max="86400" integer unit="秒" @update:model-value="$event !== null && (draft!.intervalSeconds = $event)" />
        </Field>
        <Field label="最小影响级别" hint="低于该级别的事件不主动推送，但仍会记录并显示在下方列表中。" inline>
          <Select v-model="draft.minImpact" :options="[...MIN_IMPACT_OPTIONS]" width="190px" />
        </Field>
        <Field label="Telegram 通知" hint="是否推送由「系统设置 → 通知 → 上游官网状态」控制。" inline>
          <div class="notif">
            <Badge :tone="draft.notificationEnabled ? 'green' : 'neutral'" dot>{{ draft.notificationEnabled ? '已开启' : '已关闭' }}</Badge>
            <button type="button" class="jump" @click="goPage('/settings', { section: 'notifications' })">去设置 <ArrowUpRight /></button>
          </div>
        </Field>
      </template>
    </SettingsSection>

    <Card title="事件">
      <template #icon><BellRing /></template>
      <template #actions>
        <Btn size="sm" variant="primary" :loading="refresher.running.value" @click="refresh"><RefreshCw /> 立即刷新</Btn>
      </template>
      <div class="bar">
        <Segmented v-model="view" :options="VIEWS" />
        <Select v-model="provider" :options="providerOptions" width="140px" />
      </div>
      <OpProgress v-if="refresher.op.value && refresher.running.value" :op="refresher.op.value" title="正在读取官方状态页" :steps="{ STATUS_PROVIDER_REFRESHED: '读取服务状态' }" class="gap" />

      <p v-if="listError" class="err"><TriangleAlert /> {{ listError }} <Btn size="sm" @click="loadList()">重试</Btn></p>
      <div v-else-if="listLoading && !items.length" class="sk"><Skeleton v-for="i in 3" :key="i" h="56px" /></div>
      <Empty v-else-if="!items.length" :title="emptyText.title" :detail="emptyText.detail" />
      <template v-else>
        <ul class="list" :class="{ dim: listLoading }">
          <li v-for="i in items" :key="i.provider + i.id" class="inc">
            <span class="imp" :class="`t-${IMPACT[i.impact]?.tone || 'neutral'}`" />
            <div class="main">
              <p class="t">
                <Badge>{{ providerLabel(i.provider) }}</Badge>
                <span class="name">{{ i.name || '未命名事件' }}</span>
              </p>
              <p class="meta">
                <Badge :tone="IMPACT[i.impact]?.tone || 'neutral'">{{ IMPACT[i.impact]?.label || '影响未知' }}</Badge>
                <span :class="{ 'c-green': i.status === 'resolved' || i.status === 'completed' }">{{ statusLabel(i.status) }}</span>
                <span :title="dateTime(i.createdAt)">开始 {{ relative(i.createdAt) }}</span>
                <span v-if="i.updatedAt" :title="dateTime(i.updatedAt)">更新 {{ relative(i.updatedAt) }}</span>
                <span v-if="i.muted" class="muted-tag"><BellOff /> 已静音{{ i.mutedAt ? ` · ${relative(i.mutedAt)}` : '' }}</span>
              </p>
            </div>
            <div class="acts">
              <a v-if="i.shortlink" class="ext" :href="i.shortlink" target="_blank" rel="noopener noreferrer" title="打开官方状态页"><ExternalLink /></a>
              <Btn v-if="i.muted" size="sm" :loading="busy === i.id" @click="unmute(i)">取消静音</Btn>
              <Btn v-else size="sm" variant="ghost" :loading="busy === i.id" @click="mute(i)"><BellOff /> 静音</Btn>
            </div>
          </li>
        </ul>
        <Pager :page="page" :page-size="pageSize" :total="total" :disabled="listLoading" @update:page="loadList" />
      </template>
    </Card>
  </Page>
</template>

<style scoped>
.targets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
}
.tg {
  height: 30px;
  padding: 0 12px;
  border-radius: 999px;
  font-size: 12.5px;
  color: var(--text-2);
  background: var(--fill-2);
  box-shadow: inset 0 0 0 0.5px var(--border);
  transition: background 0.15s, color 0.15s;
}
.tg.on {
  color: #fff;
  background: var(--accent);
  box-shadow: none;
}
.notif {
  display: flex;
  align-items: center;
  gap: 10px;
}
.jump {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  min-height: 28px;
  font-size: 12px;
  color: var(--accent);
}
.jump svg {
  width: 13px;
  height: 13px;
}
.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.gap {
  margin-bottom: 10px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  transition: opacity 0.2s;
}
.list.dim {
  opacity: 0.6;
}
.inc {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.inc:last-child {
  border-bottom: 0;
}
.imp {
  align-self: stretch;
  width: 3px;
  border-radius: 2px;
  flex-shrink: 0;
  background: var(--text-3);
}
.imp.t-orange {
  background: var(--orange);
}
.imp.t-red {
  background: var(--red);
}
.imp.t-purple {
  background: var(--purple);
}
.imp.t-blue {
  background: var(--blue);
}
.main {
  flex: 1;
  min-width: 0;
}
.t {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.name {
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 12px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-2);
}
.muted-tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: var(--text-3);
}
.muted-tag svg {
  width: 12px;
  height: 12px;
}
.acts {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.ext {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
}
.ext:hover {
  color: var(--accent);
  background: var(--fill-2);
}
.ext svg {
  width: 15px;
  height: 15px;
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-2);
}
.err svg {
  width: 15px;
  height: 15px;
  color: var(--orange);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
@container (max-width: 640px) {
  .inc {
    flex-wrap: wrap;
  }
  .name {
    white-space: normal;
  }
  .acts {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
