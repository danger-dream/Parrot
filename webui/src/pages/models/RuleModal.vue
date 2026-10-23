<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ChevronDown } from '@lucide/vue'
import type { Schemas } from '@/api/client'
import { catalog, refreshCatalog } from '@/stores/catalog'
import Modal from '@/components/ui/Modal.vue'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import TextInput from '@/components/ui/TextInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import ModelPicker from './ModelPicker.vue'
import { channelLabel } from './shared'

type Rule = Schemas['RegistryRuleData']

const props = defineProps<{ open: boolean; saving?: boolean }>()
const emit = defineEmits<{ close: []; save: [rule: Rule] }>()

const form = reactive({ type: 'merge' as 'merge' | 'assign', model: '', into: '', source: '', upstreamModel: '' })
const picking = ref<'' | 'model' | 'into'>('')
const touched = ref(false)

watch(
  () => form.type,
  () => (touched.value = false),
)

function reset(): void {
  Object.assign(form, { type: 'merge', model: '', into: '', source: '', upstreamModel: '' })
  touched.value = false
  refreshCatalog()
}
watch(
  () => props.open,
  (o) => o && reset(),
)

const sourceOptions = computed(() => [
  { value: '', label: '选择账户或渠道' },
  ...Object.keys(catalog.channels)
    .sort((a, b) => channelLabel(a).localeCompare(channelLabel(b)))
    .map((id) => ({ value: id, label: `${channelLabel(id)}${id.startsWith('api:') ? '（API 渠道）' : ''}` })),
])

const errors = computed(() => {
  const e: Record<string, string> = {}
  const name = /^\S{1,300}$/
  if (!name.test(form.model.trim())) e.model = '请填写模型名，不能含空白'
  if (form.type === 'merge') {
    if (!name.test(form.into.trim())) e.into = '请选择要并入的目标模型'
    else if (form.into.trim() === form.model.trim()) e.into = '不能并入自己'
  } else {
    if (!form.source) e.source = '请选择来源'
    if (!name.test(form.upstreamModel.trim())) e.upstreamModel = '请填写上游模型名'
  }
  return e
})

function submit(): void {
  touched.value = true
  if (Object.keys(errors.value).length) return
  const rule: Rule =
    form.type === 'merge'
      ? { type: 'merge', model: form.model.trim(), into: form.into.trim() }
      : { type: 'assign', model: form.model.trim(), source: form.source, upstreamModel: form.upstreamModel.trim() }
  emit('save', rule)
}

function onPick(id: string): void {
  if (picking.value === 'model') form.model = id
  else if (picking.value === 'into') form.into = id
  picking.value = ''
}

</script>

<template>
  <Modal :open="open" title="新增命名规则" width="md" @close="emit('close')">
    <div class="form">
      <Segmented
        v-model="form.type"
        :options="[
          { value: 'merge', label: '合并两个模型名' },
          { value: 'assign', label: '指定来源的上游模型' },
        ]"
      />
      <p class="explain">
        <template v-if="form.type === 'merge'">把「旧名字」的所有来源归入「目标模型」。旧名字继续可以调用，相当于目标模型的别名。</template>
        <template v-else>某个账户/渠道提供的上游模型（例如 <span class="mono">hy3-x</span>），固定归到你指定的模型名下。填已有模型名即并入它，填新名字即单独成为一个模型。</template>
      </p>

      <template v-if="form.type === 'merge'">
        <Field label="旧名字" required :error="touched ? errors.model : ''" hint="会变成别名的那个模型名">
          <div class="pick">
            <TextInput v-model="form.model" mono placeholder="如 claude-haiku-4-5" />
            <Btn icon title="从模型列表选择" @click="picking = 'model'"><ChevronDown /></Btn>
          </div>
        </Field>
        <Field label="并入目标模型" required :error="touched ? errors.into : ''">
          <div class="pick">
            <TextInput v-model="form.into" mono placeholder="如 claude-haiku-4-5-20251001" />
            <Btn icon title="从模型列表选择" @click="picking = 'into'"><ChevronDown /></Btn>
          </div>
        </Field>
      </template>
      <template v-else>
        <Field label="来源" required :error="touched ? errors.source : ''">
          <Select v-model="form.source" :options="sourceOptions" />
        </Field>
        <Field label="上游模型名" required :error="touched ? errors.upstreamModel : ''" hint="这个来源实际发给上游的模型 ID">
          <TextInput v-model="form.upstreamModel" mono placeholder="如 hy3-x" />
        </Field>
        <Field label="归入模型名" required :error="touched ? errors.model : ''">
          <div class="pick">
            <TextInput v-model="form.model" mono placeholder="如 hy3" />
            <Btn icon title="从模型列表选择" @click="picking = 'model'"><ChevronDown /></Btn>
          </div>
        </Field>
      </template>
    </div>
    <template #footer>
      <Btn variant="ghost" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" @click="submit">添加规则</Btn>
    </template>
  </Modal>
  <ModelPicker
    :open="Boolean(picking)"
    :title="picking === 'into' ? '选择目标模型' : '选择模型'"
    :kinds="['chat', 'image']"
    :current="picking === 'into' ? form.into : form.model"
    @close="picking = ''"
    @pick="onPick"
  />
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.explain {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.6;
}
.pick {
  display: flex;
  gap: 6px;
}
.pick > :first-child {
  flex: 1;
  min-width: 0;
}
</style>
