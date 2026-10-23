<script setup lang="ts">
import { bytes, pct } from '@/utils/format'
import { computed, onMounted, ref } from 'vue'
import { api, type Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import { errorTitle } from '@/utils/errors'
import { openModel } from '@/links'
import Field from '@/components/ui/Field.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Select from '@/components/ui/Select.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Switch from '@/components/ui/Switch.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { PROVIDER_NAME } from './labels'

type Settings = Schemas['ImageSettingsData']

const props = defineProps<{ kind: 'image' | 'video'; modelChoices: string[] }>()
const noun = computed(() => (props.kind === 'image' ? '图片' : '视频'))

const res = useResource<Settings>(props.kind === 'image' ? '/images/settings' : '/videos/settings', {
  successText: props.kind === 'image' ? '图片设置已保存' : '视频设置已保存',
})
// getMediaStatistics (round 2) is not in the generated schema yet.
interface ModelStat {
  model: string
  completedCalls: number
  generatedCount: number
  unknownCountCalls: number
  recordedBytes: number
  unknownBytesCalls: number
}
interface Statistics {
  cache: { files: number; bytes: number }
  models: ModelStat[]
}
const stats = ref<Statistics | null>(null)
const statsError = ref('')
async function loadStats(): Promise<void> {
  try {
    stats.value = (await api.get<Statistics>(`/media/${props.kind}/statistics`)).data
    statsError.value = ''
  } catch (err) {
    statsError.value = errorTitle(err)
  }
}
onMounted(loadStats)
defineExpose({
  reload: () => {
    res.load()
    loadStats()
  },
})
const statOf = (m: string) => stats.value?.models.find((x) => x.model === m)
/** Models that produced media but are no longer configured. */
const retired = computed(() => {
  const listed = new Set(Object.values(res.data.value?.models || {}).flat())
  return (stats.value?.models || []).filter((x) => !listed.has(x.model))
})
const unit = computed(() => (props.kind === 'image' ? '张' : '段'))
function statText(st: ModelStat | undefined): string {
  if (!st || !st.completedCalls) return '尚未生成'
  const parts: string[] = []
  if (st.generatedCount) parts.push(`已生成 ${st.generatedCount} ${unit.value}`)
  if (st.unknownCountCalls) parts.push(st.generatedCount ? `另有 ${st.unknownCountCalls} 次未记录数量` : `完成 ${st.completedCalls} 次（未记录数量）`)
  if (st.recordedBytes) parts.push(`已记录 ${bytes(st.recordedBytes)}`)
  return parts.join(' · ')
}

const d = computed(() => res.draft.value)

const MB = 1024 * 1024
const maxMb = computed({
  get: () => (d.value ? Math.round((d.value.cacheMaxBytes / MB) * 10) / 10 : null),
  set: (v: number | null) => {
    if (d.value && v !== null) d.value.cacheMaxBytes = Math.round(v * MB)
  },
})
const ttlHours = computed({
  get: () => (d.value?.jobTtlSeconds ? Math.round((d.value.jobTtlSeconds / 3600) * 100) / 100 : null),
  set: (v: number | null) => {
    if (d.value && v !== null) d.value.jobTtlSeconds = Math.max(1, Math.round(v * 3600))
  },
})

const defaultOptions = computed(() => {
  const set = new Set(props.modelChoices)
  if (d.value?.defaultModel) set.add(d.value.defaultModel)
  return [{ value: '', label: '自动（按可用列表选择）' }, ...[...set].sort().map((m) => ({ value: m, label: m }))]
})

const providers = computed(() => Object.entries(res.data.value?.models || {}).filter(([, list]) => list.length))

function sizeText(n: number): string {
  return n ? bytes(n) : '不限'
}
</script>

<template>
  <SettingsSection
    :title="`${noun}设置`"
    :description="kind === 'image' ? '图片生成 / 编辑接口与缓存。' : '视频生成接口、任务有效期与缓存。'"
    :loading="res.loading.value && !d"
    :error="res.error.value && !d ? errorTitle(res.error.value) : undefined"
    :dirty="res.dirty.value"
    :saving="res.saving.value"
    @save="res.save()"
    @reset="res.reset()"
  >
    <template v-if="d">
      <Field inline :label="`${noun}接口`" :hint="`关闭后客户端无法调用${noun}生成`">
        <Switch v-model="d.enabled" />
      </Field>
      <Field inline label="默认模型" hint="客户端没有指定模型时使用；它不可用时自动回落到可用列表">
        <Select v-model="d.defaultModel" width="230px" :options="defaultOptions" />
      </Field>
      <Field inline label="请求超时" :hint="`单次${noun}请求最长等待`">
        <NumberInput v-model="d.requestTimeoutSeconds" :min="1" integer unit="秒" />
      </Field>
      <Field v-if="kind === 'video'" inline label="任务有效期" hint="视频任务提交后多久内可以查询结果">
        <NumberInput v-model="ttlHours" :min="0.01" unit="小时" />
      </Field>
      <p class="grp">缓存</p>
      <Field inline :label="`缓存生成的${noun}`" hint="开启后可以在「媒体日志」里预览和下载产物">
        <Switch v-model="d.cacheEnabled" />
      </Field>
      <Field inline label="缓存目录" hint="相对数据目录；修改不会移动或删除已有文件">
        <TextInput v-model="d.cachePath" width="230px" mono />
      </Field>
      <Field inline label="保留天数" hint="0 表示永久保留">
        <NumberInput v-model="d.cacheRetentionDays" :min="0" :max="36500" integer unit="天" />
      </Field>
      <Field inline label="容量上限" :hint="`当前 ${sizeText(res.data.value?.cacheMaxBytes ?? 0)}；0 表示不限`">
        <NumberInput v-model="maxMb" :min="0" unit="MB" />
      </Field>
      <div class="usage">
        <span class="faint">当前缓存占用</span>
        <span v-if="statsError" class="faint">{{ statsError }}</span>
        <span v-else-if="!stats" class="faint">读取中…</span>
        <template v-else>
          <span class="num">{{ stats.cache.files }} 个文件 · {{ bytes(stats.cache.bytes) }}</span>
          <span v-if="res.data.value?.cacheMaxBytes" class="bar" :title="`占容量上限 ${pct(stats.cache.bytes / res.data.value.cacheMaxBytes)}`">
            <i :style="{ width: `${Math.min(100, (stats.cache.bytes / res.data.value.cacheMaxBytes) * 100)}%` }" />
          </span>
        </template>
      </div>
      <p class="grp">可用模型</p>
      <p v-if="!providers.length" class="faint small">暂无已配置的{{ noun }}模型。</p>
      <div v-for="[p, list] in providers" :key="p" class="prov">
        <span class="pn">{{ PROVIDER_NAME[p] || p }}</span>
        <ul class="mlist">
          <li v-for="m in list" :key="m">
            <button type="button" class="mn mono truncate" :class="{ def: m === res.data.value?.defaultModel }" @click="openModel(m)">{{ m }}</button>
            <span v-if="m === res.data.value?.defaultModel" class="dt">默认</span>
            <span class="ms" :class="{ none: !statOf(m)?.completedCalls }">{{ statText(statOf(m)) }}</span>
          </li>
        </ul>
      </div>
      <div v-if="retired.length" class="prov">
        <span class="pn">已移除</span>
        <ul class="mlist">
          <li v-for="st in retired" :key="st.model">
            <button type="button" class="mn mono truncate" @click="openModel(st.model)">{{ st.model }}</button>
            <span class="ms">{{ statText(st) }}</span>
          </li>
        </ul>
      </div>
      <p class="faint small note">模型名称由配置维护；xAI 的图片 / 视频模型可以在下方「xAI 媒体模型」里增删改名。</p>
    </template>
  </SettingsSection>
</template>

<style scoped>
.grp {
  margin-top: 12px;
  font-size: 11px;
  font-weight: 650;
  color: var(--text-3);
}
.small {
  font-size: 12px;
}
.prov {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 6px 0;
}
.pn {
  width: 76px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-2);
  line-height: 24px;
}
.mlist {
  list-style: none;
  margin: 0;
  padding: 0;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.mlist li {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px 8px;
  min-height: 24px;
  min-width: 0;
}
.mn {
  font-size: 12px;
  text-align: left;
  max-width: 100%;
}
.mn:hover {
  color: var(--accent);
}
.mn.def {
  color: var(--accent);
}
.dt {
  font-size: 10px;
  padding: 0 5px;
  border-radius: 4px;
  color: var(--accent);
  background: var(--accent-soft);
}
.ms {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
}
.ms.none {
  color: var(--text-3);
}
.usage {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  padding: 10px 0 2px;
  font-size: 12px;
}
.bar {
  flex: 1;
  min-width: 80px;
  height: 5px;
  border-radius: 999px;
  background: var(--fill-2);
  overflow: hidden;
}
.bar i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--teal), var(--blue));
}
.note {
  margin: 6px 0 8px;
}
</style>
