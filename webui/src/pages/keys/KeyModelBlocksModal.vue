<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Plus, Trash2 } from '@lucide/vue'
import { api, enc } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import Select from '@/components/ui/Select.vue'
import Switch from '@/components/ui/Switch.vue'
import TextArea from '@/components/ui/TextArea.vue'
import { loadModelOptions, type ApiKey } from './meta'
import { flattenModelBlocks, groupModelBlocks, modelBlockOptions, modelBlockValidation, updateModelSelection,
  type ModelBlockGroup, type ModelBlockRule, type ModelOption } from './modelBlockGroups'

const props = defineProps<{ open: boolean; k: ApiKey }>()
const emit = defineEmits<{ close: []; saved: [key: ApiKey] }>()
const groups = ref<ModelBlockGroup[]>([])
const savedModels = ref<Record<number, string[]>>({})
const revision = ref('')
const saving = ref(false)
const models = ref<ModelOption[]>([])
const loading = ref(false)
const loadError = ref('')
let loadVersion = 0
let nextId = 0
const presets: { value: NonNullable<ModelBlockRule['preset']>; label: string; status: number; message: string }[] = [
  { value: 'quota_exhausted', label: '额度耗尽', status: 402, message: 'The quota for this model has been exhausted.' },
  { value: 'insufficient_balance', label: '余额不足', status: 402, message: 'Insufficient balance to use this model.' },
  { value: 'service_busy', label: '服务繁忙', status: 403, message: 'This model is busy and unavailable for this API key.' },
  { value: 'model_unavailable', label: '模型不可用', status: 403, message: 'This model is unavailable for this API key.' },
]
function preset(group: ModelBlockGroup) { return presets.find((p) => p.value === group.preset) || presets[0]! }
function options(group: ModelBlockGroup) { return modelBlockOptions(models.value, savedModels.value[group.id] || []) }
function select(group: ModelBlockGroup, next: string[]) {
  if (saving.value || loading.value) return
  group.models = updateModelSelection(group.models, next, options(group))
}
async function loadModels() {
  const version = ++loadVersion
  loading.value = true
  loadError.value = ''
  try {
    const result = await loadModelOptions(true)
    if (version === loadVersion) models.value = result
  } catch (err) {
    if (version === loadVersion) {
      models.value = []
      loadError.value = errorTitle(err)
    }
  } finally {
    if (version === loadVersion) loading.value = false
  }
}
// Compare source values, not a freshly allocated tuple: runtime snapshots replace
// k even for the same key. Keep that edit session (including its revision) intact.
watch([() => props.open, () => props.k.keyId], ([open]) => {
  ++loadVersion // Ignore requests from a closed dialog or a different key.
  models.value = []
  loadError.value = ''
  loading.value = false
  groups.value = [] // Remount lists/details, clearing old search/expanded state.
  savedModels.value = {}
  if (!open) return
  groups.value = groupModelBlocks(props.k.modelBlocks || []).map((group) => ({ ...group, id: nextId++ }))
  savedModels.value = Object.fromEntries(groups.value.map((group) => [group.id, [...group.models]]))
  revision.value = props.k.revision
  void loadModels()
}, { immediate: true })
onBeforeUnmount(() => { ++loadVersion })
const count = computed(() => groups.value.reduce((total, group) => total + group.models.length, 0))
const validation = computed(() => modelBlockValidation(groups.value))
const cannotSave = computed(() => saving.value || loading.value || !!validation.value)
function add() { groups.value.push({ id: nextId++, models: [], enabled: true, preset: 'quota_exhausted', message: null }) }
async function save() {
  if (cannotSave.value) return
  saving.value = true
  try {
    const res = await api.patch<ApiKey>(`/api-keys/${enc(props.k.keyId)}`, {
      modelBlocks: flattenModelBlocks(groups.value),
    }, { ifMatch: revision.value })
    toast('模型拦截规则已保存', 'success')
    emit('saved', res.data)
    emit('close')
  } catch (err) { toast(errorTitle(err), 'error', errorDetail(err), 7000) }
  finally { saving.value = false }
}
</script>

<template>
  <Modal :open="open" title="模型拦截" :subtitle="k.name" width="md" :dismissible="!saving" @close="emit('close')">
    <p class="lead">仅拦截这个 Key 的指定文本模型，不改变模型列表或原有权限。保存后对新请求（含 WebSocket 后续轮次）生效，不中断正在生成的回复。</p>
    <div class="notice">本地拒绝，不调用上游、不计费，Parrot 不重试。常见 SDK 不自动重试；无法阻止第三方强制重发，但重发仍在本地拒绝。</div>
    <p v-if="loading" class="load-status" role="status">正在加载模型列表…</p>
    <div v-else-if="loadError" class="load-error" role="alert">
      <p>模型列表加载失败：{{ loadError }}。仍保留已保存项，重试后可选择其他模型。</p>
      <Btn size="sm" :disabled="saving" @click="loadModels">重试加载</Btn>
    </div>
    <div v-if="!groups.length" class="empty">暂无拦截规则 · 该 Key 保持原有行为</div>
    <div class="rules">
      <section v-for="(group, i) in groups" :key="group.id" class="rule" :aria-label="`规则组 ${i + 1}`">
        <header>
          <span class="rule-title">规则组 {{ i + 1 }} <span class="group-count">· {{ group.models.length }} 个模型</span></span>
          <Switch v-model="group.enabled" :disabled="saving" :label="`启用规则组 ${i + 1}`" />
          <Btn size="sm" variant="ghost" :disabled="saving" :aria-label="`移除规则组 ${i + 1}`" @click="groups.splice(i, 1)"><Trash2 /> 移除</Btn>
        </header>
        <fieldset :disabled="saving || loading" :aria-busy="loading">
          <legend>拦截模型（可多选）<span class="required">*</span></legend>
          <CheckList :model-value="group.models" :options="options(group)" max-height="220px"
            empty-text="没有可选模型；搜索仅筛选列表，不创建模型" @update:model-value="select(group, $event)" />
          <p class="hint">从列表勾选模型；精确匹配请求名或有效模型（含其别名），不做子串匹配。</p>
        </fieldset>
        <Field label="对外错误预设"><Select v-model="group.preset" :options="presets" :disabled="saving" :aria-label="`规则组 ${i + 1} 的错误预设`" /></Field>
        <details class="advanced">
          <summary>高级选项</summary>
          <div class="advanced-body">
            <Field label="自定义提示（可选）" hint="建议使用英文；留空使用预设。最多 1000 字符，仅替换 message，不改变状态码。">
              <TextArea v-model="group.message" :rows="2" :placeholder="preset(group).message" :disabled="saving"
                :aria-label="`规则组 ${i + 1} 的自定义提示`" :invalid="(group.message?.length || 0) > 1000" />
            </Field>
            <p class="preview"><span class="mono">{{ preset(group).status }} · {{ preset(group).value }}</span><br>{{ group.message?.trim() || preset(group).message }}</p>
            <p class="hint">固定返回非重试型 402 / 403 与不可重试提示，不返回 429 / 5xx。</p>
          </div>
        </details>
      </section>
    </div>
    <div class="add-row">
      <Btn :disabled="saving || loading || groups.length >= 100 || count >= 100" @click="add"><Plus /> 新增规则组</Btn>
      <span class="hint">已选 {{ count }} / 100 个模型</span>
    </div>
    <p v-if="validation" class="validation" role="alert">{{ validation }}</p>
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="cannotSave" @click="save">保存规则</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.lead, .notice { font-size: 12.5px; line-height: 1.6; color: var(--text-2); margin-bottom: 12px; }
.notice { padding: 10px 12px; border-left: 2px solid var(--accent); background: var(--accent-soft); border-radius: var(--radius-sm); }
.empty { padding: 24px 0; text-align: center; font-size: 12.5px; color: var(--text-3); }
.rules { display: flex; flex-direction: column; gap: 12px; margin-bottom: 12px; }
.rule { padding: 14px; border: 1px solid var(--border); border-radius: var(--radius-lg); display: flex; flex-direction: column; gap: 12px; }
header { display: flex; align-items: center; gap: 10px; }
.rule-title { flex: 1; font-size: 12.5px; font-weight: 600; }
.group-count { color: var(--text-3); font-weight: 400; }
fieldset { min-width: 0; border: 0; padding: 0; margin: 0; }
legend { padding: 0; margin-bottom: 6px; font-size: 12.5px; font-weight: 500; }
.required { color: var(--red); margin-left: 3px; }
.hint { font-size: 11.5px; line-height: 1.5; color: var(--text-3); margin-top: 6px; }
.advanced { border-top: 1px solid var(--hairline); padding-top: 10px; }
summary { cursor: pointer; font-size: 12px; color: var(--text-2); }
summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: var(--radius-sm); }
.advanced-body { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
.preview { padding-top: 10px; border-top: 1px solid var(--hairline); color: var(--text-3); font-size: 11.5px; line-height: 1.6; overflow-wrap: anywhere; white-space: pre-wrap; }
.add-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.load-status, .load-error, .validation { font-size: 12px; line-height: 1.6; }
.load-status, .load-error { margin-bottom: 12px; }
.load-error, .validation { color: var(--red); }
.load-error p { margin-bottom: 6px; }
.validation { margin-top: 10px; }
</style>
