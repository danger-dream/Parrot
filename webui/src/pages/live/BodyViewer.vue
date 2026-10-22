<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronRight, Copy, Search } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { toast } from '@/stores/ui'
import { compact } from '@/utils/format'
import { kindLabel, kindTone } from './status'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

type Item = Schemas['LogBodyItemData']

const props = defineProps<{ id: string; kind: 'request' | 'response' }>()

const items = ref<Item[]>([])
const total = ref(0)
const hasNext = ref(false)
const page = ref(1)
const loading = ref(false)
const error = ref('')
const kindCounts = ref<{ kind: string; count: number }[]>([])
const filter = ref<string>('')
const query = ref('')
const appliedQuery = ref('')
const order = ref<'original' | 'reverse'>('reverse')
const open = ref<Set<string>>(new Set())
const full = ref<Record<string, string>>({})

const PAGE = 30

async function load(reset = true): Promise<void> {
  if (reset) {
    page.value = 1
    items.value = []
  }
  loading.value = true
  error.value = ''
  try {
    const res = await api.get<Item[]>(`/logs/${enc(props.id)}/body`, {
      kind: props.kind,
      itemKind: filter.value || undefined,
      query: appliedQuery.value || undefined,
      sort: order.value,
      page: page.value,
      pageSize: PAGE,
    })
    items.value = reset ? res.data : [...items.value, ...res.data]
    total.value = res.meta.total ?? items.value.length
    hasNext.value = Boolean(res.meta.hasNext)
    const counts = (res.meta as { kindCounts?: { kind: string; count: number }[] }).kindCounts
    if (counts && !filter.value) kindCounts.value = counts
  } catch (err) {
    error.value = err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND' ? '这条请求没有保存正文。' : (err as Error).message
  } finally {
    loading.value = false
  }
}

function more(): void {
  page.value += 1
  load(false)
}

function key(item: Item): string {
  return `${item.seq}:${item.id ?? ''}`
}

function toggle(item: Item): void {
  const k = key(item)
  const next = new Set(open.value)
  if (next.has(k)) next.delete(k)
  else next.add(k)
  open.value = next
}

async function loadFull(item: Item): Promise<void> {
  if (!item.id) return
  try {
    const res = await api.get<Item>(`/logs/${enc(props.id)}/body/items/${enc(item.id)}`, { kind: props.kind })
    full.value = { ...full.value, [key(item)]: res.data.raw || res.data.text }
  } catch (err) {
    toast('加载完整内容失败', 'error', (err as Error).message)
  }
}

function textOf(item: Item): string {
  return full.value[key(item)] ?? item.text
}

function truncated(item: Item): boolean {
  return !full.value[key(item)] && item.size > item.text.length + 16
}

async function copy(item: Item): Promise<void> {
  try {
    await navigator.clipboard.writeText(textOf(item))
    toast('已复制', 'success')
  } catch {
    toast('复制失败', 'error')
  }
}

const chips = computed(() => [{ kind: '', count: kindCounts.value.reduce((a, b) => a + b.count, 0) }, ...kindCounts.value])

watch([filter, order], () => load())
watch(
  () => props.id,
  () => load(),
)
onMounted(() => load())

let searchTimer: number | undefined
watch(query, (q) => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    appliedQuery.value = q.trim()
    load()
  }, 320)
})
</script>

<template>
  <div class="viewer">
    <div class="tools">
      <div class="search">
        <Search />
        <input v-model="query" placeholder="在正文中搜索" />
      </div>
      <button class="order" @click="order = order === 'reverse' ? 'original' : 'reverse'">
        {{ order === 'reverse' ? '最新在前' : '最早在前' }}
      </button>
    </div>
    <div v-if="kindCounts.length > 1" class="chips">
      <button v-for="c in chips" :key="c.kind" class="chip" :class="{ on: filter === c.kind }" @click="filter = c.kind">
        {{ c.kind ? kindLabel(c.kind) : '全部' }}<span class="mono">{{ c.count }}</span>
      </button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <div v-else-if="loading && !items.length" class="sk">
      <Skeleton v-for="i in 4" :key="i" h="34px" r="8px" />
    </div>
    <p v-else-if="!items.length" class="none">没有内容</p>

    <div class="items">
      <div v-for="item in items" :key="key(item)" class="item" :class="{ open: open.has(key(item)) }">
        <button class="item-head" @click="toggle(item)">
          <ChevronRight class="chev" />
          <Badge :tone="kindTone(item.kind)">{{ kindLabel(item.kind) }}</Badge>
          <span class="summary truncate">{{ item.summary }}</span>
          <span class="size mono">{{ compact(item.size) }}</span>
        </button>
        <div v-if="open.has(key(item))" class="item-body">
          <pre>{{ textOf(item) }}</pre>
          <div class="item-actions">
            <Btn v-if="truncated(item)" size="sm" variant="ghost" @click="loadFull(item)">显示完整内容</Btn>
            <Btn size="sm" variant="ghost" @click="copy(item)"><Copy /> 复制</Btn>
          </div>
        </div>
      </div>
    </div>
    <div v-if="hasNext" class="more">
      <Btn size="sm" :loading="loading" @click="more">加载更多（共 {{ total }} 条）</Btn>
    </div>
  </div>
</template>

<style scoped>
.viewer {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tools {
  display: flex;
  gap: 8px;
}
.search {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 9px;
  border-radius: var(--radius-sm);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.search svg {
  width: 13px;
  height: 13px;
  color: var(--text-3);
}
.search input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: none;
  font-size: 12.5px;
}
.order {
  height: 28px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--text-2);
  background: var(--fill-3);
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
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  font-size: 11.5px;
  color: var(--text-2);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.chip span {
  font-size: 10.5px;
  color: var(--text-3);
}
.chip.on {
  background: var(--accent-soft);
  color: var(--accent);
  box-shadow: inset 0 0 0 0.5px color-mix(in srgb, var(--accent) 40%, transparent);
}
.items {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.item {
  border-radius: var(--radius-sm);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.item.open {
  background: var(--surface-sunken);
}
.item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 34px;
  padding: 0 10px 0 6px;
  text-align: left;
}
.chev {
  width: 14px;
  height: 14px;
  color: var(--text-3);
  flex-shrink: 0;
  transition: transform 0.2s var(--ease-out);
}
.open .chev {
  transform: rotate(90deg);
}
.summary {
  flex: 1;
  font-size: 12.5px;
  color: var(--text-2);
}
.size {
  font-size: 10.5px;
  color: var(--text-3);
}
.item-body {
  padding: 0 10px 10px;
}
pre {
  margin: 0;
  max-height: 420px;
  overflow: auto;
  padding: 10px;
  border-radius: 6px;
  background: var(--bg);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}
.item-actions {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  margin-top: 6px;
}
.err,
.none {
  padding: 18px 0;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-3);
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.more {
  display: flex;
  justify-content: center;
  padding: 4px 0 8px;
}
</style>
