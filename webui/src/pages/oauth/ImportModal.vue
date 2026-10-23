<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FileUp, TriangleAlert, Upload } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { openAccount } from '@/links'
import { toast } from '@/stores/ui'
import { clock } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import Segmented from '@/components/ui/Segmented.vue'
import TextArea from '@/components/ui/TextArea.vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import { errorToast } from './oauthApi'
import { providerMeta } from './meta'

type Preview = Schemas['OAuthImportPreviewData']
type Commit = Schemas['OAuthImportCommitData']
type Format = 'openai' | 'cpa' | 'sub2api' | 'zhipu'

const props = defineProps<{ open: boolean; initialFormat?: Format }>()
const emit = defineEmits<{ close: []; done: [] }>()

const FORMATS: { value: Format; label: string; hint: string }[] = [
  { value: 'cpa', label: 'CPA', hint: 'CLIProxyAPI 导出的 JSON 文件，或多个文件打包的 ZIP。' },
  { value: 'sub2api', label: 'Sub2API', hint: 'Sub2API 导出的账号 JSON。' },
  { value: 'openai', label: 'OpenAI', hint: 'Codex / ChatGPT 的 auth.json，或包含 refresh_token 的 JSON。' },
  { value: 'zhipu', label: '智谱', hint: '智谱账号 JSON（site、credential_mode 等字段，见新增账号里的说明）。' },
]

const format = ref<Format>('cpa')
const source = ref<'file' | 'text'>('file')
const text = ref('')
const file = ref<File | null>(null)
const busy = ref(false)
const preview = ref<Preview | null>(null)
const decisions = ref<Record<string, 'keep' | 'overwrite'>>({})
const result = ref<Commit | null>(null)
const dragOver = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

function reset(): void {
  text.value = ''
  file.value = null
  preview.value = null
  result.value = null
  decisions.value = {}
}
watch(
  () => props.open,
  (o) => {
    if (o) {
      reset()
      if (props.initialFormat) format.value = props.initialFormat
    }
  },
)

const formatHint = computed(() => FORMATS.find((f) => f.value === format.value)?.hint || '')
const ready = computed(() => (source.value === 'file' ? Boolean(file.value) : Boolean(text.value.trim())))
const conflicts = computed(() => preview.value?.candidates.filter((c) => c.conflictAccountId) || [])

function pickFile(e: Event): void {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) file.value = f
}
function onDrop(e: DragEvent): void {
  dragOver.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) {
    file.value = f
    source.value = 'file'
  }
}

async function toBase64(f: File): Promise<string> {
  const buf = new Uint8Array(await f.arrayBuffer())
  let bin = ''
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return btoa(bin)
}

async function runPreview(): Promise<void> {
  busy.value = true
  try {
    let body: Record<string, unknown>
    if (source.value === 'file' && file.value) {
      const f = file.value
      const isZip = /\.zip$/i.test(f.name) || f.type.includes('zip')
      body = isZip
        ? { format: format.value, payloadEncoding: 'base64', payload: await toBase64(f), filename: f.name }
        : { format: format.value, payloadEncoding: 'json', payload: await f.text(), filename: f.name }
    } else {
      body = { format: format.value, payloadEncoding: 'json', payload: text.value.trim() }
    }
    const res = await api.post<Preview>('/oauth/imports/preview', body)
    preview.value = res.data
    decisions.value = Object.fromEntries(res.data.candidates.map((c) => [c.candidateId, 'keep' as const]))
  } catch (err) {
    errorToast(err)
  } finally {
    busy.value = false
  }
}

function setAll(action: 'keep' | 'overwrite'): void {
  for (const c of conflicts.value) decisions.value[c.candidateId] = action
}

async function commit(): Promise<void> {
  if (!preview.value) return
  busy.value = true
  try {
    const res = await api.post<Commit>(`/oauth/imports/${enc(preview.value.importId)}/commit`, {
      importSecret: preview.value.importSecret,
      decisions: preview.value.candidates.map((c) => ({ candidateId: c.candidateId, action: decisions.value[c.candidateId] || 'keep' })),
    })
    result.value = res.data
    const r = res.data
    toast('导入完成', 'success', `新增 ${r.added.length} 个，覆盖 ${r.replaced.length} 个，跳过 ${r.skipped.length} 个`)
    emit('done')
  } catch (err) {
    errorToast(err)
  } finally {
    busy.value = false
  }
}

const ERROR_CODES: Record<string, string> = {
  PARSE_FAILED: '文件无法解析',
  INVALID_CANDIDATE: '账号数据不完整',
  PREPARE_FAILED: '凭据校验失败',
}
</script>

<template>
  <Modal :open="open" title="导入账号" subtitle="先预览，确认后再写入；已存在的账号可以选择覆盖或跳过" width="lg" @close="emit('close')">
    <!-- step 3: result -->
    <div v-if="result" class="result">
      <div class="stat"><b class="mono c-green">{{ result.added.length }}</b> 新增</div>
      <div class="stat"><b class="mono c-blue">{{ result.replaced.length }}</b> 覆盖</div>
      <div class="stat"><b class="mono">{{ result.skipped.length }}</b> 跳过</div>
      <ul class="ids">
        <li v-for="id in [...result.added, ...result.replaced]" :key="id">
          <button type="button" class="link mono truncate" @click="openAccount(id)">{{ id }}</button>
        </li>
      </ul>
      <p class="faint small">新账号的模型目录和额度会在后台同步，稍后刷新即可看到。</p>
    </div>

    <!-- step 2: preview -->
    <template v-else-if="preview">
      <p class="lead">
        识别到 <b>{{ preview.candidates.length }}</b> 个账号<template v-if="conflicts.length">，其中 <b class="c-orange">{{ conflicts.length }}</b> 个已存在</template><template v-if="preview.errors.length">，<b class="c-red">{{ preview.errors.length }}</b> 条无法导入</template>。
        预览在 <span class="mono">{{ clock(preview.expiresAt, false) }}</span> 前有效。
      </p>
      <div v-if="conflicts.length > 1" class="bulk">
        已存在的账号：
        <button type="button" @click="setAll('keep')">全部跳过</button>
        <button type="button" @click="setAll('overwrite')">全部覆盖</button>
      </div>
      <ul class="cands">
        <li v-for="c in preview.candidates" :key="c.candidateId">
          <ProviderIcon :provider="c.provider" :size="24" />
          <div class="who">
            <p class="truncate">{{ c.displayName }}</p>
            <p class="faint truncate small">{{ providerMeta(c.provider).label }} · {{ c.identity }}</p>
          </div>
          <template v-if="c.conflictAccountId">
            <Segmented
              v-model="decisions[c.candidateId]"
              :options="[
                { value: 'keep', label: '跳过' },
                { value: 'overwrite', label: '覆盖' },
              ]"
              size="sm"
            />
          </template>
          <Badge v-else tone="green">新增</Badge>
        </li>
      </ul>
      <div v-if="preview.errors.length" class="errs">
        <p class="et"><TriangleAlert /> 无法导入的条目</p>
        <p v-for="(e, i) in preview.errors" :key="i" class="small">
          <span v-if="e.index !== null && e.index !== undefined" class="mono">#{{ e.index + 1 }}</span>
          {{ ERROR_CODES[e.code] || e.code }}<span v-if="e.message" class="faint">：{{ e.message }}</span>
        </p>
      </div>
    </template>

    <!-- step 1: input -->
    <div v-else class="form">
      <Field label="文件格式" :hint="formatHint">
        <Segmented v-model="format" :options="FORMATS.map((f) => ({ value: f.value, label: f.label }))" />
      </Field>
      <Field label="来源">
        <Segmented
          v-model="source"
          :options="[
            { value: 'file', label: '上传文件' },
            { value: 'text', label: '粘贴文本' },
          ]"
          size="sm"
        />
      </Field>
      <div
        v-if="source === 'file'"
        class="drop"
        :class="{ over: dragOver, has: file }"
        @click="fileInput?.click()"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="onDrop"
      >
        <Upload />
        <p v-if="file" class="mono truncate">{{ file.name }}（{{ Math.ceil(file.size / 1024) }} KB）</p>
        <p v-else>点击选择或把文件拖到这里</p>
        <p class="faint small">支持 .json，CPA 可上传 .zip；最大约 1.5 MB</p>
        <input ref="fileInput" type="file" accept=".json,.zip,application/json,application/zip" hidden @change="pickFile" />
      </div>
      <TextArea v-else v-model="text" :rows="9" mono placeholder="把 JSON 内容粘贴到这里" />
    </div>

    <template #footer>
      <template v-if="result">
        <Btn variant="primary" @click="emit('close')">完成</Btn>
      </template>
      <template v-else-if="preview">
        <Btn variant="ghost" :disabled="busy" @click="preview = null">返回修改</Btn>
        <Btn variant="primary" :loading="busy" :disabled="!preview.candidates.length" @click="commit">
          导入 {{ preview.candidates.length }} 个账号
        </Btn>
      </template>
      <template v-else>
        <Btn variant="ghost" @click="emit('close')">取消</Btn>
        <Btn variant="primary" :loading="busy" :disabled="!ready" @click="runPreview"><FileUp /> 预览</Btn>
      </template>
    </template>
  </Modal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.form :deep(.seg) {
  max-width: 100%;
}
.drop {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 26px 16px;
  border-radius: var(--radius-md);
  border: 1px dashed var(--border-strong);
  background: var(--surface-sunken);
  text-align: center;
  font-size: 12.5px;
  cursor: pointer;
  min-width: 0;
}
.drop.over,
.drop.has {
  border-color: var(--accent);
}
.drop svg {
  width: 22px;
  height: 22px;
  color: var(--text-3);
}
.drop p {
  max-width: 100%;
}
.lead {
  margin-bottom: 10px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
}
.bulk {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--text-2);
}
.bulk button,
.link {
  color: var(--accent);
  font-size: 12px;
}
.cands {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 340px;
  overflow-y: auto;
}
.cands li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  border-radius: 8px;
  background: var(--fill-3);
}
.who {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
}
.small {
  font-size: 11.5px;
}
.errs {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--red) 8%, transparent);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.et {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--red);
}
.et svg {
  width: 14px;
  height: 14px;
}
.result {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 24px;
  align-items: baseline;
}
.stat b {
  font-size: 20px;
  margin-right: 4px;
}
.ids {
  width: 100%;
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 220px;
  overflow-y: auto;
}
.ids li {
  min-width: 0;
  display: flex;
}
.link {
  max-width: 100%;
  text-align: left;
}
</style>
