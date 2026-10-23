<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChartBar, ChevronLeft, ChevronRight, KeyRound, Plug, Puzzle, RefreshCw, ScrollText, TriangleAlert } from '@lucide/vue'
import { api, ApiError, type Schemas } from '@/api/client'
import { inspector, openInspector, toast } from '@/stores/ui'
import { dateTime, integer, ms, percent, relative } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import { goPage, openApiKey } from '@/links'
import Page from '@/components/ui/Page.vue'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import CopyText from '@/components/ui/CopyText.vue'
import Empty from '@/components/ui/Empty.vue'
import Field from '@/components/ui/Field.vue'
import Meter from '@/components/ui/Meter.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import StatusDot from '@/components/ui/StatusDot.vue'
import Switch from '@/components/ui/Switch.vue'
import { adoptServer, useResource, useSection } from '@/composables/useResource'
import McpLogDetail from './McpLogDetail.vue'
import { errorLabel, STATUS_LABEL, subjectOf, toolIcon, toolLabel, TOOLS } from './labels'

type Settings = Schemas['McpSettingsData']
type Log = Schemas['McpCallLogData']
type LogList = Schemas['McpLogListData']
type Stats = Schemas['McpStatsData']

const res = useResource<Settings>('/mcp', { successText: 'MCP 设置已保存' })
const adv = useSection(res, ['mediaTtlSeconds', 'maxRequestBodyBytes'])
const data = computed(() => res.data.value)
const d = computed(() => res.draft.value)

const MB = 1024 * 1024
const bodyMb = computed({
  get: () => (d.value ? Math.round((d.value.maxRequestBodyBytes / MB) * 10) / 10 : null),
  set: (v: number | null) => {
    if (d.value && v !== null) d.value.maxRequestBodyBytes = Math.round(v * MB)
  },
})
const ttlMin = computed({
  get: () => (d.value ? Math.round((d.value.mediaTtlSeconds / 60) * 10) / 10 : null),
  set: (v: number | null) => {
    if (d.value && v !== null) d.value.mediaTtlSeconds = Math.round(v * 60)
  },
})

const state = computed(() => {
  const s = data.value
  if (!s) return { label: '加载中', tone: 'gray' as const }
  if (!s.enabled) return { label: '已关闭', tone: 'gray' as const }
  if (!s.runtime.mounted) return { label: '装配失败', tone: 'orange' as const }
  return { label: '运行中', tone: 'green' as const }
})
const enabledTools = computed(() => TOOLS.filter((t) => data.value?.tools[t.name]).length)
const endpoint = computed(() => `${window.location.origin}${data.value?.runtime.path || '/mcp'}`)
const configJson = computed(() =>
  JSON.stringify({ mcpServers: { parrot: { type: 'http', url: endpoint.value, headers: { Authorization: 'Bearer <Parrot API Key>' } } } }, null, 2),
)
const bridgeCmd = computed(() => `npx -y mcp-remote ${endpoint.value} --header "Authorization: Bearer <Key>"`)

async function write(fn: (rev?: string) => Promise<{ data: Settings }>, ok: string): Promise<boolean> {
  try {
    const out = await fn(res.data.value?.revision)
    adoptServer(res, out.data)
    toast(ok, 'success')
    return true
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      await res.load()
      toast('MCP 设置已在别处被修改', 'warning', '已载入最新数据，请重新操作。')
    } else toast(errorTitle(err), 'error', errorDetail(err), 7000)
    return false
  }
}

const busy = ref('')
async function toggleService(v: boolean): Promise<void> {
  busy.value = 'service'
  await write((rev) => api.patch<Settings>('/mcp', { enabled: v }, { ifMatch: rev }), v ? '已开启 MCP 服务' : '已关闭 MCP 服务')
  busy.value = ''
}
async function toggleTool(name: string, v: boolean): Promise<void> {
  busy.value = name
  await write((rev) => api.put<Settings>('/mcp/tools', { toolName: name, enabled: v }, { ifMatch: rev }), `已${v ? '开启' : '关闭'}「${toolLabel(name)}」`)
  busy.value = ''
}

// ---- keys that may use MCP ----
const keys = ref<{ keyId: string; name: string; enabled: boolean; allowMcp: boolean }[]>([])
const keysLoaded = ref(false)
async function loadKeys(): Promise<void> {
  try {
    const r = await api.get<{ items: { keyId: string; name: string; enabled: boolean; allowMcp: boolean }[] }>('/api-keys', { pageSize: 200 })
    keys.value = r.data.items || []
  } catch {
    keys.value = []
  } finally {
    keysLoaded.value = true
  }
}
const mcpKeys = computed(() => keys.value.filter((k) => k.allowMcp))

// ---- stats & logs ----
const period = ref<'today' | 'month'>('today')
const stats = ref<Stats | null>(null)
const statsErr = ref('')
async function loadStats(): Promise<void> {
  try {
    stats.value = (await api.get<Stats>('/mcp/stats', { period: period.value })).data
    statsErr.value = ''
  } catch (err) {
    statsErr.value = errorTitle(err)
  }
}
const statTotals = computed(() => {
  const items = stats.value?.items || []
  const calls = items.reduce((a, r) => a + r.calls, 0)
  const success = items.reduce((a, r) => a + r.success, 0)
  const failed = items.reduce((a, r) => a + r.failed, 0)
  const avg = calls ? items.reduce((a, r) => a + r.avgElapsedMs * r.calls, 0) / calls : null
  return { calls, success, failed, avg, max: Math.max(1, ...items.map((r) => r.calls)) }
})

const tool = ref('')
const key = ref('')
const page = ref(1)
const PAGE = 20
const logs = ref<LogList | null>(null)
const logsLoading = ref(false)
const logsErr = ref('')
const now = ref(Date.now())
let seq = 0
async function loadLogs(): Promise<void> {
  const my = ++seq
  logsLoading.value = true
  try {
    const r = await api.get<LogList>('/mcp/logs', {
      period: period.value,
      toolName: tool.value || undefined,
      apiKeyName: key.value || undefined,
      page: page.value,
      pageSize: PAGE,
    })
    if (my !== seq) return
    logs.value = r.data
    logsErr.value = ''
    now.value = Date.now()
  } catch (err) {
    if (my === seq) logsErr.value = errorTitle(err)
  } finally {
    if (my === seq) logsLoading.value = false
  }
}
watch(period, () => {
  page.value = 1
  loadStats()
  loadLogs()
})
watch([tool, key], () => {
  page.value = 1
  loadLogs()
})
function go(dlt: number): void {
  page.value = Math.max(1, page.value + dlt)
  loadLogs()
}
function refresh(): void {
  res.load()
  loadStats()
  loadLogs()
}

function openLog(l: Log): void {
  openInspector(`mcplog:${l.id}`, 'MCP 调用详情', McpLogDetail, { log: l }, `${toolLabel(l.toolName)} · ${dateTime(l.createdAt)}`)
}
const selected = computed(() => (inspector.open && inspector.key.startsWith('mcplog:') ? inspector.key.slice(7) : ''))

const toolOptions = [{ value: '', label: '全部工具' }, ...TOOLS.map((t) => ({ value: t.name, label: t.label }))]
const keyOptions = computed(() => [{ value: '', label: '全部 Key' }, ...keys.value.map((k) => ({ value: k.name, label: k.name }))])

onMounted(() => {
  loadKeys()
  loadStats()
  loadLogs()
})
</script>

<template>
  <Page title="MCP" subtitle="Parrot 自带的 MCP 服务：让 Claude Code、Cursor 等客户端直接调用搜索、抓取、图片和视频工具。">
    <template #actions>
      <Btn icon title="刷新" @click="refresh"><RefreshCw /></Btn>
    </template>

    <p v-if="res.error.value" class="err">MCP 设置加载失败：{{ errorTitle(res.error.value) }}</p>

    <div class="top">
      <Card title="服务状态">
        <template #icon><Plug /></template>
        <template #actions>
          <Switch :model-value="data?.enabled" :loading="busy === 'service'" :disabled="!data" label="MCP 总开关" @update:model-value="toggleService" />
        </template>
        <div v-if="!data" class="skl"><Skeleton h="28px" w="40%" /><Skeleton h="14px" /><Skeleton h="14px" w="60%" /></div>
        <template v-else>
          <div class="status">
            <StatusDot :tone="state.tone" :size="10" :pulse="state.tone === 'green'" />
            <span class="st">{{ state.label }}</span>
            <span class="faint">· 已启用 {{ enabledTools }}/{{ TOOLS.length }} 个工具</span>
          </div>
          <p v-if="data.runtime.reason" class="warn"><TriangleAlert /> {{ data.runtime.reason }}</p>
          <dl class="kv">
            <div class="full"><dt>服务地址</dt><dd><CopyText :text="endpoint" /></dd></div>
            <div><dt>服务路径</dt><dd class="mono">{{ data.runtime.path }}</dd></div>
            <div><dt>媒体资源路径</dt><dd class="mono">{{ data.runtime.mediaPath }}</dd></div>
          </dl>
          <div class="keys">
            <p class="kt">可以使用 MCP 的 API Key <span v-if="keysLoaded" class="faint">{{ mcpKeys.length }} 个</span></p>
            <Skeleton v-if="!keysLoaded" h="26px" w="60%" r="999px" />
            <div v-else-if="mcpKeys.length" class="chips">
              <button v-for="k in mcpKeys" :key="k.keyId" type="button" class="chip" :class="{ off: !k.enabled }" @click="openApiKey(k.keyId)">
                <KeyRound />{{ k.name }}<span v-if="!k.enabled" class="faint">（已停用）</span>
              </button>
            </div>
            <p v-else class="faint small">还没有 Key 开启 MCP。请到「下游 API Key」里为某个 Key 开启。</p>
            <Btn size="sm" variant="ghost" @click="goPage('/keys')">管理 API Key</Btn>
          </div>
        </template>
      </Card>

      <Card title="接入方式" subtitle="在 MCP 客户端里填入以下配置，把 <Parrot API Key> 换成开启了 MCP 的 Key">
        <template #icon><Puzzle /></template>
        <p class="lbl">HTTP 客户端（Claude Code / Cursor 等）</p>
        <div class="code">
          <pre>{{ configJson }}</pre>
          <CopyText class="cp" :text="configJson" display="" />
        </div>
        <p class="lbl">不支持 HTTP 的客户端，用 mcp-remote 桥接</p>
        <div class="code">
          <pre>{{ bridgeCmd }}</pre>
          <CopyText class="cp" :text="bridgeCmd" display="" />
        </div>
      </Card>
    </div>

    <Card title="工具" subtitle="关闭后，所有客户端都无法调用该工具；单个 Key 能用哪些工具在「下游 API Key」里设置">
      <template #icon><Puzzle /></template>
      <div v-if="!data" class="tools"><Skeleton v-for="i in 6" :key="i" h="84px" r="10px" /></div>
      <div v-else class="tools">
        <div v-for="t in TOOLS" :key="t.name" class="tool" :class="{ off: !data.tools[t.name] || !data.enabled }">
          <span class="ti"><component :is="t.icon" /></span>
          <div class="tt">
            <p class="tn">{{ t.label }} <span class="mono faint">{{ t.name }}</span></p>
            <p class="td">{{ t.desc }}</p>
          </div>
          <Switch :model-value="Boolean(data.tools[t.name])" :loading="busy === t.name" :label="t.label" @update:model-value="(v: boolean) => toggleTool(t.name, v)" />
        </div>
      </div>
      <p v-if="data && !data.enabled" class="note">MCP 总开关已关闭，工具开关暂不生效。</p>
    </Card>

    <SettingsSection
      title="高级设置"
      description="媒体链接有效期和请求体大小上限。"
      :loading="res.loading.value && !d"
      :dirty="adv.dirty.value"
      :saving="adv.saving.value"
      @save="adv.save()"
      @reset="adv.reset()"
    >
      <template v-if="d">
        <Field inline label="媒体链接有效期" hint="工具返回的图片 / 视频链接多久后失效（1 分钟 – 7 天）">
          <NumberInput v-model="ttlMin" :min="1" :max="10080" unit="分钟" />
        </Field>
        <Field inline label="请求体上限" hint="图片编辑需要上传图片，默认远高于 SDK 的 4 MB（1 – 256 MB）">
          <NumberInput v-model="bodyMb" :min="1" :max="256" unit="MB" />
        </Field>
      </template>
    </SettingsSection>

    <div class="bottom">
      <Card title="调用统计" :subtitle="period === 'today' ? '今天' : '本月'" flush>
        <template #icon><ChartBar /></template>
        <template #actions>
          <Segmented
            v-model="period"
            size="sm"
            :options="[
              { value: 'today', label: '今天' },
              { value: 'month', label: '本月' },
            ]"
          />
        </template>
        <p v-if="statsErr" class="state">{{ statsErr }}</p>
        <div v-else-if="!stats" class="skl pad"><Skeleton v-for="i in 3" :key="i" h="14px" /></div>
        <Empty v-else-if="!stats.items.length" title="这段时间还没有调用" />
        <div v-else class="stats">
          <div class="sum">
            <div><p class="k">调用</p><p class="v">{{ integer(statTotals.calls) }}</p></div>
            <div><p class="k">成功</p><p class="v c-green">{{ integer(statTotals.success) }}</p></div>
            <div><p class="k">失败</p><p class="v" :class="{ 'c-red': statTotals.failed }">{{ integer(statTotals.failed) }}</p></div>
            <div><p class="k">平均耗时</p><p class="v">{{ ms(statTotals.avg) }}</p></div>
          </div>
          <button
            v-for="r in stats.items"
            :key="r.toolName"
            type="button"
            class="srow"
            :class="{ on: tool === r.toolName }"
            title="只看这个工具的调用记录"
            @click="tool = tool === r.toolName ? '' : r.toolName"
          >
            <span class="s1">
              <component :is="toolIcon(r.toolName)" class="si" />
              <span class="sn">{{ toolLabel(r.toolName) }}</span>
              <span class="num">{{ integer(r.calls) }} 次</span>
            </span>
            <Meter :value="(r.calls / statTotals.max) * 100" :warn="101" :danger="101" />
            <span class="s2 faint">
              <span>成功 {{ integer(r.success) }}</span>
              <span>失败 <b :class="{ 'c-red': r.failed }">{{ integer(r.failed) }}</b></span>
              <span>成功率 {{ percent(r.success, r.calls) }}</span>
              <span>平均 {{ ms(r.avgElapsedMs) }}</span>
              <span>最近 {{ relative(r.lastAt * 1000, now) }}</span>
            </span>
          </button>
        </div>
      </Card>

      <Card title="调用记录" subtitle="包括被拒绝、没有发出上游请求的调用；点击查看详情" flush>
        <template #icon><ScrollText /></template>
        <template #actions>
          <Select v-model="tool" width="120px" :options="toolOptions" />
          <Select v-model="key" width="130px" :options="keyOptions" />
          <Btn size="sm" variant="ghost" icon title="刷新" :loading="logsLoading" @click="loadLogs"><RefreshCw /></Btn>
        </template>
        <p v-if="logsErr" class="state">{{ logsErr }}</p>
        <div v-else class="logs">
          <template v-if="logsLoading && !logs">
            <div v-for="i in 8" :key="i" class="pad"><Skeleton h="14px" /></div>
          </template>
          <Empty v-else-if="!logs?.items.length" title="没有调用记录" :detail="tool || key ? '换个筛选条件试试' : undefined" />
          <div v-else :class="{ dim: logsLoading }">
            <button
              v-for="l in logs.items"
              :key="l.id"
              type="button"
              class="lrow"
              :class="[`s-${l.status}`, { sel: selected === String(l.id) }]"
              @click="openLog(l)"
            >
              <span class="lt mono">{{ dateTime(l.createdAt) }}</span>
              <span class="ls"><i class="dot" />{{ STATUS_LABEL[l.status] || l.status }}</span>
              <span class="ltool"><component :is="toolIcon(l.toolName)" class="si" />{{ toolLabel(l.toolName) }}</span>
              <span class="lq truncate" :title="subjectOf(l).value">{{ subjectOf(l).value || '—' }}</span>
              <span class="lm mono">{{ ms(l.elapsedMs) }}<span class="faint"> · {{ l.resultCount }} 项</span></span>
              <span class="lk truncate">
                <span v-if="l.errorCode" class="c-red">{{ errorLabel(l.errorCode) }}</span>
                <span v-else class="faint">{{ l.apiKeyName || '—' }}</span>
              </span>
            </button>
          </div>
        </div>
        <div class="pager">
          <span class="faint">第 {{ page }} 页</span>
          <Btn icon size="sm" :disabled="page <= 1 || logsLoading" @click="go(-1)"><ChevronLeft /></Btn>
          <Btn icon size="sm" :disabled="!logs?.hasNext || logsLoading" @click="go(1)"><ChevronRight /></Btn>
        </div>
      </Card>
    </div>
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
.top,
.bottom {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}
.bottom {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.7fr);
}
@container (max-width: 1000px) {
  .top,
  .bottom {
    grid-template-columns: minmax(0, 1fr);
  }
}
.skl {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.pad {
  padding: 12px 14px;
}
.status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}
.st {
  font-family: var(--font-display);
  font-size: 20px;
  font-weight: 700;
}
.warn {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--orange);
}
.warn svg {
  width: 14px;
  height: 14px;
}
.kv {
  margin: 12px 0 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 16px;
}
.kv .full {
  grid-column: 1 / -1;
}
dt {
  font-size: 11px;
  color: var(--text-3);
}
dd {
  margin: 2px 0 0;
  font-size: 12.5px;
  min-width: 0;
}
.keys {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 0.5px solid var(--hairline);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.kt {
  font-size: 12px;
  font-weight: 600;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--fill-2);
}
.chip:hover {
  color: var(--accent);
}
.chip svg {
  width: 12px;
  height: 12px;
  color: var(--yellow);
}
.small {
  font-size: 12px;
}
.lbl {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--text-2);
}
.lbl:not(:first-child) {
  margin-top: 12px;
}
.code {
  display: flex;
  align-items: flex-start;
  gap: 4px;
}
.code pre {
  flex: 1;
  min-width: 0;
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--surface-sunken);
  box-shadow: inset 0 0 0 0.5px var(--border);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.55;
  white-space: pre;
  overflow-x: auto;
}
.cp {
  flex-shrink: 0;
  margin-top: 6px;
}
.tools {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 10px;
  align-items: stretch;
}
.tool {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 100%;
  padding: 12px 14px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  transition: opacity 0.2s;
}
.tool.off .ti {
  color: var(--text-3);
  background: var(--fill-2);
}
.ti {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  flex-shrink: 0;
  color: var(--green);
  background: color-mix(in srgb, var(--green) 14%, transparent);
}
.ti svg {
  width: 17px;
  height: 17px;
}
.tt {
  flex: 1;
  min-width: 0;
}
.tn {
  font-weight: 600;
}
.tn .mono {
  font-weight: 400;
  font-size: 10.5px;
}
.td {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-3);
}
.note {
  margin-top: 10px;
  font-size: 12px;
  color: var(--orange);
}
.state {
  padding: 40px 16px;
  text-align: center;
  color: var(--text-3);
}
.stats {
  border-top: 0.5px solid var(--hairline);
}
.sum {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 26px;
  padding: 12px 14px;
  border-bottom: 0.5px solid var(--hairline);
}
.k {
  font-size: 11px;
  color: var(--text-3);
}
.v {
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.srow {
  display: flex;
  flex-direction: column;
  gap: 5px;
  width: 100%;
  padding: 10px 14px;
  text-align: left;
  border-bottom: 0.5px solid var(--hairline);
}
.srow:hover {
  background: var(--fill-3);
}
.srow.on {
  background: var(--accent-soft);
}
.s1 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}
.sn {
  flex: 1;
  font-weight: 550;
}
.si {
  width: 13px;
  height: 13px;
  color: var(--text-3);
  flex-shrink: 0;
}
.s2 {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 12px;
  font-size: 11.5px;
}
.logs {
  container-type: inline-size;
  border-top: 0.5px solid var(--hairline);
}
.lrow {
  display: grid;
  grid-template-columns: 108px 62px 92px minmax(120px, 2fr) 96px minmax(70px, 0.8fr);
  grid-template-areas: 'lt ls ltool lq lm lk';
  align-items: center;
  column-gap: 10px;
  width: 100%;
  min-height: 36px;
  padding: 0 14px;
  text-align: left;
  font-size: 12.5px;
  border-bottom: 0.5px solid var(--hairline);
}
.lrow:hover {
  background: var(--fill-3);
}
.lrow.sel {
  background: var(--accent-soft);
}
.s-success { --tone: var(--green); }
.s-error { --tone: var(--red); }
.s-timeout,
.s-denied { --tone: var(--orange); }
.s-running { --tone: var(--blue); }
.lt { grid-area: lt; font-size: 11.5px; color: var(--text-2); }
.ls { grid-area: ls; display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--tone); }
.ltool { grid-area: ltool; display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.lq { grid-area: lq; }
.lm { grid-area: lm; text-align: right; font-size: 11.5px; white-space: nowrap; }
.lk { grid-area: lk; text-align: right; font-size: 11.5px; }
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--tone);
}
.dim {
  opacity: 0.55;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 8px 14px 10px;
  font-size: 12px;
}
@container (max-width: 640px) {
  .lrow {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas: 'ltool ls' 'lq lq' 'lt lm' 'lk lk';
    row-gap: 3px;
    padding: 9px 12px;
  }
  .lk {
    text-align: left;
  }
  .lt {
    font-size: 11px;
    color: var(--text-3);
  }
}
</style>
