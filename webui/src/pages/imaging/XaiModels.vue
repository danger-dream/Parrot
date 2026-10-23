<script setup lang="ts">
import { computed, ref } from 'vue'
import { Film, Image as ImageIcon, Pencil, Plus, Trash2 } from '@lucide/vue'
import { api, ApiError, enc, type Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import { confirmAction, promptText } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { openModel } from '@/links'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'

type Settings = Schemas['XaiMediaSettingsData']
type Kind = 'image' | 'video'

const emit = defineEmits<{ changed: [] }>()
const res = useResource<Settings>('/xai/media-settings', { successText: 'xAI 媒体设置已保存' })
const d = computed(() => res.draft.value)
const busy = ref('')

// Only the two numbers are edited through the draft; model lists use the dedicated add/rename/remove calls.
const dirty = computed(
  () => Boolean(res.data.value && d.value) && (d.value!.jobTtlSeconds !== res.data.value!.jobTtlSeconds || d.value!.requestTimeoutSeconds !== res.data.value!.requestTimeoutSeconds),
)
async function saveNumbers(): Promise<void> {
  if (!d.value || !res.data.value) return
  const body: Record<string, unknown> = {}
  if (d.value.jobTtlSeconds !== res.data.value.jobTtlSeconds) body.jobTtlSeconds = d.value.jobTtlSeconds
  if (d.value.requestTimeoutSeconds !== res.data.value.requestTimeoutSeconds) body.requestTimeoutSeconds = d.value.requestTimeoutSeconds
  if (await res.save(body)) emit('changed')
}

const NOUN: Record<Kind, string> = { image: '图片', video: '视频' }
const validate = (v: string) => (!v.trim() ? '请输入模型名称' : v.trim().length > 128 ? '不能超过 128 个字符' : /\s/.test(v.trim()) ? '模型名称不能包含空格' : null)

async function run(tag: string, fn: (rev?: string) => Promise<unknown>, ok: string): Promise<void> {
  busy.value = tag
  try {
    await fn(res.data.value?.revision)
    toast(ok, 'success')
    await res.load()
    emit('changed')
  } catch (err) {
    if (err instanceof ApiError && err.code === 'REVISION_CONFLICT') {
      await res.load()
      toast('xAI 媒体设置已在别处被修改', 'warning', '已载入最新数据，请重新操作。')
    } else toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    busy.value = ''
  }
}

async function add(kind: Kind): Promise<void> {
  const name = await promptText({ title: `新增 xAI ${NOUN[kind]}模型`, label: '模型名称', placeholder: kind === 'image' ? 'grok-imagine-image' : 'grok-imagine-video', mono: true, confirmText: '新增', validate })
  if (!name) return
  await run(`add:${kind}`, (rev) => api.post(`/xai/media-models/${kind}`, { modelId: name.trim() }, { ifMatch: rev }), `已新增 ${name.trim()}`)
}
async function rename(kind: Kind, model: string): Promise<void> {
  const name = await promptText({ title: `重命名 ${model}`, label: '新的模型名称', initial: model, mono: true, confirmText: '重命名', validate: (v) => validate(v) || (v.trim() === model ? '名称没有变化' : null) })
  if (!name) return
  await run(`mv:${kind}:${model}`, (rev) => api.patch(`/xai/media-models/${kind}/${enc(model)}`, { newModelId: name.trim() }, { ifMatch: rev }), `已改名为 ${name.trim()}`)
}
async function remove(kind: Kind, model: string): Promise<void> {
  const ok = await confirmAction({
    title: `删除 ${model}？`,
    detail: `客户端将不能再通过这个名称调用 xAI ${NOUN[kind]}生成。历史记录不受影响。`,
    danger: true,
    confirmText: '删除',
  })
  if (!ok) return
  await run(`rm:${kind}:${model}`, (rev) => api.del(`/xai/media-models/${kind}/${enc(model)}`, { ifMatch: rev }), `已删除 ${model}`)
}

const lists = computed(() => [
  { kind: 'image' as Kind, icon: ImageIcon, items: res.data.value?.imageModels || [] },
  { kind: 'video' as Kind, icon: Film, items: res.data.value?.videoModels || [] },
])
defineExpose({ reload: () => res.load() })
</script>

<template>
  <SettingsSection
    title="xAI 媒体模型"
    description="Grok 图片 / 视频可用的模型名称，以及 xAI 媒体任务的超时设置。"
    :loading="res.loading.value && !d"
    :error="res.error.value && !d ? errorTitle(res.error.value) : undefined"
    :dirty="dirty"
    :saving="res.saving.value"
    @save="saveNumbers"
    @reset="res.reset()"
  >
    <template v-if="d">
      <div class="lists">
        <section v-for="l in lists" :key="l.kind" class="ml">
          <header>
            <p class="t"><component :is="l.icon" />{{ NOUN[l.kind] }}模型 <span class="faint">{{ l.items.length }} 个</span></p>
            <Btn size="sm" :loading="busy === `add:${l.kind}`" @click="add(l.kind)"><Plus /> 新增</Btn>
          </header>
          <p v-if="!l.items.length" class="faint small">还没有{{ NOUN[l.kind] }}模型。</p>
          <ul v-else>
            <li v-for="m in l.items" :key="m">
              <button type="button" class="nm mono truncate" title="查看模型" @click="openModel(m)">{{ m }}</button>
              <Btn size="sm" variant="ghost" icon title="重命名" :loading="busy === `mv:${l.kind}:${m}`" @click="rename(l.kind, m)"><Pencil /></Btn>
              <Btn size="sm" variant="ghost" icon title="删除" :loading="busy === `rm:${l.kind}:${m}`" @click="remove(l.kind, m)"><Trash2 /></Btn>
            </li>
          </ul>
        </section>
      </div>
      <Field inline label="任务有效期" hint="xAI 媒体任务提交后保留多久（秒）">
        <NumberInput v-model="d.jobTtlSeconds" :min="1" integer unit="秒" />
      </Field>
      <Field inline label="请求超时" hint="单次 xAI 媒体请求最长等待（秒）">
        <NumberInput v-model="d.requestTimeoutSeconds" :min="1" integer unit="秒" />
      </Field>
    </template>
  </SettingsSection>
</template>

<style scoped>
.lists {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
  align-items: stretch;
}
.ml {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.t {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
}
.t svg {
  width: 14px;
  height: 14px;
  color: var(--text-2);
}
.t .faint {
  font-weight: 400;
}
ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
li {
  display: flex;
  align-items: center;
  gap: 2px;
  min-height: 32px;
  padding-left: 4px;
  border-radius: 6px;
}
li:hover {
  background: var(--fill-3);
}
.nm {
  flex: 1;
  text-align: left;
  font-size: 12.5px;
}
.nm:hover {
  color: var(--accent);
}
.small {
  font-size: 12px;
}
</style>
