<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { FlaskConical } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { errorDetail, errorTitle } from '@/utils/errors'
import { toast } from '@/stores/ui'
import Btn from '@/components/ui/Btn.vue'
import Empty from '@/components/ui/Empty.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import TextArea from '@/components/ui/TextArea.vue'
import TextInput from '@/components/ui/TextInput.vue'
import TraceView from './TraceView.vue'
import { PHASE_OPTIONS, providerLabel, type EditorSample, type ErrorRulesData, type Phase, type TestResult } from './model'

const props = defineProps<{
  open: boolean
  data: ErrorRulesData
  sample: EditorSample | null
  channelOptions: { value: string; label: string; hint?: string }[]
}>()
const emit = defineEmits<{ close: []; createFrom: [sample: EditorSample] }>()

const EXAMPLES: { label: string; sample: EditorSample }[] = [
  {
    label: '智谱 5 小时用量',
    sample: {
      provider: 'zhipu', channelKey: '', model: 'glm-5.3', status: 429, phase: 'http',
      body: '{"type":"error","error":{"type":"rate_limit_error","code":"1308","message":"[1308][已达到 5 小时的使用上限。您的限额将在 2026-11-21 02:30:00 重置。]"}}',
    },
  },
  {
    label: 'MiniMax Token Plan',
    sample: {
      provider: 'minimax', channelKey: '', model: 'MiniMax-M3.1', status: 500, phase: 'http',
      body: '{"error":{"message":"当前已达到 Token Plan 用量上限。为避免调用中断，请升级 Token Plan 套餐，或购买积分补充用量并开启积分自动消耗。 (2067)","code":"server_error"}}',
    },
  },
  {
    label: 'OpenAI 过载',
    sample: {
      provider: 'openai', channelKey: '', model: 'gpt-6.1', status: 503, phase: 'http',
      body: '{"error":{"type":"service_unavailable_error","code":"server_is_overloaded","message":"Our servers are currently overloaded. Please try again later."}}',
    },
  },
]

const form = reactive<EditorSample>({ provider: '', channelKey: '', model: '', status: 429, phase: 'http', body: '' })
const result = ref<TestResult | null>(null)
const running = ref(false)

function fill(s: EditorSample | null): void {
  form.provider = s?.provider || ''
  form.channelKey = s?.channelKey || ''
  form.model = s?.model || ''
  form.status = s ? s.status : 429
  form.phase = s?.phase || 'http'
  form.body = s?.body || ''
  result.value = null
}

watch(
  () => [props.open, props.sample] as const,
  ([open]) => {
    if (!open) return
    fill(props.sample)
    if (props.sample?.body) void run()
  },
  { immediate: true },
)

const providerOptions = computed(() => [
  { value: '', label: '按渠道自动识别' },
  ...props.data.providers.map((p) => ({ value: p.id, label: providerLabel(p.id) })),
])
const channelSelect = computed(() => [{ value: '', label: '不指定渠道' }, ...props.channelOptions])
const phaseOptions = PHASE_OPTIONS.map((p) => ({ value: p.value as Phase, label: p.label }))

async function run(): Promise<void> {
  running.value = true
  try {
    const res = await api.post<TestResult>('/error-rules/test', {
      channelKey: form.channelKey,
      provider: form.provider,
      model: form.model.trim(),
      phase: form.phase,
      status: form.status || null,
      body: form.body,
    } satisfies Schemas['ErrorRulesTestRequest'])
    result.value = res.data
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    running.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="试一试" subtitle="填一段上游错误，看看会由哪条规则处理、会做什么。只是模拟，不会真的暂停或停用任何东西。" width="xl" @close="emit('close')">
    <div class="tester">
      <div class="input">
        <div class="examples">
          <span class="ex-label">示例：</span>
          <button v-for="e in EXAMPLES" :key="e.label" type="button" class="ex" @click="fill(e.sample); run()">{{ e.label }}</button>
        </div>
        <div class="grid2">
          <Field label="提供商">
            <Select v-model="form.provider" :options="providerOptions" width="100%" />
          </Field>
          <Field label="渠道">
            <Select v-model="form.channelKey" :options="channelSelect" searchable width="100%" />
          </Field>
          <Field label="模型">
            <TextInput v-model="form.model" mono placeholder="可不填" />
          </Field>
          <Field label="HTTP 状态码">
            <NumberInput v-model="form.status" :min="100" :max="599" nullable integer placeholder="没有就留空" width="100%" />
          </Field>
        </div>
        <Field label="发生阶段">
          <Segmented v-model="form.phase" :options="phaseOptions" size="sm" />
        </Field>
        <Field label="上游返回的内容" hint="直接粘贴错误响应体，JSON 或纯文本都可以">
          <TextArea v-model="form.body" :rows="7" mono placeholder='{"error":{"code":"1308","message":"..."}}' />
        </Field>
        <div class="run">
          <Btn variant="primary" :loading="running" @click="run"><FlaskConical /> 试一下</Btn>
        </div>
      </div>
      <div class="output">
        <TraceView v-if="result" :result="result" :categories="data.categories" />
        <Empty v-else title="结果会显示在这里" detail="可以先点上面的示例看看效果。" />
        <div v-if="result && !result.matched" class="cta">
          <p>想让 Parrot 换一种方式处理这类错误？</p>
          <Btn size="sm" @click="emit('createFrom', { ...form })">为它创建规则</Btn>
        </div>
      </div>
    </div>
  </Modal>
</template>

<style scoped>
.tester {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.input {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.examples {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.ex-label {
  color: var(--text-3);
}
.ex {
  padding: 3px 9px;
  border-radius: 7px;
  background: var(--fill-2);
  color: var(--text-2);
  cursor: pointer;
}
.ex:hover {
  background: var(--fill);
  color: var(--text);
}
.grid2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.run {
  display: flex;
  justify-content: flex-end;
}
.output {
  position: sticky;
  top: 0;
  min-width: 0;
}
.cta {
  margin-top: 14px;
  padding: 12px 14px;
  border-radius: 12px;
  border: 0.5px dashed var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12.5px;
  color: var(--text-2);
}
@media (max-width: 860px) {
  .tester {
    grid-template-columns: 1fr;
  }
  .output {
    position: static;
  }
}
</style>
