<script setup lang="ts">
import type { Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import { errorTitle } from '@/utils/errors'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Switch from '@/components/ui/Switch.vue'
import NumberList from '@/components/ui/NumberList.vue'

type Retry = Schemas['RetrySettingsData']
type ErrKey = keyof Retry['transient']['errors']
type RecKey = keyof Retry['recovery']

const res = useResource<Retry>('/settings/retry')
const { draft, dirty, saving, loading, error } = res

const ERRORS: { key: ErrKey; label: string; hint: string }[] = [
  { key: 'openaiServerOverloaded', label: 'OpenAI 服务过载', hint: 'OpenAI 返回“服务器繁忙/过载”时加试。' },
  { key: 'openaiServerError', label: 'OpenAI 服务端错误', hint: 'OpenAI 返回内部服务错误时加试。' },
  { key: 'claudeOverloaded', label: 'Claude 过载', hint: 'Claude 返回过载（HTTP 529）时加试。' },
  { key: 'xaiUnavailable', label: 'xAI 暂不可用', hint: 'xAI 返回服务暂不可用（HTTP 503）时加试。' },
]
const RECOVERY: { key: RecKey; label: string; hint: string }[] = [
  { key: 'oauthRefresh', label: 'OAuth 授权失效自动刷新', hint: '账号返回 401/403 时，刷新授权后在原账号再试 1 次；仍失败则换下一个渠道。' },
  { key: 'invalidEncryptedContent', label: '修复失效的加密上下文', hint: '请求中的加密推理内容已失效时，移除后整体再试 1 次。' },
  { key: 'claudeContext1mFallback', label: 'Claude 1M 上下文回退', hint: '账号没有 1M 上下文权限时，当前渠道改用普通上下文再试 1 次。' },
]
</script>

<template>
  <SettingsSection
    title="重试"
    description="只在客户端收到第一段内容之前生效：当前渠道失败后会按调度顺序尝试其他渠道，所有候选都失败才返回错误。下面的设置决定遇到临时错误时是否先在原渠道再试几次。"
    :dirty="dirty"
    :saving="saving"
    :loading="loading && !draft"
    :error="error ? errorTitle(error) : ''"
    @save="res.save()"
    @reset="res.reset()"
  >
    <template v-if="draft">
      <Field label="瞬时错误加试" hint="遇到上游临时错误时，先在同一渠道再试几次，再换下一个渠道。关闭后直接换渠道。" inline>
        <Switch v-model="draft.transient.enabled" label="瞬时错误加试" />
      </Field>
      <Field label="额外加试次数" hint="整个请求共享的加试机会（1–5 次），不会按渠道数量倍增；排队等待不计入。" inline>
        <NumberInput
          :model-value="draft.transient.maxExtraAttempts"
          :min="1"
          :max="5"
          integer
          unit="次"
          @update:model-value="$event !== null && (draft!.transient.maxExtraAttempts = $event)"
        />
      </Field>
      <Field label="加试前等待" hint="第 N 次加试前等待的秒数（1–5 项，每项 0–60 秒），实际另加 0–0.25 秒随机抖动；项数不足时沿用最后一项。">
        <NumberList
          v-model="draft.transient.backoffSeconds"
          :min="0"
          :max="60"
          unit="秒"
          :min-items="1"
          :max-items="5"
          :default-value="1"
          :item-label="(i) => `第 ${i + 1} 次`"
        />
      </Field>

      <p class="group">哪些错误会触发加试</p>
      <Field v-for="e in ERRORS" :key="e.key" :label="e.label" :hint="e.hint" inline>
        <Switch v-model="draft.transient.errors[e.key]" :label="e.label" />
      </Field>

      <p class="group">授权与请求修复（每类最多 1 次，独立计数）</p>
      <Field v-for="r in RECOVERY" :key="r.key" :label="r.label" :hint="r.hint" inline>
        <Switch v-model="draft.recovery[r.key]" :label="r.label" />
      </Field>
    </template>
  </SettingsSection>
</template>

<style scoped>
.group {
  margin: 14px 0 0;
  padding-bottom: 4px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 0.5px solid var(--hairline);
}
</style>
