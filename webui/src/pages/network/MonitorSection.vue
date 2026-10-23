<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Info, Play } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import type { Operation } from '@/api/operations'
import { useResource } from '@/composables/useResource'
import { channelName, refreshCatalog } from '@/stores/catalog'
import { toast } from '@/stores/ui'
import { errorTitle } from '@/utils/errors'
import { humanSeconds } from '@/utils/format'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Btn from '@/components/ui/Btn.vue'
import OpProgress from '@/components/ui/OpProgress.vue'
import EntityLink from '@/components/ui/EntityLink.vue'
import { useOperation } from '@/composables/useOperation'
import ChecksList from './ChecksList.vue'

type Monitor = Schemas['NetworkMonitorSettingsData']
type Check = Schemas['NetworkCheckData']
const emit = defineEmits<{ ran: [] }>()

const res = useResource<Monitor>('/network/monitor')
const { draft, dirty, saving, loading, error } = res
onMounted(() => refreshCatalog().catch(() => undefined))

const CORE: { key: keyof Monitor['core']; label: string; hint: string }[] = [
  { key: 'claude', label: 'Claude', hint: '检测能否连上 Anthropic 官方接口。' },
  { key: 'openai', label: 'OpenAI', hint: '检测能否连上 OpenAI 官方接口。' },
  { key: 'cloudflare', label: 'Cloudflare', hint: '检测能否连上 Cloudflare，用来判断整体出网是否正常。' },
]

const channelOptions = computed(() =>
  Object.keys(draft.value?.channels.byChannel || {}).map((id) => ({ value: id, label: channelName(id), hint: id })),
)
const selected = computed({
  get: () => Object.entries(draft.value?.channels.byChannel || {}).filter(([, v]) => v).map(([k]) => k),
  set: (ids: string[]) => {
    if (!draft.value) return
    const on = new Set(ids)
    for (const k of Object.keys(draft.value.channels.byChannel)) draft.value.channels.byChannel[k] = on.has(k)
  },
})

const runner = useOperation()
const runPreviewOnly = ref(false)
const masterOff = computed(() => Boolean(res.data.value && !res.data.value.enabled))
const lastRun = computed<Check[]>(() => {
  const r = runner.op.value?.status === 'succeeded' ? (runner.op.value.result as { checks?: Check[] } | null) : null
  return (r?.checks || []).map((c) => ({ ...c, latencyMilliseconds: c.latencyMilliseconds ?? null, checkedAt: c.checkedAt ?? null }))
})
async function runNow(): Promise<void> {
  // Snapshot: a run with the master switch off is display-only (R2-6).
  runPreviewOnly.value = Boolean(res.data.value && !res.data.value.enabled)
  if (dirty.value) toast('有未保存的修改', 'warning', '本次检测按已保存的设置执行。')
  const op = await runner.run(() => api.post<Operation>('/network/monitor/actions/run'))
  if (op?.status === 'succeeded') {
    const checks = (op.result as { checks?: Check[] } | null)?.checks || []
    const bad = checks.filter((c) => !c.ok).length
    if (!checks.length) toast('没有执行任何检测项', 'warning', '请至少开启一项检测目标。')
    else
      toast(
        bad ? `检测完成，${bad} 项异常` : `检测完成，${checks.length} 项全部正常`,
        bad ? 'warning' : 'success',
        runPreviewOnly.value ? '总开关关闭，这次结果只在这里显示，没有写入检测记录，也没有发送通知。' : undefined,
      )
    if (!runPreviewOnly.value) emit('ran')
  }
}
</script>

<template>
  <SettingsSection
    title="网络监控"
    description="按间隔自动检测 DNS、代理、官方上游和所选 API 渠道的连通性；发现异常时通过 Telegram 通知（需在系统设置 → 通知中开启「网络健康」）。只做连接测试，不消耗 Token。"
    :dirty="dirty"
    :saving="saving"
    :loading="loading && !draft"
    :error="error ? errorTitle(error) : ''"
    @save="res.save()"
    @reset="res.reset()"
  >
    <template #actions>
      <Btn size="sm" variant="primary" :loading="runner.running.value" @click="runNow"><Play /> 立即运行</Btn>
    </template>
    <template v-if="draft">
      <Field label="启用自动监控" hint="总开关。关闭后不再定时检测；仍可「立即运行」查看结果，但结果不写入检测记录、不发送通知。" inline>
        <Switch v-model="draft.enabled" label="启用自动监控" />
      </Field>
      <Field label="检查间隔" :hint="`两次自动检测之间的间隔，至少 5 秒。当前约 ${humanSeconds(draft.intervalSeconds)}。`" inline>
        <NumberInput :model-value="draft.intervalSeconds" :min="5" integer unit="秒" @update:model-value="$event !== null && (draft!.intervalSeconds = $event)" />
      </Field>
      <Field label="检测 DNS" hint="用当前 DNS 设置解析常用域名，确认解析正常。" inline>
        <Switch v-model="draft.dns" label="检测 DNS" />
      </Field>

      <p class="group">核心目标</p>
      <Field v-for="c in CORE" :key="c.key" :label="c.label" :hint="c.hint" inline>
        <Switch v-model="draft.core[c.key]" :label="c.label" />
      </Field>

      <p class="group">API 渠道</p>
      <Field label="检测 API 渠道" hint="对勾选的 API 渠道做 TCP 连接测试（OAuth 账号有单独的状态监控，不在这里）。" inline>
        <Switch v-model="draft.channels.enabled" label="检测 API 渠道" />
      </Field>
      <div class="chs" :class="{ off: !draft.channels.enabled }">
        <CheckList v-model="selected" :options="channelOptions" max-height="220px" empty-text="没有 API 渠道" />
        <p v-if="selected.length" class="picked">
          已选：
          <template v-for="(id, i) in selected" :key="id">
            <EntityLink kind="channel" :id="id" /><span v-if="i < selected.length - 1" class="sep">、</span>
          </template>
        </p>
      </div>

      <p v-if="masterOff" class="note"><Info /> 总开关关闭时，「立即运行」仍会按上面勾选的检测项执行一次，但结果只在本页显示：不写入检测记录，也不发送通知。</p>
      <div v-if="runner.op.value" class="run">
        <OpProgress :op="runner.op.value" title="正在检测网络" :done-text="runPreviewOnly ? '检测完成（仅本次显示，未写入记录）' : '检测完成，已写入检测记录'" />
        <ChecksList v-if="lastRun.length" :items="lastRun" compact />
      </div>
    </template>
  </SettingsSection>
</template>

<style scoped>
.note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-2);
  background: var(--fill-3);
}
.note svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--accent);
}
.group {
  margin: 14px 0 0;
  padding-bottom: 4px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 0.5px solid var(--hairline);
}
.chs {
  margin: 4px 0 6px;
  transition: opacity 0.2s;
}
.chs.off {
  opacity: 0.55;
}
.picked {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-3);
}
.sep {
  color: var(--text-3);
}
.run {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 14px;
}
</style>
