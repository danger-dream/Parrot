<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowRight, GitMerge, Plus, RotateCcw, Trash2, TriangleAlert } from '@lucide/vue'
import { api, ApiError, type Schemas } from '@/api/client'
import { openChannel, openModel } from '@/links'
import { refreshCatalog } from '@/stores/catalog'
import { confirmAction, promptText } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Badge from '@/components/ui/Badge.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Empty from '@/components/ui/Empty.vue'
import RuleModal from './RuleModal.vue'
import { channelLabel, loadModels } from './shared'

type Registry = Schemas['ModelRegistryData']
type Rule = Schemas['RegistryRuleData']
type Pending = Schemas['RegistryPendingData']

const data = ref<Registry | null>(null)
const loading = ref(true)
const error = ref('')
const busy = ref('')
const ruleOpen = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    data.value = (await api.get<Registry>('/models/registry')).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  load()
  refreshCatalog()
})

function providerOfSource(source: string): string {
  return source.startsWith('oauth:') ? source.slice(6).split(':')[0] : source.startsWith('api:') ? 'api' : source
}

/** One decision per provider and upstream id, as in TG. */
const pendingGroups = computed(() => {
  const groups = new Map<string, Pending[]>()
  for (const p of data.value?.pending || []) {
    const key = `${providerOfSource(p.source)}|${p.upstreamModel}`
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(p)
  }
  return [...groups.entries()].map(([key, items]) => ({ key, items, first: items[0] }))
})

async function write(change: (rules: Rule[], dismissed: string[]) => void, done: string, key: string): Promise<boolean> {
  if (!data.value) return false
  const rules = data.value.rules.map((r) => ({ ...r }))
  const dismissed = [...data.value.dismissed]
  change(rules, dismissed)
  busy.value = key
  try {
    await api.put('/models/registry/rules', { rules, dismissed }, { ifMatch: data.value.revision })
    toast(done, 'success')
    await load()
    loadModels(true)
    return true
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      toast('命名规则已在别处被修改', 'warning', '已载入最新规则，请重新操作。', 7000)
      await load()
    } else toast(errorTitle(err), 'error', errorDetail(err), 7000)
    return false
  } finally {
    busy.value = ''
  }
}

function sameRule(a: Rule, b: Rule): boolean {
  return a.type === b.type && a.model === b.model && (a.into || null) === (b.into || null) && (a.source || null) === (b.source || null) && (a.upstreamModel || null) === (b.upstreamModel || null)
}

function assign(items: Pending[], model: string, key: string): void {
  const sources = new Set(items.map((i) => i.source))
  const wire = items[0].upstreamModel
  write(
    (rules) => {
      for (let i = rules.length - 1; i >= 0; i--) {
        const r = rules[i]
        if (r.type === 'assign' && sources.has(r.source || '') && r.upstreamModel === wire) rules.splice(i, 1)
      }
      for (const s of sources) rules.push({ type: 'assign', source: s, upstreamModel: wire, model })
    },
    `已归入 ${model}`,
    key,
  )
}

async function assignCustom(items: Pending[], key: string): Promise<void> {
  const name = await promptText({
    title: '归入哪个模型名？',
    detail: `为 ${items[0].upstreamModel} 指定模型名。填已有模型名即并入它，填新名字即单独成为一个模型。`,
    label: '模型名',
    mono: true,
    validate: (v) => (!v.trim() ? '' : v.trim().length > 100 || /\s/.test(v.trim()) ? '1—100 个字符，不能含空白' : null),
  })
  if (name) assign(items, name.trim(), key)
}

function merge(s: Schemas['RegistrySuggestionData']): void {
  write((rules) => rules.push({ type: 'merge', model: s.modelId, into: s.intoModelId, pin: s.pin || null }), `已把 ${s.modelId} 并入 ${s.intoModelId}`, `sg:${s.key}`)
}
function dismiss(s: Schemas['RegistrySuggestionData']): void {
  write((_r, d) => d.push(s.key), '已忽略这条建议', `dm:${s.key}`)
}
function undismiss(): void {
  write((_r, d) => d.splice(0), '已恢复忽略的建议', 'undismiss')
}

async function removeRule(rule: Rule, idx: number): Promise<void> {
  const ok = await confirmAction({
    title: '删除这条命名规则？',
    detail: '删除后按证据重新归并；合并时带过去的启停开关不会还原。',
    confirmText: '删除规则',
    danger: true,
  })
  if (!ok) return
  write((rules) => {
    const i = rules.findIndex((r) => sameRule(r, rule))
    if (i >= 0) rules.splice(i, 1)
  }, '规则已删除', `rm:${idx}`)
}

async function addRule(rule: Rule): Promise<void> {
  const ok = await write((rules) => rules.push(rule), '规则已添加', 'add')
  if (ok) ruleOpen.value = false
}
</script>

<template>
  <div class="reg">
    <p class="intro">
      统一命名把不同账户、渠道里其实是同一个的模型归到一个名字下：客户端用统一的名字调用，Parrot 会把请求分给所有来源。
      自动无法判断的会出现在「待确认」，你也可以手动写规则。
    </p>
    <p v-if="error" class="err"><TriangleAlert /> {{ error }}</p>
    <div v-else-if="loading && !data" class="grid"><Skeleton v-for="i in 3" :key="i" h="160px" r="14px" /></div>
    <template v-else-if="data">
      <p v-if="!data.enabled" class="err"><TriangleAlert /> 统一模型名未开启，规则暂不生效。</p>

      <div class="grid">
        <Card title="待确认" :subtitle="`${pendingGroups.length} 项 · 系统拿不准归到哪个名字`">
          <p v-if="!pendingGroups.length" class="faint small">没有待确认的模型。</p>
          <div v-for="g in pendingGroups" :key="g.key" class="item">
            <p class="line">
              <span class="mono">{{ g.first.upstreamModel }}</span>
              <span class="faint">暂名</span>
              <button class="lnk mono" @click="openModel(g.first.modelId)">{{ g.first.modelId }}</button>
            </p>
            <p class="sub faint">
              {{ g.first.name }} · 来自
              <template v-for="(it, i) in g.items" :key="it.source">
                <button class="lnk" @click="openChannel(it.source)">{{ channelLabel(it.source) }}</button><template v-if="i < g.items.length - 1">、</template>
              </template>
            </p>
            <p class="sub">{{ g.first.reason.replace(/`/g, '') }}</p>
            <div class="acts">
              <Btn v-for="o in g.first.options" :key="o" size="sm" :loading="busy === `${g.key}:${o}`" @click="assign(g.items, o, `${g.key}:${o}`)">归入 {{ o }}</Btn>
              <Btn size="sm" variant="ghost" :loading="busy === `${g.key}:keep`" @click="assign(g.items, g.first.modelId, `${g.key}:keep`)">保持 {{ g.first.modelId }}</Btn>
              <Btn size="sm" variant="ghost" :loading="busy === `${g.key}:custom`" @click="assignCustom(g.items, `${g.key}:custom`)">自定义名称</Btn>
            </div>
          </div>
        </Card>

        <Card title="合并建议" :subtitle="`${data.suggestions.length} 条 · 疑似同一个模型的不同名字`">
          <template v-if="data.dismissed.length" #actions>
            <Btn size="sm" variant="ghost" :loading="busy === 'undismiss'" @click="undismiss"><RotateCcw /> 恢复已忽略的 {{ data.dismissed.length }} 条</Btn>
          </template>
          <p v-if="!data.suggestions.length" class="faint small">没有新的合并建议。</p>
          <div v-for="s in data.suggestions" :key="s.key" class="item">
            <p class="line">
              <button class="lnk mono" @click="openModel(s.modelId)">{{ s.modelId }}</button>
              <ArrowRight class="arrow" />
              <button class="lnk mono" @click="openModel(s.intoModelId)">{{ s.intoModelId }}</button>
            </p>
            <p class="sub">{{ s.text.replace(/`/g, '') }}</p>
            <p class="sub faint">合并后 {{ s.modelId }} 的所有来源归入 {{ s.intoModelId }}，旧名字继续可以调用。</p>
            <div class="acts">
              <Btn size="sm" variant="primary" :loading="busy === `sg:${s.key}`" @click="merge(s)"><GitMerge /> 合并</Btn>
              <Btn size="sm" variant="ghost" :loading="busy === `dm:${s.key}`" @click="dismiss(s)">忽略</Btn>
            </div>
          </div>
        </Card>
      </div>

      <Card title="人工规则" :subtitle="`${data.rules.length} 条 · 优先于自动归并`" flush>
        <template #actions>
          <Btn size="sm" variant="primary" @click="ruleOpen = true"><Plus /> 新增规则</Btn>
        </template>
        <div class="legend">
          <span><Badge tone="purple">合并</Badge> 把一个模型名并入另一个，旧名字变成别名，仍可调用。</span>
          <span><Badge tone="teal">指定</Badge> 某个账户/渠道的某个上游模型，固定归到指定的模型名下。</span>
        </div>
        <Empty v-if="!data.rules.length" title="还没有人工规则" detail="自动归并不准确时，在这里新增规则。" />
        <div v-for="(r, i) in data.rules" :key="i" class="rule">
          <Badge :tone="r.type === 'merge' ? 'purple' : 'teal'">{{ r.type === 'merge' ? '合并' : '指定' }}</Badge>
          <p v-if="r.type === 'merge'" class="rule-t">
            <button class="lnk mono" @click="openModel(r.model)">{{ r.model }}</button>
            <span class="faint">并入</span>
            <button class="lnk mono" @click="openModel(r.into || '')">{{ r.into }}</button>
          </p>
          <p v-else class="rule-t">
            <button class="lnk" @click="openChannel(r.source)">{{ channelLabel(r.source) }}</button>
            <span class="faint">的</span>
            <span class="mono">{{ r.upstreamModel }}</span>
            <span class="faint">归入</span>
            <button class="lnk mono" @click="openModel(r.model)">{{ r.model }}</button>
          </p>
          <span v-if="r.pin" class="faint small pin">价格目录：{{ r.pin.catalog }}</span>
          <Btn icon size="sm" variant="ghost" title="删除规则" :loading="busy === `rm:${i}`" @click="removeRule(r, i)"><Trash2 /></Btn>
        </div>
      </Card>

      <Card v-if="data.conflicts.length" title="别名冲突" subtitle="这些名字同时指向多个模型，需要人工处理">
        <ul class="conf">
          <li v-for="c in data.conflicts" :key="c">{{ c.replace(/`/g, '') }}</li>
        </ul>
      </Card>
    </template>

    <RuleModal :open="ruleOpen" :saving="busy === 'add'" @close="ruleOpen = false" @save="addRule" />
  </div>
</template>

<style scoped>
.reg {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.intro {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.65;
  max-width: 860px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  align-items: stretch;
  gap: 16px;
}
.item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 0;
  border-bottom: 0.5px solid var(--hairline);
}
.item:first-of-type {
  padding-top: 0;
}
.item:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}
.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  word-break: break-all;
}
.arrow {
  width: 13px;
  height: 13px;
  color: var(--text-3);
}
.sub {
  font-size: 12px;
  color: var(--text-2);
  line-height: 1.55;
}
.sub.faint {
  color: var(--text-3);
}
.acts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}
.lnk {
  color: var(--accent);
  text-align: left;
  word-break: break-all;
}
.lnk:hover {
  text-decoration: underline;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
  padding: 0 16px 12px;
  font-size: 12px;
  color: var(--text-2);
  border-bottom: 0.5px solid var(--border);
}
.legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.rule {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 6px 10px 6px 16px;
  border-bottom: 0.5px solid var(--hairline);
}
.rule:last-child {
  border-bottom: 0;
}
.rule-t {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  font-size: 12.5px;
}
.pin {
  flex-shrink: 0;
}
.conf {
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.7;
}
.small {
  font-size: 12px;
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
@media (max-width: 767px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .pin {
    display: none;
  }
}
</style>
