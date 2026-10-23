<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Plus, Trash2, TriangleAlert } from '@lucide/vue'
import { api } from '@/api/client'
import { openAccount, openChannel, openModel, openProxy } from '@/links'
import { catalog, channelName, refreshCatalog } from '@/stores/catalog'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Select from '@/components/ui/Select.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { channelProvider } from '@/utils/providers'
import { loadModelOptions } from '@/pages/keys/meta'
import { LEGACY_FUNCTIONS, PROVIDER_ROUTES, notifyProxiesChanged, proxyBus, targetLabel, type Proxy, type ProxyGroup, type Routing } from './meta'

type Section = 'accounts' | 'channels' | 'models'

const props = defineProps<{ proxies: Proxy[]; groups: ProxyGroup[] }>()

const routing = ref<Routing | null>(null)
const error = ref('')
const saving = ref('')
const models = ref<string[]>([])
const adding = ref<Record<Section, { key: string; target: string }>>({
  accounts: { key: '', target: 'direct' },
  channels: { key: '', target: 'direct' },
  models: { key: '', target: 'direct' },
})

async function load(): Promise<void> {
  try {
    routing.value = (await api.get<Routing>('/proxy-routing')).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  }
}
onMounted(() => {
  load()
  refreshCatalog().catch(() => undefined)
  loadModelOptions()
    .then((list) => (models.value = list.map((m) => m.value)))
    .catch(() => undefined)
})
watch(() => proxyBus.version, load)

const targets = computed(() => [
  { value: 'direct', label: '直连' },
  ...props.groups.map((g) => ({ value: g.name, label: `${g.name}（代理组）` })),
  ...props.proxies.map((p) => ({ value: p.name, label: p.name })),
])
const inheritTargets = computed(() => [{ value: '', label: '继承（不单独设置）' }, ...targets.value])
function targetOptions(current: string, withInherit = false) {
  const base = withInherit ? inheritTargets.value : targets.value
  return current && !base.some((t) => t.value === current) ? [{ value: current, label: `${current}（已不存在）` }, ...base] : base
}

async function patch(body: Record<string, unknown>, key: string, text = '分流规则已更新'): Promise<boolean> {
  if (!routing.value) return false
  saving.value = key
  try {
    routing.value = (await api.patch<Routing>('/proxy-routing', body, { ifMatch: routing.value.revision })).data
    toast(text, 'success')
    notifyProxiesChanged()
    return true
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    load()
    return false
  } finally {
    saving.value = ''
  }
}

const functionRows = computed(() => {
  const r = routing.value
  if (!r) return []
  return [
    { key: 'telegram', label: 'Telegram', section: 'functions', value: r.functions.telegram || '' },
    ...PROVIDER_ROUTES.map((p) => ({ key: p.key, label: p.label, section: 'providers', value: r.providers[p.key] || '' })),
    ...LEGACY_FUNCTIONS.filter((l) => r.functions[l.key]).map((l) => ({ key: l.key, label: l.label, section: 'functions', value: r.functions[l.key] || '' })),
  ]
})
function setFunction(section: string, key: string, value: string): void {
  patch({ [section]: { [key]: value || null } }, `${section}:${key}`)
}

const SECTIONS: { key: Section; title: string; sub: string; empty: string; pick: string }[] = [
  { key: 'accounts', title: '账号规则', sub: '为指定 OAuth 账号选择出口', empty: '还没有账号规则', pick: '选择账号' },
  { key: 'channels', title: '渠道规则', sub: '为指定 API 渠道选择出口', empty: '还没有渠道规则', pick: '选择渠道' },
  { key: 'models', title: '模型规则', sub: '按请求的模型选择出口', empty: '还没有模型规则', pick: '选择模型' },
]
function entries(sec: Section): [string, string][] {
  return Object.entries((routing.value?.[sec] || {}) as Record<string, string>)
}
function label(sec: Section, key: string): string {
  if (sec === 'accounts') return channelName(`oauth:${key}`)
  if (sec === 'channels') return channelName(key)
  return key
}
function open(sec: Section, key: string): void {
  if (sec === 'accounts') openAccount(key, channelName(`oauth:${key}`))
  else if (sec === 'channels') openChannel(key)
  else openModel(key)
}
function exists(sec: Section, key: string): boolean {
  if (sec === 'accounts') return Boolean(catalog.channels[`oauth:${key}`])
  if (sec === 'channels') return Boolean(catalog.channels[key])
  return !models.value.length || models.value.includes(key)
}
function candidates(sec: Section): { value: string; label: string }[] {
  const used = new Set(entries(sec).map(([k]) => k))
  if (sec === 'models') return models.value.filter((m) => !used.has(m)).map((m) => ({ value: m, label: m }))
  const type = sec === 'accounts' ? 'oauth' : 'api'
  return Object.entries(catalog.channels)
    .filter(([id, c]) => c.type === type && !used.has(sec === 'accounts' ? id.slice(6) : id))
    .map(([id, c]) => ({ value: sec === 'accounts' ? id.slice(6) : id, label: c.name }))
}
async function addRule(sec: Section): Promise<void> {
  const a = adding.value[sec]
  if (!a.key) return
  const ok = await patch({ [sec]: { [a.key]: a.target } }, `add:${sec}`, '已添加规则')
  if (ok) adding.value[sec] = { key: '', target: a.target }
}
</script>

<template>
  <p v-if="error" class="err"><TriangleAlert /> {{ error }} <Btn size="sm" @click="load">重试</Btn></p>
  <div v-else-if="!routing" class="cols"><Skeleton h="220px" r="14px" /><Skeleton h="220px" r="14px" /></div>
  <template v-else>
    <p class="prio">命中顺序：账号规则 = 渠道规则 &gt; 模型规则 &gt; 上游类型 &gt; 旧兼容规则 &gt; 默认路线</p>
    <div class="stack">
      <Card title="默认与兜底" subtitle="没有命中任何规则时的出口">
        <div class="rows grid2">
          <div class="row">
            <span class="r-label">默认路线</span>
            <Select
              :model-value="routing.default"
              :options="targetOptions(routing.default)"
              :disabled="saving === 'default'"
              width="200px"
              @update:model-value="patch({ default: $event }, 'default')"
            />
          </div>
          <div class="row">
            <span class="r-label">
              直连兜底
              <span class="r-sub">
                {{
                  routing.directFallback
                    ? '已配置的非直连路由异常时，会在代理链末尾尝试直连；可能暴露本机出口。'
                    : '已配置的非直连路由异常时保持失败，不会悄悄改走直连；没有配置网络规则时照常直连。'
                }}
              </span>
            </span>
            <Switch
              :model-value="routing.directFallback"
              :loading="saving === 'directFallback'"
              label="直连兜底"
              @update:model-value="patch({ directFallback: $event }, 'directFallback', $event ? '直连兜底已开启' : '直连兜底已关闭')"
            />
          </div>
        </div>
      </Card>
      <Card title="功能与上游类型" subtitle="按接入来源选择出口，登录、刷新、额度查询和模型请求共用；未设置时沿用旧兼容规则，最后走默认路线">
        <div class="rows grid2">
          <div v-for="f in functionRows" :key="f.section + f.key" class="row">
            <span class="r-label">{{ f.label }}</span>
            <Select
              :model-value="f.value"
              :options="targetOptions(f.value, true)"
              :disabled="saving === `${f.section}:${f.key}`"
              width="200px"
              @update:model-value="setFunction(f.section, f.key, $event)"
            />
          </div>
        </div>
      </Card>
    </div>
    <div class="cols three">
      <Card v-for="s in SECTIONS" :key="s.key" :title="s.title" :subtitle="s.sub">
        <template #actions><span class="count">{{ entries(s.key).length }} 条</span></template>
        <div class="rules">
          <p v-if="!entries(s.key).length" class="empty">{{ s.empty }}</p>
          <div v-for="[k, t] in entries(s.key)" :key="k" class="rule">
            <ProviderIcon v-if="s.key !== 'models'" :provider="s.key === 'accounts' ? channelProvider(`oauth:${k}`) : channelProvider(k)" :size="18" />
            <button type="button" class="r-key truncate" :class="{ mono: s.key === 'models', gone: !exists(s.key, k) }" :title="k" @click="open(s.key, k)">
              {{ label(s.key, k) }}<span v-if="!exists(s.key, k)" class="gone-tag">已不存在</span>
            </button>
            <button v-if="t !== 'direct'" type="button" class="to mono" :title="`查看 ${t}`" @click="openProxy(groups.some((g) => g.name === t) ? 'group' : 'proxy', t)">→</button>
            <span v-else class="to">→</span>
            <Select
              :model-value="t"
              :options="targetOptions(t)"
              :disabled="saving === `${s.key}:${k}`"
              width="140px"
              @update:model-value="patch({ [s.key]: { [k]: $event } }, `${s.key}:${k}`)"
            />
            <Btn icon size="sm" variant="ghost" title="删除规则" :loading="saving === `del:${s.key}:${k}`" @click="patch({ [s.key]: { [k]: null } }, `del:${s.key}:${k}`, '已删除规则')"><Trash2 /></Btn>
          </div>
        </div>
        <div class="add">
          <Select v-model="adding[s.key].key" :options="candidates(s.key)" :placeholder="s.pick" width="100%" />
          <div class="add-row">
            <Select v-model="adding[s.key].target" :options="targets" width="100%" />
            <Btn :disabled="!adding[s.key].key" :loading="saving === `add:${s.key}`" @click="addRule(s.key)"><Plus /> 添加</Btn>
          </div>
        </div>
      </Card>
    </div>
    <p class="faint foot">规则目标显示为 {{ targetLabel('direct') }} 时表示不走代理。</p>
  </template>
</template>

<style scoped>
.prio {
  font-size: 12px;
  color: var(--text-2);
}
.cols {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  align-items: stretch;
}
.cols.three {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.stack {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.rows.grid2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 32px;
}
.rows.grid2 .row:nth-last-child(2):nth-child(odd) {
  border-bottom: 0;
}
.cols > :deep(.card) {
  height: 100%;
}
.rows {
  display: flex;
  flex-direction: column;
}
.row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 42px;
  padding: 4px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.row:last-child {
  border-bottom: 0;
}
.r-label {
  display: flex;
  flex-direction: column;
  min-width: 0;
  font-size: 12.5px;
}
.r-sub {
  margin-top: 2px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-3);
}
.count {
  font-size: 11.5px;
  color: var(--text-3);
}
.rules {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 40px;
}
.rule {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 34px;
}
.r-key {
  flex: 1;
  min-width: 0;
  text-align: left;
  font-size: 12.5px;
}
.r-key:hover {
  color: var(--accent);
}
.r-key.gone {
  color: var(--text-3);
}
.gone-tag {
  margin-left: 6px;
  font-size: 10.5px;
  color: var(--orange);
}
.to {
  color: var(--text-3);
  font-size: 12px;
}
button.to:hover {
  color: var(--accent);
}
.empty {
  font-size: 12px;
  color: var(--text-3);
  padding: 8px 0;
}
.add {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 0.5px solid var(--hairline);
}
.add-row {
  display: flex;
  gap: 6px;
}
.add-row > :first-child {
  flex: 1;
}
.foot {
  font-size: 11.5px;
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
}
.err svg {
  width: 16px;
  height: 16px;
  color: var(--orange);
}
@container (max-width: 1100px) {
  .cols.three {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@container (max-width: 760px) {
  .rows.grid2 {
    grid-template-columns: minmax(0, 1fr);
  }
  .rows.grid2 .row:nth-last-child(2):nth-child(odd) {
    border-bottom: 0.5px solid var(--hairline);
  }
  .cols,
  .cols.three {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 560px) {
  .row {
    flex-wrap: wrap;
  }
  .row :deep(.ctl) {
    width: 100% !important;
  }
}
</style>
