<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  ArrowRight,
  Cog,
  Download,
  ExternalLink,
  FlaskConical,
  Layers,
  ListChecks,
  Lock,
  Plus,
  RefreshCw,
  Route,
  Sparkles,
  Trash2,
  TriangleAlert,
  Upload,
} from '@lucide/vue'
import { api, ApiError, enc } from '@/api/client'
import { catalog, channelName, refreshCatalog } from '@/stores/catalog'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { channelProvider, providerTile } from '@/utils/providers'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Page from '@/components/ui/Page.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import RecentErrors from './RecentErrors.vue'
import RuleCard from './RuleCard.vue'
import RuleEditor from './RuleEditor.vue'
import RuleTester from './RuleTester.vue'
import { storeRuleNames, takeHandOff } from './ruleNames'
import {
  providerLabel,
  ruleFingerprint,
  rulePayload,
  seedFromBody,
  seedFromError,
  type EditorSample,
  type EditorSeed,
  type ErrorRule,
  type ErrorRulesData,
  type InvalidRule,
  type RecentGroup,
} from './model'

const data = ref<ErrorRulesData | null>(null)
const loading = ref(true)
const error = ref('')
const busy = ref('')
const recent = ref<InstanceType<typeof RecentErrors> | null>(null)

const editorOpen = ref(false)
const seed = ref<EditorSeed | null>(null)
const testerOpen = ref(false)
const testerSample = ref<EditorSample | null>(null)

const route = useRoute()
const router = useRouter()

async function load(): Promise<void> {
  loading.value = true
  try {
    data.value = (await api.get<ErrorRulesData>('/error-rules')).data
    storeRuleNames(data.value)
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  refreshCatalog().catch(() => undefined)
  await load()
  if (route.query.edit) {
    const id = String(route.query.edit)
    void router.replace({ query: {} })
    editById(id)
  }
  if (route.query.create) {
    void router.replace({ query: {} })
    const sample = takeHandOff()
    if (sample && data.value) {
      sample.provider = data.value.channelProviders[sample.channelKey] || sample.provider
      seed.value = seedFromBody(sample, '根据调用日志里的这次失败创建。条件已按这段错误填好，检查一下再保存。')
      editorOpen.value = true
    }
  }
})

function channelLabel(id: string): string {
  const c = catalog.channels[id]
  const name = c?.name || channelName(id)
  if ((c?.type || (id.startsWith('oauth:') ? 'oauth' : 'api')) === 'oauth') return `${providerTile(channelProvider(id)).label} · ${name}`
  return name
}

const channelOptions = computed(() =>
  Object.entries(catalog.channels)
    .map(([id, c]) => ({
      value: id,
      label: channelLabel(id),
      hint: c.type === 'oauth' ? 'OAuth 账号' : `API · ${providerLabel(data.value?.channelProviders[id] || '') || c.protocol}`,
    }))
    .sort((a, b) => a.label.localeCompare(b.label)),
)

const userEnabled = computed(() => (data.value?.rules || []).filter((r) => r.enabled).length)
const presetEnabled = computed(() => (data.value?.presets || []).filter((r) => r.enabled).length)

const presetGroups = computed(() => {
  const groups: { name: string; provider: string; rules: ErrorRule[] }[] = []
  for (const p of data.value?.presets || []) {
    let g = groups.find((x) => x.name === (p.group || '其他'))
    if (!g) {
      g = { name: p.group || '其他', provider: presetProvider(p), rules: [] }
      groups.push(g)
    }
    g.rules.push(p)
  }
  return groups
})

function presetProvider(p: ErrorRule): string {
  const c = p.when.items.find((i) => i.kind === 'cond' && i.field === 'provider')
  return c && c.kind === 'cond' ? c.values?.[0] || '' : ''
}

function hits(id: string) {
  return data.value?.stats[id] || null
}

async function mutate(key: string, fn: () => Promise<ErrorRulesData>, ok: string): Promise<boolean> {
  busy.value = key
  try {
    data.value = await fn()
    toast(ok, 'success')
    return true
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') await load()
    return false
  } finally {
    busy.value = ''
  }
}

const rev = () => ({ ifMatch: data.value?.revision })

function toggleAll(next: boolean): Promise<boolean> {
  return mutate(
    'global',
    async () => (await api.patch<ErrorRulesData>('/error-rules/settings', { enabled: next }, rev())).data,
    next ? '已开启错误处理规则' : '已关闭错误处理规则，所有错误改按内置处理和默认策略处理',
  )
}

function toggleRule(r: ErrorRule, next: boolean): Promise<boolean> {
  return mutate(
    `toggle:${r.id}`,
    async () => (await api.patch<ErrorRulesData>(`/error-rules/rules/${enc(r.id)}`, { ...rulePayload(r), enabled: next }, rev())).data,
    next ? `已启用「${r.name}」` : `已停用「${r.name}」`,
  )
}

function togglePreset(r: ErrorRule, next: boolean): Promise<boolean> {
  return mutate(
    `toggle:${r.id}`,
    async () => (await api.patch<ErrorRulesData>(`/error-rules/presets/${enc(r.id)}`, { enabled: next }, rev())).data,
    next ? `已启用预置规则「${r.name}」` : `已停用预置规则「${r.name}」`,
  )
}

async function removeRule(r: ErrorRule): Promise<void> {
  const ok = await confirmAction({
    title: `删除规则「${r.name}」？`,
    detail: '删除后立即不再生效，这类错误会交给后面的规则处理。',
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  await mutate(`del:${r.id}`, async () => (await api.del<ErrorRulesData>(`/error-rules/rules/${enc(r.id)}`, rev())).data, '规则已删除')
}

async function removeInvalid(r: InvalidRule): Promise<void> {
  const ok = await confirmAction({
    title: `删除格式有误的规则「${r.name || r.id || `第 ${r.index + 1} 条`}」？`,
    detail: '它现在不生效；删除后会从 config.json 里移除，其它规则不受影响。想保留的话，先在 config.json 里改正。',
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  await mutate(`del-invalid:${r.key}`, async () => (await api.del<ErrorRulesData>(`/error-rules/invalid-rules/${enc(r.key)}`, rev())).data, '已删除')
}

function moveRule(index: number, delta: -1 | 1): Promise<boolean> {
  const ids = (data.value?.rules || []).map((r) => r.id)
  const [moved] = ids.splice(index, 1)
  ids.splice(index + delta, 0, moved)
  return mutate(`move:${moved}`, async () => (await api.put<ErrorRulesData>('/error-rules/order', { ruleIds: ids }, rev())).data, '顺序已更新')
}

function openCreate(): void {
  seed.value = { rule: { category: 'rate_limit' } }
  editorOpen.value = true
}

function openEdit(r: ErrorRule): void {
  seed.value = { rule: r, editingId: r.id }
  editorOpen.value = true
}

function editById(id: string): void {
  const r = data.value?.rules.find((x) => x.id === id)
  if (r) openEdit(r)
}

function openDuplicate(r: ErrorRule, fromPreset: boolean): void {
  seed.value = {
    rule: { ...r, name: fromPreset ? r.name : `${r.name}（副本）`, enabled: true, note: fromPreset ? '' : r.note },
    origin: fromPreset
      ? `复制自预置规则「${r.name}」。保存后排在我的规则里，比预置规则先匹配，原预置规则不用停用。`
      : `复制自「${r.name}」。`,
  }
  editorOpen.value = true
}

function openFromRecent(g: RecentGroup, sample: EditorSample): void {
  seed.value = seedFromError(sample, g.codes, g.message, `根据最近的错误创建（${g.count} 次）。条件已按这类错误填好，检查一下再保存。`)
  editorOpen.value = true
}

function openFromTester(sample: EditorSample): void {
  testerOpen.value = false
  seed.value = seedFromBody(sample, '根据试一试里的错误创建。条件已按这段错误填好，检查一下再保存。')
  editorOpen.value = true
}

function openTester(sample: EditorSample | null = null): void {
  testerSample.value = sample
  testerOpen.value = true
}

async function saveRule(payload: Record<string, unknown>, id: string | null): Promise<void> {
  const ok = id
    ? await mutate('save', async () => (await api.patch<ErrorRulesData>(`/error-rules/rules/${enc(id)}`, payload, rev())).data, '规则已保存，立即生效')
    : await mutate('save', async () => (await api.post<ErrorRulesData>('/error-rules/rules', payload, rev())).data, '规则已创建，立即生效')
  if (ok) {
    editorOpen.value = false
    void recent.value?.load()
  }
}

// ─── import / export ─────────────────────────────────────────────────

const fileInput = ref<HTMLInputElement | null>(null)
const RULE_FIELDS = ['name', 'enabled', 'note', 'when', 'category', 'steps', 'final', 'thresholds', 'response'] as const

function exportRules(): void {
  const rules = (data.value?.rules || []).map((r) => rulePayload(r))
  const text = JSON.stringify({ format: 'parrot.errorRules', version: 1, exportedAt: new Date().toISOString(), rules }, null, 2)
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `parrot-error-rules-${new Date().toISOString().slice(0, 10)}.json`
  a.click()
  URL.revokeObjectURL(url)
}

/** Accepts this page's export, a bare rule array, or a copied `errorRules` block from config.json. */
function rulesIn(parsed: unknown): Record<string, unknown>[] | null {
  const obj = parsed as Record<string, unknown> | null
  const list = Array.isArray(parsed) ? parsed : Array.isArray(obj?.rules) ? obj!.rules
    : Array.isArray((obj?.errorRules as Record<string, unknown> | undefined)?.rules) ? (obj!.errorRules as Record<string, unknown>).rules : null
  if (!Array.isArray(list)) return null
  return list.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
}

async function importRules(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !data.value) return
  let rules: Record<string, unknown>[] | null = null
  try {
    rules = rulesIn(JSON.parse(await file.text()))
  } catch {
    rules = null
  }
  if (!rules?.length) {
    toast('没有读到规则', 'warning', '请选择这里导出的 JSON 文件，或 config.json 里 errorRules 那一段。', 7000)
    return
  }
  const ok = await confirmAction({
    title: `导入 ${rules.length} 条规则？`,
    detail: '会追加到「我的规则」末尾，现有规则不受影响；和已有规则内容相同的会跳过。导入后按列表顺序匹配，可以再调整顺序。',
    confirmText: '导入',
  })
  if (!ok) return
  busy.value = 'import'
  let added = 0
  const failed: string[] = []
  const duplicates: string[] = []
  const known = new Set((data.value?.rules || []).map((r) => ruleFingerprint(r)))
  const create = async (payload: Record<string, unknown>) =>
    (data.value = (await api.post<ErrorRulesData>('/error-rules/rules', payload, rev())).data)
  try {
    for (const raw of rules) {
      const payload: Record<string, unknown> = {}
      for (const f of RULE_FIELDS) if (raw[f] !== undefined && raw[f] !== null) payload[f] = raw[f]
      const name = `「${String(raw.name || '未命名')}」`
      const fingerprint = ruleFingerprint(payload)
      if (known.has(fingerprint)) {
        duplicates.push(name)
        continue
      }
      try {
        try {
          await create(payload)
        } catch (err) {
          if (!(err instanceof ApiError && err.code === 'REVISION_CONFLICT')) throw err
          // Someone else saved meanwhile: take the current list and try this rule once more.
          await load()
          for (const r of data.value?.rules || []) known.add(ruleFingerprint(r))
          if (known.has(fingerprint)) {
            duplicates.push(name)
            continue
          }
          await create(payload)
        }
        known.add(fingerprint)
        added += 1
      } catch (err) {
        failed.push(`${name}：${errorDetail(err) || errorTitle(err)}`)
        if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') await load()
      }
    }
  } finally {
    busy.value = ''
  }
  const skippedNote = duplicates.length ? `${duplicates.length} 条和已有规则相同，已跳过：${duplicates.join('、')}` : ''
  if (failed.length) {
    toast(`导入了 ${added} 条，${failed.length} 条没导入`, 'warning', [...failed, skippedNote].filter(Boolean).join('\n'), 12000)
  } else if (duplicates.length) {
    toast(`导入了 ${added} 条规则${added ? '，立即生效' : ''}`, added ? 'success' : 'info', skippedNote, 9000)
  } else {
    toast(`导入了 ${added} 条规则，立即生效`, 'success')
  }
  void recent.value?.load()
}

function scrollTo(id: string): void {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
</script>

<template>
  <Page
    title="错误处理"
    subtitle="上游出错时 Parrot 该怎么做：要不要重试、换不换渠道、暂停多久、给客户端回什么。修改立即生效，无需重启。"
    width="wide"
  >
    <template #actions>
      <Btn icon title="刷新" :loading="loading && !!data" @click="load(); recent?.load()"><RefreshCw /></Btn>
      <Btn @click="openTester()"><FlaskConical /> 试一试</Btn>
      <Btn variant="primary" :disabled="!data" @click="openCreate"><Plus /> 新建规则</Btn>
    </template>

    <p v-if="error" class="err"><TriangleAlert /> {{ error }} <Btn size="sm" @click="load">重试</Btn></p>
    <div v-else-if="!data" class="sk"><Skeleton h="150px" r="16px" /><Skeleton h="220px" r="16px" /><Skeleton h="320px" r="16px" /></div>

    <template v-else>
      <Card>
        <div class="flow-head">
          <div>
            <h3 class="flow-title"><Route class="ft-ico" /> 处理顺序</h3>
            <p class="flow-sub">每次上游失败，从左往右找第一条命中的规则来处理。前面的优先，后面的兜底。</p>
          </div>
          <label class="master">
            <span>{{ data.enabled ? '规则已启用' : '规则已关闭' }}</span>
            <Switch :model-value="data.enabled" :loading="busy === 'global'" label="启用错误处理规则" @update:model-value="toggleAll" />
          </label>
        </div>
        <ol class="flow">
          <li :class="{ off: !data.enabled }">
            <button type="button" class="stage" @click="scrollTo('er-mine')">
              <span class="st-n">1</span>
              <span class="st-ico user"><ListChecks /></span>
              <span class="st-name">我的规则</span>
              <span class="st-count">{{ userEnabled }} 条启用<template v-if="data.rules.length > userEnabled"> / 共 {{ data.rules.length }} 条</template></span>
              <span class="st-desc">按列表顺序，自己定义</span>
            </button>
          </li>
          <li class="sep"><ArrowRight /></li>
          <li :class="{ off: !data.enabled }">
            <button type="button" class="stage" @click="scrollTo('er-presets')">
              <span class="st-n">2</span>
              <span class="st-ico preset"><Sparkles /></span>
              <span class="st-name">预置规则</span>
              <span class="st-count">{{ presetEnabled }} / {{ data.presets.length }} 条启用</span>
              <span class="st-desc">智谱、WorkBuddy、MiniMax 等已知错误</span>
            </button>
          </li>
          <li class="sep"><ArrowRight /></li>
          <li>
            <button type="button" class="stage" @click="scrollTo('er-builtin')">
              <span class="st-n">3</span>
              <span class="st-ico builtin"><Layers /></span>
              <span class="st-name">内置处理</span>
              <span class="st-count">{{ data.builtins.length - 1 }} 项</span>
              <span class="st-desc">OAuth 刷新、各家额度识别等</span>
            </button>
          </li>
          <li class="sep"><ArrowRight /></li>
          <li>
            <button type="button" class="stage" @click="scrollTo('er-builtin')">
              <span class="st-n">4</span>
              <span class="st-ico fallback"><Cog /></span>
              <span class="st-name">默认策略</span>
              <span class="st-count">换渠道 + 冷却阶梯</span>
              <span class="st-desc">都没处理的失败</span>
            </button>
          </li>
        </ol>
        <p v-if="!data.enabled" class="off-note">
          <TriangleAlert class="on-ico" /> 规则已关闭：我的规则和预置规则都不生效，所有错误按内置处理和默认策略处理。
        </p>
      </Card>

      <RecentErrors ref="recent" :channel-label="channelLabel" @test="openTester" @create="openFromRecent" @edit="editById" />

      <div id="er-mine" class="anchor">
        <Card title="我的规则" :subtitle="`排在最前面，按列表顺序匹配，第一条命中的生效。命中次数统计近 ${data.statsWindowDays} 天。`">
          <template #icon><ListChecks /></template>
          <template #actions>
            <input ref="fileInput" type="file" accept="application/json,.json" hidden @change="importRules" />
            <Btn size="sm" variant="ghost" :loading="busy === 'import'" :disabled="!!busy" title="从 JSON 文件导入规则" @click="fileInput?.click()"><Upload /> 导入</Btn>
            <Btn size="sm" variant="ghost" :disabled="!data.rules.length" title="把我的规则导出为 JSON 文件" @click="exportRules"><Download /> 导出</Btn>
            <Btn size="sm" variant="primary" @click="openCreate"><Plus /> 新建规则</Btn>
          </template>
          <div v-if="data.invalidRules?.length" class="invalid-rules">
            <p class="ir-title">
              <TriangleAlert class="on-ico" /> config.json 里有 {{ data.invalidRules?.length }} 条规则格式有误，运行时已跳过
            </p>
            <p class="ir-detail">保存其它规则时会原样保留它们。可以在 config.json 里改正，也可以在这里删除。</p>
            <ul class="ir-list">
              <li v-for="r in data.invalidRules" :key="r.key" class="ir-row">
                <div class="ir-text">
                  <span class="ir-name">{{ r.name || r.id || `第 ${r.index + 1} 条` }}</span>
                  <span class="ir-error">{{ r.error }}</span>
                </div>
                <Btn size="sm" variant="ghost" :loading="busy === `del-invalid:${r.key}`" :disabled="!!busy" @click="removeInvalid(r)">
                  <Trash2 /> 删除
                </Btn>
              </li>
            </ul>
          </div>
          <div v-if="!data.rules.length" class="empty-mine">
            <p class="em-title">还没有自己的规则</p>
            <p class="em-detail">现在所有错误都由预置规则、内置处理和默认策略处理。想换一种处理方式时：</p>
            <ul class="em-ways">
              <li>在上面「最近的上游错误」里，点某一类后面的「创建规则」，条件会自动填好；</li>
              <li>或者在下面的预置规则上点「复制为我的规则」，改成你想要的样子；</li>
              <li>也可以直接 <button type="button" class="link" @click="openCreate">新建一条</button>。</li>
            </ul>
          </div>
          <div v-else class="rules">
            <RuleCard
              v-for="(r, i) in data.rules"
              :key="r.id"
              :rule="r"
              :index="i"
              :total="data.rules.length"
              :categories="data.categories"
              :hits="hits(r.id)"
              :busy="busy"
              :inactive="!data.enabled"
              :channel-label="channelLabel"
              layer="user"
              @toggle="(v) => toggleRule(r, v)"
              @edit="openEdit(r)"
              @duplicate="openDuplicate(r, false)"
              @remove="removeRule(r)"
              @move="(d) => moveRule(i, d)"
            />
          </div>
        </Card>
      </div>

      <div id="er-presets" class="anchor">
        <Card title="预置规则" subtitle="Parrot 自带的已知错误处理，随版本更新。可以停用；想改的话复制成我的规则再改，我的规则会先匹配。">
          <template #icon><Sparkles /></template>
          <div class="groups">
            <section v-for="g in presetGroups" :key="g.name" class="group">
              <h4 class="g-title">
                <ProviderIcon :provider="g.provider" :size="18" />
                {{ g.name }}
                <span class="g-n">{{ g.rules.filter((r) => r.enabled).length }} / {{ g.rules.length }} 条启用</span>
              </h4>
              <div class="rules">
                <RuleCard
                  v-for="r in g.rules"
                  :key="r.id"
                  :rule="r"
                  :categories="data.categories"
                  :hits="hits(r.id)"
                  :busy="busy"
                  :inactive="!data.enabled"
                  :channel-label="channelLabel"
                  layer="preset"
                  @toggle="(v) => togglePreset(r, v)"
                  @duplicate="openDuplicate(r, true)"
                />
              </div>
            </section>
          </div>
        </Card>
      </div>

      <div id="er-builtin" class="anchor">
        <Card title="内置处理与默认策略" subtitle="写在程序里的协议级处理，这里只做说明。大多排在规则后面，规则命中的错误不会再走到这里；「OAuth 凭据自动刷新」「协议级自动修复」和 Antigravity 用积分重试例外，它们只是换个方式重发同一个请求，在规则之前执行，重发后仍失败才交给规则。">
          <template #icon><Lock /></template>
          <ul class="builtins">
            <li v-for="b in data.builtins" :key="b.id" :class="{ last: b.id === 'default-ladder' }">
              <div class="b-main">
                <p class="b-name">
                  <Cog v-if="b.id === 'default-ladder'" class="b-ico" />
                  <Layers v-else class="b-ico" />
                  {{ b.name }}
                </p>
                <p class="b-desc">{{ b.description }}</p>
              </div>
              <RouterLink v-if="b.link" :to="b.link" class="b-link">{{ b.setting }} <ExternalLink /></RouterLink>
              <span v-else class="b-link plain">{{ b.setting }}</span>
            </li>
          </ul>
        </Card>
      </div>
    </template>

    <RuleEditor
      v-if="data"
      :open="editorOpen"
      :data="data"
      :seed="seed"
      :saving="busy === 'save'"
      :channel-options="channelOptions"
      :channel-label="channelLabel"
      @close="editorOpen = false"
      @save="saveRule"
    />
    <RuleTester
      v-if="data"
      :open="testerOpen"
      :data="data"
      :sample="testerSample"
      :channel-options="channelOptions"
      @close="testerOpen = false"
      @create-from="openFromTester"
    />
  </Page>
</template>

<style scoped>
.sk {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.err {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--red);
  font-size: 13px;
}
.anchor {
  scroll-margin-top: calc(var(--topbar-h, 52px) + 12px);
}
.flow-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.flow-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.ft-ico {
  width: 16px;
  height: 16px;
  color: var(--text-2);
}
.flow-sub {
  margin-top: 3px;
  font-size: 12px;
  color: var(--text-3);
}
.master {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: var(--text-2);
}
.flow {
  list-style: none;
  margin: 16px 0 0;
  padding: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 24px minmax(0, 1fr) 24px minmax(0, 1fr) 24px minmax(0, 1fr);
  align-items: stretch;
}
.flow li.off {
  opacity: 0.45;
}
.sep {
  display: grid;
  place-items: center;
  color: var(--text-3);
}
.sep svg {
  width: 16px;
  height: 16px;
}
.stage {
  width: 100%;
  height: 100%;
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-rows: auto auto auto;
  column-gap: 10px;
  row-gap: 2px;
  padding: 12px 14px;
  border-radius: 14px;
  border: 0.5px solid var(--hairline);
  background: var(--fill-3, var(--fill-2));
  text-align: left;
  cursor: pointer;
  position: relative;
  transition: border-color 0.15s, background 0.15s, transform 0.15s var(--ease-out);
}
.stage:hover {
  border-color: var(--border);
  background: var(--fill-2);
  transform: translateY(-1px);
}
.st-n {
  position: absolute;
  top: 10px;
  right: 12px;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.st-ico {
  grid-row: 1 / 4;
  width: 32px;
  height: 32px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  color: #fff;
}
.st-ico svg {
  width: 17px;
  height: 17px;
}
.st-ico.user {
  background: var(--blue);
}
.st-ico.preset {
  background: var(--purple);
}
.st-ico.builtin {
  background: var(--teal);
}
.st-ico.fallback {
  background: var(--text-3);
}
.st-name {
  font-size: 13.5px;
  font-weight: 650;
}
.st-count {
  font-size: 12px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.st-desc {
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.4;
}
.off-note {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-orange, var(--orange));
}
.on-ico {
  width: 14px;
  height: 14px;
}
.rules {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.invalid-rules {
  margin-bottom: 12px;
  padding: 12px 14px;
  border-radius: 12px;
  border: 0.5px solid var(--orange);
  font-size: 12.5px;
}
.ir-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--text-orange, var(--orange));
}
.ir-detail {
  margin-top: 4px;
  color: var(--text-3);
}
.ir-list {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ir-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.ir-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.ir-name {
  font-weight: 500;
}
.ir-error {
  color: var(--text-3);
  overflow-wrap: anywhere;
}
.empty-mine {
  padding: 18px 20px;
  border-radius: 12px;
  border: 0.5px dashed var(--border);
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.6;
}
.em-title {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--text);
}
.em-detail {
  margin-top: 4px;
}
.em-ways {
  margin: 4px 0 0;
  padding-left: 18px;
}
.link {
  color: var(--accent, var(--blue));
  cursor: pointer;
}
.groups {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.g-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 650;
}
.g-n {
  font-size: 11.5px;
  font-weight: 400;
  color: var(--text-3);
}
.builtins {
  list-style: none;
  margin: 0;
  padding: 0;
}
.builtins li {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 0;
  border-top: 0.5px solid var(--hairline);
}
.builtins li:first-child {
  border-top: 0;
  padding-top: 0;
}
.builtins li.last {
  margin-top: 4px;
  padding-top: 12px;
  border-top: 0.5px dashed var(--border);
}
.b-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}
.b-ico {
  width: 14px;
  height: 14px;
  color: var(--text-3);
}
.b-desc {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-2);
  line-height: 1.55;
}
.b-link {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--accent, var(--blue));
  white-space: nowrap;
}
.b-link svg {
  width: 12px;
  height: 12px;
}
.b-link.plain {
  color: var(--text-3);
}
@media (max-width: 1000px) {
  .flow {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 8px;
  }
  .sep {
    display: none;
  }
}
@media (max-width: 560px) {
  .stage {
    padding: 10px 12px;
    column-gap: 8px;
  }
  .st-ico {
    grid-row: 1 / 3;
    width: 28px;
    height: 28px;
  }
  .st-desc {
    display: none;
  }
  .st-count {
    font-size: 11.5px;
  }
  .builtins li {
    flex-direction: column;
    gap: 6px;
  }
}
</style>
