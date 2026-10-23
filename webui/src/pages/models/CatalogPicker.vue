<script setup lang="ts">
import { ref, watch } from 'vue'
import { ChevronLeft, ChevronRight } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { errorTitle } from '@/utils/errors'
import Modal from '@/components/ui/Modal.vue'
import SearchField from '@/components/ui/SearchField.vue'
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

async function load(): Promise<void> {
  const my = ++seq
  loading.value = true
  try {
    const res = await api.get<CatalogData[]>('/model-catalog', { query: q.value.trim() || undefined, page: page.value, pageSize: PAGE })
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
    page.value = 1
    load()
  },
)
watch(q, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => {
    page.value = 1
    load()
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
      <SearchField v-model="q" placeholder="输入模型名或提供商，例如 claude opus" />
      <p v-if="error" class="err">{{ error }}</p>
      <div class="list" :class="{ dim: loading && items.length }">
        <template v-if="loading && !items.length">
          <Skeleton v-for="i in 6" :key="i" h="48px" r="8px" />
        </template>
        <button v-for="c in items" :key="c.key" type="button" class="opt" :class="{ on: c.key === currentKey }" @click="emit('pick', c)">
          <span class="main">
            <span class="name">{{ c.name }} <Badge v-if="c.key === currentKey" tone="blue">当前</Badge></span>
            <span class="mono faint small truncate">{{ c.key }}</span>
          </span>
          <span class="meta">
            <span class="prov">{{ c.providerName }}</span>
            <span class="faint small">
              <template v-if="c.metadata.contextWindow">上下文 {{ tokens(c.metadata.contextWindow) }}</template>
              <template v-if="priceSummary(c.metadata)"> · {{ priceSummary(c.metadata) }}</template>
            </span>
            <span class="tags">
              <Badge v-for="t in capabilityTags(c.metadata)" :key="t.key">{{ t.label }}</Badge>
            </span>
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
.opt {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-height: 48px;
  padding: 6px 10px;
  border-radius: 8px;
  text-align: left;
}
.opt:hover {
  background: var(--fill-3);
}
.opt.on {
  background: var(--accent-soft);
}
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 550;
}
.meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
  max-width: 45%;
  text-align: right;
}
.prov {
  font-size: 12px;
  color: var(--text-2);
}
.tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 3px;
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
