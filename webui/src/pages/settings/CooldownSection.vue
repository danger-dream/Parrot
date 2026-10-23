<script setup lang="ts">
import type { Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import { errorTitle } from '@/utils/errors'
import { humanSeconds } from '@/utils/format'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import NumberList from '@/components/ui/NumberList.vue'

type Cooldown = Schemas['ErrorCooldownSettingsData']
const res = useResource<Cooldown>('/settings/error-cooldown')
const { draft, dirty, saving, loading, error } = res

function caption(v: number, i: number): string {
  if (v === 0) return i === (draft.value?.errorWindows.length ?? 0) - 1 ? '永久冷却，需手动解除' : '永久冷却'
  return `冷却 ${humanSeconds(v * 60)}`
}
function setNum<K extends 'oauthGraceCount' | 'ladderMinIntervalSeconds' | 'permanentMinAgeSeconds'>(key: K, v: number | null): void {
  if (draft.value && v !== null) draft.value[key] = v
}
</script>

<template>
  <SettingsSection
    title="错误冷却"
    description="同一渠道的同一模型连续失败时，按阶梯逐级延长暂停时间；成功一次就立即清零。当前处于冷却中的渠道见页面底部「运行状态」。"
    :dirty="dirty"
    :saving="saving"
    :loading="loading && !draft"
    :error="error ? errorTitle(error) : ''"
    @save="res.save()"
    @reset="res.reset()"
  >
    <template v-if="draft">
      <Field label="冷却阶梯（分钟）" hint="第 N 次连续失败后暂停的分钟数，至少一项；填 0 表示永久冷却（通常放在最后一项）。默认 1, 3, 5, 10, 15, 0。">
        <NumberList
          v-model="draft.errorWindows"
          :min="0"
          integer
          unit="分钟"
          :min-items="1"
          :max-items="12"
          :default-value="5"
          :item-label="(i) => `第 ${i + 1} 次`"
          :caption="caption"
        />
      </Field>
      <Field label="OAuth 宽容次数" hint="OAuth 账号前几次失败只计数、不冷却，避免偶发故障让整个账号的模型都不可用；0 表示第一次失败就冷却（0–100）。默认 3。" inline>
        <NumberInput :model-value="draft.oauthGraceCount" :min="0" :max="100" integer unit="次" @update:model-value="setNum('oauthGraceCount', $event)" />
      </Field>
      <Field label="升级最小间隔" hint="两次升级冷却档位之间至少间隔多久，期间的失败只计数，防止客户端快速重试把渠道打进永久冷却；0 关闭（0–3600 秒）。默认 30 秒。" inline>
        <NumberInput :model-value="draft.ladderMinIntervalSeconds" :min="0" :max="3600" integer unit="秒" @update:model-value="setNum('ladderMinIntervalSeconds', $event)" />
      </Field>
      <Field label="永久冷却门槛" hint="从第一次失败起持续不足这么久时，即使到了最后一档也不进入永久冷却，而是停在倒数第二档；0 关闭（0–86400 秒）。默认 300 秒。" inline>
        <NumberInput :model-value="draft.permanentMinAgeSeconds" :min="0" :max="86400" integer unit="秒" @update:model-value="setNum('permanentMinAgeSeconds', $event)" />
      </Field>
    </template>
  </SettingsSection>
</template>
