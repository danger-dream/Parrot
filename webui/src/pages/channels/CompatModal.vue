<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, enc, type Schemas } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Modal from '@/components/ui/Modal.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import type { Channel } from './meta'

type Compat = Schemas['ChannelCompatibilityData']
type Mode = 'auto' | 'force'
type Scope = 'all' | 'some'
interface FeatureDraft {
  mode: Mode
  scope: Scope
  models: string[]
}

const props = defineProps<{ open: boolean; channel: Channel }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const data = ref<Compat | null>(null)
const loadError = ref('')
const saving = ref(false)
const drafts = ref<Record<'context1m' | 'fast', FeatureDraft>>({
  context1m: { mode: 'auto', scope: 'all', models: [] },
  fast: { mode: 'auto', scope: 'all', models: [] },
})

const isAnthropic = computed(() => props.channel.protocol === 'anthropic')
const features = computed(() =>
  [
    {
      key: 'context1m' as const,
      title: '1M 上下文',
      auto: '只透传下游已经请求的 1M 上下文标志。',
      force: '命中范围的模型会主动加入 1M 上下文标志（context-1m-2025-08-07）。',
      show: isAnthropic.value,
    },
    {
      key: 'fast' as const,
      title: 'Fast 模式',
      auto: '只透传下游已经请求的 Fast 模式。',
      force: 'Anthropic 协议写入 speed=fast 与 fast-mode beta；OpenAI 协议写入 service_tier=priority。',
      show: true,
    },
  ].filter((f) => f.show),
)
const modelOptions = computed(() =>
  props.channel.models.map((m) => ({ value: m.real, label: m.real, hint: m.alias !== m.real ? `别名：${m.alias}` : undefined })),
)

function fromData(d: Compat): void {
  for (const k of ['context1m', 'fast'] as const) {
    const f = d[k]
    drafts.value[k] = { mode: f.mode as Mode, scope: f.allModels ? 'all' : 'some', models: [...f.models] }
  }
}

watch(
  () => props.open,
  async (o) => {
    if (!o) return
    data.value = null
    loadError.value = ''
    try {
      data.value = (await api.get<Compat>(`/channels/${enc(props.channel.id)}/compatibility`)).data
      fromData(data.value)
    } catch (err) {
      loadError.value = errorTitle(err)
    }
  },
  { immediate: true },
)

const invalid = computed(() => features.value.some((f) => drafts.value[f.key].scope === 'some' && !drafts.value[f.key].models.length))

async function save(): Promise<void> {
  if (!data.value) return
  saving.value = true
  const body = Object.fromEntries(
    (['context1m', 'fast'] as const).map((k) => {
      const d = drafts.value[k]
      return [k, { mode: d.mode, models: d.scope === 'all' ? [] : d.models }]
    }),
  )
  try {
    await api.patch(`/channels/${enc(props.channel.id)}/compatibility`, body, { ifMatch: data.value.revision })
    toast('兼容性设置已保存', 'success')
    emit('saved')
    emit('close')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="兼容性设置" :subtitle="channel.name" width="md" :dismissible="!saving" @close="emit('close')">
    <p class="lead">这些设置只影响这个渠道发往上游的请求。「自动」表示原样透传下游的请求；「强制」表示由 Parrot 为命中的模型主动开启。</p>
    <p v-if="loadError" class="c-red">{{ loadError }}</p>
    <div v-else-if="!data" class="sk"><Skeleton h="80px" r="10px" /><Skeleton h="80px" r="10px" /></div>
    <template v-else>
      <section v-for="f in features" :key="f.key" class="feat">
        <div class="f-head">
          <p class="f-title">{{ f.title }}</p>
          <Segmented
            v-model="drafts[f.key].mode"
            size="sm"
            :options="[
              { value: 'auto', label: '自动' },
              { value: 'force', label: '强制' },
            ]"
          />
        </div>
        <p class="f-desc">{{ drafts[f.key].mode === 'force' ? f.force : f.auto }}</p>
        <div class="scope">
          <span class="s-label">作用范围</span>
          <Segmented
            v-model="drafts[f.key].scope"
            size="sm"
            :options="[
              { value: 'all', label: '全部模型（含今后新增）' },
              { value: 'some', label: '指定模型' },
            ]"
          />
        </div>
        <CheckList v-if="drafts[f.key].scope === 'some'" v-model="drafts[f.key].models" :options="modelOptions" max-height="200px" />
        <p v-if="drafts[f.key].scope === 'some' && !drafts[f.key].models.length" class="err">请至少选择一个模型，或改为全部模型</p>
        <p v-if="drafts[f.key].mode === 'auto' && drafts[f.key].scope === 'some'" class="f-desc">模型范围可以预先选好，切换到「强制」后生效。未命中范围的模型始终保持自动透传。</p>
      </section>
      <p v-if="!isAnthropic" class="f-desc">1M 上下文只适用于 Anthropic 协议的渠道。</p>
    </template>
    <template #footer>
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="!data || invalid" @click="save">保存</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.lead {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.6;
  margin-bottom: 12px;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.feat {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.feat + .feat {
  margin-top: 10px;
}
.f-head,
.scope {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.f-title {
  font-weight: 650;
}
.f-desc {
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.5;
}
.s-label {
  font-size: 12px;
  color: var(--text-2);
}
.err {
  font-size: 12px;
  color: var(--red);
}
</style>
