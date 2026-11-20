<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronDown, ChevronRight, FlaskConical, History, Pencil, Plus, RefreshCw } from '@lucide/vue'
import { api } from '@/api/client'
import { errorTitle } from '@/utils/errors'
import { dateTime, relative } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { providerLabel, statusLabel, type EditorSample, type RecentData, type RecentGroup } from './model'

defineProps<{ channelLabel: (id: string) => string }>()
const emit = defineEmits<{
  test: [sample: EditorSample]
  create: [group: RecentGroup, sample: EditorSample]
  edit: [ruleId: string]
}>()

const HOURS = [
  { value: '24', label: '近 24 小时' },
  { value: '168', label: '近 7 天' },
]
const hours = ref('24')
const view = ref<'open' | 'all'>('open')
const data = ref<RecentData | null>(null)
const loading = ref(false)
const error = ref('')
const open = ref<string | null>(null)
const showAll = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    data.value = (await api.get<RecentData>('/error-rules/recent', { hours: Number(hours.value) })).data
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}

watch(hours, () => {
  showAll.value = false
  void load()
})
watch(view, () => {
  showAll.value = false
})
onMounted(load)
defineExpose({ load })

// "Covered" means a rule handles this kind of error now, whatever handled its past occurrences.
const openGroups = computed(() => (data.value?.groups || []).filter((g) => !g.currentRule))
const coveredCount = computed(() => (data.value?.groups.length || 0) - openGroups.value.length)
const shown = computed(() => (view.value === 'open' ? openGroups.value : data.value?.groups || []))
const viewOptions = computed(() => [
  { value: 'open' as const, label: `还没有规则的 ${openGroups.value.length}` },
  { value: 'all' as const, label: `全部 ${data.value?.groups.length || 0}` },
])

function sampleOf(g: RecentGroup): EditorSample {
  return {
    provider: g.provider,
    channelKey: g.channels[0] || '',
    model: g.models[0] || '',
    status: g.status ?? null,
    phase: g.phase,
    body: g.sample.replace(/^HTTP \d{3}:\s*/, ''),
  }
}

function ruleText(layer: string | null | undefined, name: string | null | undefined): string {
  return `${layer === 'preset' ? '预置' : '我的'} · ${name || ''}`
}

/** The latest occurrence was handled differently from how it would be now (usually: before the rule existed). */
function handledEarlier(g: RecentGroup): boolean {
  return (g.handledBy?.ruleId || null) !== (g.currentRule?.id || null)
}

function lastHandler(g: RecentGroup): string {
  return g.handledBy ? `「${g.handledBy.ruleName}」` : '默认策略'
}
</script>

<template>
  <Card title="最近的上游错误" subtitle="相同提供商、状态码、错误码和内容的失败归为一类。已经有规则处理的会标出来；还没有规则的，可以直接为它创建。">
    <template #icon><History /></template>
    <template #actions>
      <Segmented v-model="hours" :options="HOURS" size="sm" />
      <Btn icon size="sm" variant="ghost" title="刷新" :loading="loading" @click="load"><RefreshCw /></Btn>
    </template>

    <p v-if="error" class="err">{{ error }}</p>
    <div v-else-if="!data" class="sk"><Skeleton h="44px" r="10px" /><Skeleton h="44px" r="10px" /></div>
    <Empty v-else-if="!data.groups.length" :title="`${hours === '24' ? '近 24 小时' : '近 7 天'}没有上游错误`" detail="出现失败后会在这里归类显示。" />
    <template v-else>
      <div class="bar">
        <Segmented v-model="view" :options="viewOptions" size="sm" />
        <p class="total">
          共 {{ data.total }} 次失败、{{ data.groups.length }} 类，其中 {{ coveredCount }} 类已有规则处理{{ data.truncated ? '（只统计了最近 3000 次）' : '' }}
        </p>
      </div>
      <div v-if="!shown.length" class="done">
        <p>最近的上游错误都已经有规则处理了。</p>
        <button type="button" class="link" @click="view = 'all'">查看全部 {{ data.groups.length }} 类</button>
      </div>
      <ul v-else class="list">
        <li v-for="g in showAll ? shown : shown.slice(0, 8)" :key="g.id" class="item" :class="{ open: open === g.id, covered: !!g.currentRule }">
          <button type="button" class="line" :aria-expanded="open === g.id" @click="open = open === g.id ? null : g.id">
            <component :is="open === g.id ? ChevronDown : ChevronRight" class="chev" />
            <span class="who" :title="g.channels.map(channelLabel).join('、')">
              <ProviderIcon :provider="g.provider" :size="18" />
              <span class="prov">{{ g.provider ? providerLabel(g.provider) : g.channels.length ? channelLabel(g.channels[0]) : '未知渠道' }}</span>
            </span>
            <span class="tags">
              <Badge :tone="g.status && g.status >= 500 ? 'red' : g.status ? 'orange' : 'neutral'" mono>{{ statusLabel(g.status, g.outcome) }}</Badge>
              <Badge v-for="c in g.codes.slice(0, 2)" :key="c" tone="neutral" mono>{{ c }}</Badge>
            </span>
            <span class="msg" :title="g.message">{{ g.message || '（没有错误内容）' }}</span>
            <span class="count">
              <span class="n"><b>{{ g.count }}</b> 次</span>
              <small>{{ relative(g.lastAt) }}</small>
            </span>
            <span class="owner">
              <template v-if="g.currentRule">
                <Badge :tone="g.currentRule.layer === 'preset' ? 'purple' : 'green'" :title="`现在由${g.currentRule.layer === 'preset' ? '预置规则' : '我的规则'}「${g.currentRule.name}」处理`">
                  <span class="ellip">✓ {{ ruleText(g.currentRule.layer, g.currentRule.name) }}</span>
                </Badge>
                <small v-if="handledEarlier(g)" class="was" :title="`最近一次（${dateTime(g.lastAt)}）由${lastHandler(g)}处理，通常是这条规则创建之前发生的`">
                  最近一次：{{ g.handledBy ? ruleText(g.handledBy.layer, g.handledBy.ruleName) : '默认处理' }}
                </small>
              </template>
              <Badge v-else tone="neutral" title="没有规则命中，按内置处理和默认策略">还没有规则 · 默认处理</Badge>
            </span>
          </button>
          <div class="ops">
            <Btn icon size="sm" variant="ghost" title="试一试" @click="emit('test', sampleOf(g))"><FlaskConical /></Btn>
            <Btn v-if="g.currentRule?.layer === 'user'" size="sm" @click="emit('edit', g.currentRule.id)"><Pencil /> 编辑规则</Btn>
            <Btn v-else size="sm" :title="g.currentRule ? '新建一条我的规则，它会排在预置规则前面' : ''" @click="emit('create', g, sampleOf(g))"><Plus /> 创建规则</Btn>
          </div>

          <div v-if="open === g.id" class="detail">
            <p v-if="g.currentRule && handledEarlier(g)" class="now">
              现在由「{{ g.currentRule.name }}」处理。最近一次（{{ dateTime(g.lastAt) }}）是{{ lastHandler(g) }}处理的，通常是这条规则创建之前发生的；之后再出现会按这条规则处理。
            </p>
            <p v-else-if="!g.currentRule && g.handledBy" class="now">
              当时由「{{ g.handledBy.ruleName }}」处理，这条规则现在已经停用、删除或改了条件；再出现会交给内置处理和默认策略。
            </p>
            <dl class="kv">
              <div><dt>时间</dt><dd>{{ dateTime(g.firstAt) }} — {{ dateTime(g.lastAt) }}</dd></div>
              <div v-if="g.channels.length"><dt>渠道</dt><dd>{{ g.channels.map(channelLabel).join('、') }}</dd></div>
              <div v-if="g.models.length"><dt>模型</dt><dd class="mono">{{ g.models.join('、') }}</dd></div>
            </dl>
            <pre class="sample mono">{{ g.sample }}</pre>
          </div>
        </li>
      </ul>
      <button v-if="shown.length > 8" type="button" class="all" @click="showAll = !showAll">
        {{ showAll ? '收起' : `显示全部 ${shown.length} 类` }}
      </button>
    </template>
  </Card>
</template>

<style scoped>
.sk {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.err {
  color: var(--red);
  font-size: 13px;
}
.bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  margin-bottom: 8px;
}
.total {
  font-size: 12px;
  color: var(--text-3);
}
.done {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-radius: 12px;
  border: 0.5px dashed var(--border);
  font-size: 12.5px;
  color: var(--text-2);
}
.link {
  color: var(--accent, var(--blue));
  cursor: pointer;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.item {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  border-top: 0.5px solid var(--hairline);
}
.item:first-child {
  border-top: 0;
}
.line {
  display: grid;
  grid-template-columns: 14px 116px auto minmax(0, 1fr) 76px 188px;
  align-items: center;
  gap: 10px;
  padding: 9px 4px;
  text-align: left;
  cursor: pointer;
  min-width: 0;
  border-radius: 8px;
}
.line:hover {
  background: var(--fill-3, var(--fill-2));
}
.chev {
  width: 14px;
  height: 14px;
  color: var(--text-3);
}
.who {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.prov {
  font-size: 12.5px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tags {
  display: flex;
  gap: 4px;
}
.msg {
  font-size: 12.5px;
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.count {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  font-size: 12px;
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
  line-height: 1.3;
}
.count .n {
  white-space: nowrap;
}
.count b {
  font-size: 13px;
  color: var(--text);
}
.count small {
  font-size: 11px;
  color: var(--text-3);
}
.owner {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 0;
}
.was {
  max-width: 100%;
  font-size: 11px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item.covered .msg {
  color: var(--text-3);
}
.owner :deep(.badge) {
  max-width: 100%;
  min-width: 0;
}
.ellip {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ops {
  display: flex;
  align-items: center;
  gap: 4px;
  padding-left: 8px;
}
.detail {
  grid-column: 1 / -1;
  margin: 0 4px 12px 28px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.now {
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-2);
}
.kv {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 18px;
  margin: 0;
  font-size: 12px;
}
.kv div {
  display: flex;
  gap: 6px;
}
.kv dt {
  color: var(--text-3);
}
.kv dd {
  margin: 0;
  color: var(--text-2);
}
.sample {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--surface-sunken, var(--fill-2));
  border: 0.5px solid var(--hairline);
  font-size: 11.5px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow: auto;
}
.all {
  margin-top: 8px;
  font-size: 12.5px;
  color: var(--accent, var(--blue));
  cursor: pointer;
}
@media (max-width: 900px) {
  .item {
    grid-template-columns: minmax(0, 1fr);
  }
  .line {
    grid-template-columns: 14px minmax(0, 1fr) auto;
    row-gap: 6px;
    padding-bottom: 4px;
  }
  .tags {
    grid-column: 2;
  }
  .msg {
    grid-column: 2 / -1;
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .count {
    grid-row: 1;
    grid-column: 3;
    flex-direction: row;
    align-items: baseline;
    gap: 6px;
  }
  .owner {
    grid-column: 2 / -1;
  }
  .ops {
    justify-content: flex-end;
    padding: 0 4px 10px 28px;
  }
  .detail {
    margin-left: 4px;
  }
}
</style>
