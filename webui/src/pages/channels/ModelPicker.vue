<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CloudDownload, Plus } from '@lucide/vue'
import { promptText } from '@/stores/dialogs'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { parseModelsInput, type DiscoveryResult, type ModelPair, type OpOutcome } from './meta'

const props = defineProps<{
  modelValue: ModelPair[]
  discover?: (() => Promise<OpOutcome<DiscoveryResult>>) | null
  staticModels?: string[]
  autoDiscover?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: ModelPair[]] }>()

const discovered = ref<string[]>([])
const extra = ref<string[]>([])
const discovering = ref(false)
const note = ref<{ tone: 'ok' | 'warn'; text: string } | null>(null)
const manual = ref('')
const manualError = ref('')

const aliasOf = computed(() => Object.fromEntries(props.modelValue.map((m) => [m.real, m.alias])))
const options = computed(() => {
  const seen = new Set<string>()
  const out: { value: string; hint?: string }[] = []
  const add = (v: string, hint?: string) => {
    if (!v || seen.has(v)) return
    seen.add(v)
    out.push({ value: v, hint })
  }
  for (const m of props.modelValue) add(m.real)
  for (const v of discovered.value) add(v)
  for (const v of props.staticModels || []) add(v, '内置参考')
  for (const v of extra.value) add(v, '手动添加')
  return out.map((o) => ({ ...o, hint: aliasOf.value[o.value] && aliasOf.value[o.value] !== o.value ? `别名：${aliasOf.value[o.value]}` : o.hint }))
})
const selected = computed(() => props.modelValue.map((m) => m.real))

function setSelected(reals: string[]): void {
  emit(
    'update:modelValue',
    reals.map((r) => ({ real: r, alias: aliasOf.value[r] || r })),
  )
}

async function runDiscover(): Promise<void> {
  if (!props.discover) return
  discovering.value = true
  note.value = null
  const out = await props.discover()
  discovering.value = false
  if (out.ok && out.result?.models?.length) {
    discovered.value = out.result.models
    note.value = { tone: 'ok', text: `已从上游获取 ${out.result.models.length} 个模型` }
  } else {
    const reason = out.error || out.result?.error || '上游未返回可用模型'
    note.value = {
      tone: 'warn',
      text: props.staticModels?.length
        ? `实时模型列表获取失败（${reason}）。当前显示 ${props.staticModels.length} 个内置参考模型，可能不是最新版本，也可以手动添加。`
        : `自动获取模型失败（${reason}），请手动添加模型。`,
    }
  }
}

function addManual(): void {
  manualError.value = ''
  const items = parseModelsInput(manual.value)
  if (!items.length) {
    manualError.value = '请输入模型名'
    return
  }
  const next = [...props.modelValue]
  for (const it of items) {
    const at = next.findIndex((m) => m.real === it.real)
    if (at >= 0) next[at] = it
    else next.push(it)
    if (!extra.value.includes(it.real)) extra.value.push(it.real)
  }
  const aliases = next.map((m) => m.alias)
  if (new Set(aliases).size !== aliases.length) {
    manualError.value = '别名不能重复'
    return
  }
  emit('update:modelValue', next)
  manual.value = ''
}

async function editAlias(m: ModelPair): Promise<void> {
  const v = await promptText({
    title: '设置别名',
    detail: `下游客户端按别名请求时，会以 ${m.real} 发往上游。留空表示与真实名相同。`,
    label: '别名',
    initial: m.alias === m.real ? '' : m.alias,
    placeholder: m.real,
    mono: true,
    validate: (text) => {
      const alias = text.trim() || m.real
      return props.modelValue.some((x) => x.real !== m.real && x.alias === alias) ? `别名 ${alias} 已被其他模型使用` : null
    },
  })
  if (v === null) return
  const alias = v.trim() || m.real
  emit(
    'update:modelValue',
    props.modelValue.map((x) => (x.real === m.real ? { real: x.real, alias } : x)),
  )
}

onMounted(() => {
  if (props.autoDiscover && props.discover) runDiscover()
})
</script>

<template>
  <div class="mp">
    <div v-if="discover" class="disc">
      <Btn size="sm" :loading="discovering" @click="runDiscover"><CloudDownload /> {{ discovered.length ? '重新获取' : '从上游获取模型' }}</Btn>
      <span v-if="discovering" class="faint small">正在获取模型列表…</span>
      <span v-else-if="note" class="small" :class="note.tone === 'ok' ? 'c-green' : 'c-orange'">{{ note.text }}</span>
      <span v-else-if="staticModels?.length" class="faint small">已列出 {{ staticModels.length }} 个内置参考模型</span>
    </div>
    <CheckList :model-value="selected" :options="options" max-height="260px" empty-text="还没有模型，可从上游获取或手动添加" @update:model-value="setSelected" />
    <div class="manual">
      <TextInput v-model="manual" mono placeholder="手动添加：真实名[:别名]，多个用逗号分隔" @enter="addManual" />
      <Btn size="md" @click="addManual"><Plus /> 添加</Btn>
    </div>
    <p v-if="manualError" class="err">{{ manualError }}</p>
    <div v-if="modelValue.length" class="sel">
      <p class="faint small">已选 {{ modelValue.length }} 个 · 点击模型可设置别名（下游按别名请求，发往上游时用真实名）</p>
      <div class="chips">
        <button v-for="m in modelValue" :key="m.real" type="button" class="chip mono" @click="editAlias(m)">
          {{ m.real }}<template v-if="m.alias !== m.real"> → <b>{{ m.alias }}</b></template>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mp {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.disc {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.small {
  font-size: 12px;
  line-height: 1.5;
}
.manual {
  display: flex;
  gap: 8px;
}
.manual :deep(.ctl) {
  flex: 1;
}
.err {
  font-size: 11.5px;
  color: var(--red);
}
.sel {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 120px;
  overflow-y: auto;
}
.chip {
  max-width: 100%;
  min-height: 24px;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  text-align: left;
  word-break: break-all;
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--border);
}
.chip:hover {
  background: var(--accent-soft);
  color: var(--accent);
}
</style>
