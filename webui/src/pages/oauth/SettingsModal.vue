<script setup lang="ts">
import { computed, watch } from 'vue'
import type { Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Switch from '@/components/ui/Switch.vue'
import { errorTitle } from '@/utils/errors'

type OAuthSettings = Schemas['OAuthSettingsData']
type TgPrefs = Schemas['TelegramOAuthPreferencesData']

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const settings = useResource<OAuthSettings>('/oauth/settings', { immediate: false, successText: 'OAuth 设置已保存' })
const prefs = useResource<TgPrefs>('/preferences/telegram/oauth', { immediate: false, successText: 'Telegram 显示偏好已保存' })

watch(
  () => props.open,
  (o) => {
    if (o) {
      settings.load()
      prefs.load()
    }
  },
  { immediate: true },
)

const s = computed(() => settings.draft.value)
const p = computed(() => prefs.draft.value)

function close(): void {
  if (settings.dirty.value) settings.reset()
  if (prefs.dirty.value) prefs.reset()
  emit('close')
}
</script>

<template>
  <Modal :open="open" title="OAuth 设置" subtitle="对全部 OAuth 账号生效" width="md" @close="close">
    <div class="stack">
      <SettingsSection
        title="额度监控与请求伪装"
        description="定时检查每个账号的官方额度，达到阈值自动暂停，恢复后自动启用。"
        :dirty="settings.dirty.value"
        :saving="settings.saving.value"
        :loading="settings.loading.value && !s"
        :error="settings.error.value ? errorTitle(settings.error.value) : ''"
        @save="settings.save()"
        @reset="settings.reset()"
      >
        <template v-if="s">
          <Field label="额度监控" hint="开启后按间隔拉取每个账号的用量；任一额度窗口达到阈值就暂停该账号，重置后低于阈值会自动恢复。" inline>
            <Switch v-model="s.quotaMonitor.enabled" label="额度监控" />
          </Field>
          <Field label="检查间隔" hint="10 ~ 86400 秒。建议不少于 60 秒，过于频繁可能触发上游风控。" inline>
            <NumberInput v-model="s.quotaMonitor.intervalSeconds" :min="10" :max="86400" integer unit="秒" />
          </Field>
          <Field label="暂停阈值" hint="已用比例达到这个值就暂停账号，常见 90 / 95 / 98。" inline>
            <NumberInput v-model="s.quotaMonitor.thresholdPercent" :min="1" :max="100" unit="%" />
          </Field>
          <Field label="CCH 模式" hint="以 Claude Code 客户端的方式发送 Claude 请求。" inline>
            <Segmented
              v-model="s.cchMode"
              :options="[
                { value: 'disabled', label: '关闭' },
                { value: 'dynamic', label: '动态伪装' },
              ]"
              size="sm"
            />
          </Field>
          <Field label="Antigravity 指纹伪装" hint="Antigravity 请求使用与官方客户端一致的 TLS 指纹。" inline>
            <Switch v-model="s.antigravityTlsFingerprintEnabled" label="Antigravity 指纹伪装" />
          </Field>
        </template>
      </SettingsSection>

      <SettingsSection
        title="Telegram 显示偏好"
        description="只影响 Telegram 机器人里账号列表的显示，不影响网页和调度。"
        :dirty="prefs.dirty.value"
        :saving="prefs.saving.value"
        :loading="prefs.loading.value && !p"
        :error="prefs.error.value ? errorTitle(prefs.error.value) : ''"
        @save="prefs.save()"
        @reset="prefs.reset()"
      >
        <template v-if="p">
          <Field label="用量显示口径" hint="额度窗口显示已经用掉多少，还是还剩多少。" inline>
            <Segmented
              v-model="p.usageDisplayMode"
              :options="[
                { value: 'used', label: '已用量' },
                { value: 'remaining', label: '剩余量' },
              ]"
              size="sm"
            />
          </Field>
          <Field label="黑白进度条" hint="在额度百分比后面附加 █░ 字符进度条。" inline>
            <Switch v-model="p.quotaProgressBar" label="黑白进度条" />
          </Field>
        </template>
      </SettingsSection>
    </div>
  </Modal>
</template>

<style scoped>
.stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
