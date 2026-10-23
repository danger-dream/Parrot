<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { errorTitle } from '@/utils/errors'
import Modal from '@/components/ui/Modal.vue'
import SearchField from '@/components/ui/SearchField.vue'
import Select from '@/components/ui/Select.vue'
import Btn from '@/components/ui/Btn.vue'
import Badge from '@/components/ui/Badge.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Empty from '@/components/ui/Empty.vue'
import { capabilityTags, priceSummary, tokens } from './shared'

// Search the models.dev catalog (searchModelCatalog) and pick an entry to bind.

type CatalogData = Schemas['CatalogData']

const props = defineProps<{ open: boolean; initialQuery?: string; currentKey?: string | null; subtitle?: string }>()
const emit = defineEmits<{ close: []; pick: [entry: CatalogData] }>()

const PAGE = 20
const q = ref('')
const page = ref(1)
const items = ref<CatalogData[]>([])
const total = ref(0)
const hasNext = ref(false)
const loading = ref(false)
const error = ref('')
let timer: number | undefined
let seq = 0
const provider = ref('')
const providers = ref<{ value: string; label: string }[]>([])
let facetSeq = 0

/** Providers among the first 200 matches of the text query, most entries first. */
async function loadProviders(): Promise<void> {
  const my = ++facetSeq
  try {
    const res = await api.get<CatalogData[]>('/model-catalog', { query: q.value.trim() || undefined, page: 1, pageSize: 200 })
    if (my !== facetSeq) return
    const count = new Map<string, { name: string; n: number }>()
    for (const c of res.data) {
      const e = count.get(c.providerId) || { name: c.providerName, n: 0 }
      e.n++
      count.set(c.providerId, e)
    }
    providers.value = [...count.entries()]
      .sort((a, b) => b[1].n - a[1].n || a[1].name.localeCompare(b[1].name))
      .map(([id, e]) => ({ value: id, label: `${e.name}（${e.n}）` }))
    if (provider.value && !count.has(provider.value)) provider.value = ''
  } catch {
    providers.value = []
  }
}
const providerOptions = computed(() => [{ value: '', label: '全部提供商' }, ...providers.value])
watch(provider, () => {
  page.value = 1
  load()
})

async function load(): Promise<void> {
  const my = ++seq
  loading.value = true
  try {
    const res = await api.get<CatalogData[]>('/model-catalog', { query: q.value.trim() || undefined, provider: provider.value || undefined, page: page.value, pageSize: PAGE })
    if (my !== seq) return
    items.value = res.data
    total.value = res.meta.total ?? res.data.length
    hasNext.value = Boolean(res.meta.hasNext)
    error.value = ''
  } catch (err) {
    if (my === seq) error.value = errorTitle(err)
  } finally {
    if (my === seq) loading.value = false
  }
}

watch(
  () => props.open,
  (o) => {
    if (!o) return
    q.value = props.initialQuery || ''
    provider.value = ''
    page.value = 1
    load()
    loadProviders()
  },
)
watch(q, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => {
    page.value = 1
    load()
    loadProviders()
  }, 250)
})
function go(d: number): void {
  page.value = Math.max(1, page.value + d)
  load()
}
</script>

<template>
  <Modal :open="open" title="从 models.dev 目录选择" :subtitle="subtitle" width="lg" @close="emit('close')">
    <div class="cat">
      <div class="filters">
        <SearchField v-model="q" placeholder="输入模型名，例如 claude opus" class="grow" />
        <Select v-model="provider" :options="providerOptions" width="220px" />
      </div>
      <p v-if="error" class="err">{{ error }}</p>
      <div class="list" :class="{ dim: loading && items.length }">
        <template v-if="loading && !items.length">
          <Skeleton v-for="i in 6" :key="i" h="48px" r="8px" />
        </template>
        <button v-for="c in items" :key="c.key" type="button" class="opt" :class="{ on: c.key === currentKey }" @click="emit('pick', c)">
          <span class="name">{{ c.name }} <Badge v-if="c.key === currentKey" tone="blue">当前</Badge></span>
          <span class="prov">{{ c.providerName }}</span>
          <span class="mono faint small truncate">{{ c.key }}</span>
          <span class="faint small r">
            <template v-if="c.metadata.contextWindow">上下文 {{ tokens(c.metadata.contextWindow) }}</template>
            <template v-if="priceSummary(c.metadata)"> · {{ priceSummary(c.metadata) }}</template>
          </span>
          <span v-if="capabilityTags(c.metadata).length" class="tags">
            <Badge v-for="t in capabilityTags(c.metadata)" :key="t.key">{{ t.label }}</Badge>
          </span>
        </button>
        <Empty v-if="!loading && !items.length && !error" title="没有匹配的目录条目" detail="换个关键词，或只输入型号的一部分" />
      </div>
      <div v-if="total" class="pager">
        <span class="faint small">共 {{ total }} 条 · 第 {{ page }} 页</span>
        <Btn icon size="sm" :disabled="page <= 1" @click="go(-1)"><ChevronLeft /></Btn>
        <Btn icon size="sm" :disabled="!hasNext" @click="go(1)"><ChevronRight /></Btn>
      </div>
    </div>
  </Modal>
</template>

<style scoped>
.cat {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: min(54dvh, 480px);
  overflow-y: auto;
  margin: 0 -6px;
  padding: 0 6px;
}
.dim {
  opacity: 0.55;
}
.filters {
  display: flex;
  gap: 8px;
}
.filters .grow {
  flex: 1;
  min-width: 0;
}
/* name · provider / catalog key · limits and price / capability tags */
.opt {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 2px 12px;
  flex-shrink: 0;
  width: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  text-align: left;
}
.opt:hover {
  background: var(--fill-3);
}
.opt.on {
  background: var(--accent-soft);
}
.name {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 13px;
  font-weight: 550;
}
.prov {
  font-size: 12px;
  color: var(--text-2);
  text-align: right;
}
.r {
  text-align: right;
  white-space: nowrap;
}
.tags {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
  margin-top: 2px;
}
.small {
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.err {
  color: var(--red);
  font-size: 12.5px;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 560px) {
  .opt {
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
  }
  .meta {
    align-items: flex-start;
    max-width: none;
    text-align: left;
  }
  .tags {
    justify-content: flex-start;
  }
}
</style>
