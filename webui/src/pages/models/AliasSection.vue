<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Check, Pencil, Plus, Trash2, X } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import TextInput from '@/components/ui/TextInput.vue'
import DetailSection from '@/components/DetailSection.vue'
import { fetchAll, modelStore } from './shared'

// Alias mappings that point at this model, edited in place (put/update/deleteModelMapping).

type Mapping = Schemas['MappingData']

const props = defineProps<{ modelId: string }>()
const emit = defineEmits<{ changed: [] }>()

const all = ref<Mapping[]>([])
const revision = ref('')
const loading = ref(true)
const busy = ref('')
const adding = ref('')
const addError = ref('')
const editing = ref('')
const editText = ref('')
const editError = ref('')

async function load(): Promise<void> {
  try {
    const res = await fetchAll<Mapping>('/model-mappings')
    all.value = res.items
    revision.value = res.revision
  } catch {
    all.value = []
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(() => props.modelId, load)

const mine = computed(() => all.value.filter((m) => m.realModel === props.modelId))

function validate(name: string, current?: string): string {
  const a = name.trim()
  if (!a) return '请填写别名'
  if (/\s/.test(a)) return '别名不能包含空白'
  if (a.length > 300) return '最多 300 个字符'
  if (a === current) return ''
  if (modelStore.models.some((m) => m.modelId === a)) return '这个名字已经是真实模型，不能用作别名'
  const taken = all.value.find((m) => m.alias === a)
  if (taken) return taken.realModel === props.modelId ? '这个别名已存在' : `已被用作 ${taken.realModel} 的别名`
  return ''
}

function conflictText(err: unknown): string {
  if (err instanceof ApiError && (err.code === 'RESOURCE_CONFLICT' || err.code === 'IDENTITY_CONFLICT')) return '名字与已有别名或真实模型冲突'
  return ''
}

async function add(): Promise<void> {
  const a = adding.value.trim()
  addError.value = validate(a)
  if (addError.value) return
  busy.value = 'add'
  try {
    await api.put(`/model-mappings/${enc(a)}`, { realModel: props.modelId }, { ifMatch: revision.value })
    toast('已添加别名', 'success', `${a} → ${props.modelId}`)
    adding.value = ''
    await load()
    emit('changed')
  } catch (err) {
    addError.value = conflictText(err)
    if (!addError.value) toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') load()
  } finally {
    busy.value = ''
  }
}

function startEdit(m: Mapping): void {
  editing.value = m.alias
  editText.value = m.alias
  editError.value = ''
}

async function rename(m: Mapping): Promise<void> {
  const a = editText.value.trim()
  if (a === m.alias) {
    editing.value = ''
    return
  }
  editError.value = validate(a, m.alias)
  if (editError.value) return
  busy.value = `ed:${m.alias}`
  try {
    await api.patch(`/model-mappings/${enc(m.alias)}`, { alias: a, realModel: props.modelId }, { ifMatch: m.revision })
    toast('别名已改名', 'success', `${m.alias} → ${a}`)
    editing.value = ''
    await load()
    emit('changed')
  } catch (err) {
    editError.value = conflictText(err)
    if (!editError.value) toast(errorTitle(err), 'error', errorDetail(err), 7000)
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') load()
  } finally {
    busy.value = ''
  }
}

async function remove(m: Mapping): Promise<void> {
  const ok = await confirmAction({
    title: `删除别名 ${m.alias}？`,
    detail: `删除后客户端再请求 ${m.alias} 会按原名调度，很可能因为找不到模型而失败。${props.modelId} 本身不受影响。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  busy.value = `rm:${m.alias}`
  try {
    await api.del(`/model-mappings/${enc(m.alias)}`, { ifMatch: m.revision })
    toast('别名已删除', 'success', m.alias)
    await load()
    emit('changed')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <DetailSection title="别名" :note="mine.length ? `${mine.length} 个` : undefined">
    <p class="hint faint">客户端请求别名时，按这个模型调度。改动立即生效，只影响这条别名。</p>
    <div class="list">
      <div v-for="m in mine" :key="m.alias" class="al">
        <template v-if="editing === m.alias">
          <TextInput v-model="editText" mono :invalid="Boolean(editError)" autofocus @enter="rename(m)" />
          <Btn icon size="sm" variant="primary" title="保存" :loading="busy === `ed:${m.alias}`" @click="rename(m)"><Check /></Btn>
          <Btn icon size="sm" variant="ghost" title="取消" @click="editing = ''"><X /></Btn>
        </template>
        <template v-else>
          <span class="mono name">{{ m.alias }}</span>
          <span v-if="m.sourceLine !== 'global'" class="faint small">旧版入口配置</span>
          <Btn icon size="sm" variant="ghost" title="改名" @click="startEdit(m)"><Pencil /></Btn>
          <Btn icon size="sm" variant="ghost" title="删除" :loading="busy === `rm:${m.alias}`" @click="remove(m)"><Trash2 /></Btn>
        </template>
      </div>
      <p v-if="editing && editError" class="err">{{ editError }}</p>
      <p v-if="!loading && !mine.length" class="faint small">还没有别名。</p>
    </div>
    <div class="add">
      <TextInput v-model="adding" mono placeholder="新增别名，例如 gpt" :invalid="Boolean(addError)" @enter="add" @update:model-value="addError = ''" />
      <Btn size="sm" :loading="busy === 'add'" :disabled="!adding.trim()" @click="add"><Plus /> 添加</Btn>
    </div>
    <p v-if="addError" class="err">{{ addError }}</p>
  </DetailSection>
</template>

<style scoped>
.hint {
  font-size: 11.5px;
}
.list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.al {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 4px 0 10px;
  border-radius: 7px;
  background: var(--fill-3);
}
.al .name {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 600;
  word-break: break-all;
}
.al > :deep(.ctl) {
  flex: 1;
}
.add {
  display: flex;
  gap: 6px;
  align-items: center;
}
.add > :first-child {
  flex: 1;
  min-width: 0;
}
.err {
  font-size: 11.5px;
  color: var(--red);
}
.small {
  font-size: 11.5px;
}
</style>
