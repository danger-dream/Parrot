<script setup lang="ts">
import { TriangleAlert } from '@lucide/vue'
import type { Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import { confirmAction } from '@/stores/dialogs'
import { errorTitle } from '@/utils/errors'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'

type WebUi = Schemas['WebUiSettingsData']
const res = useResource<WebUi>('/settings/web-ui')
const { draft, data, dirty, saving, loading, error } = res

async function save(): Promise<void> {
  if (!draft.value) return
  if (data.value?.enabled && !draft.value.enabled) {
    const ok = await confirmAction({
      title: '关闭网页控制台？',
      detail:
        '保存后 /ui 网页会立即无法访问，包括你正在看的这个页面。\n\n' +
        '之后只能通过 Telegram Bot「系统设置 → WebUI」，或修改配置文件中的 management.webUi.enabled 重新开启。',
      confirmText: '确认关闭',
      danger: true,
      requireText: '关闭网页控制台',
    })
    if (!ok) return
  }
  await res.save()
}
</script>

<template>
  <SettingsSection
    title="网页控制台"
    description="控制本网页（/ui）是否可以访问。登录需要在 Telegram 中批准或使用管理密钥。"
    :dirty="dirty"
    :saving="saving"
    :loading="loading && !draft"
    :error="error ? errorTitle(error) : ''"
    @save="save"
    @reset="res.reset()"
  >
    <template v-if="draft">
      <Field label="启用网页控制台" hint="关闭后网页将无法访问，只能通过 Telegram 或配置文件重新打开。" inline>
        <Switch v-model="draft.enabled" label="启用网页控制台" />
      </Field>
      <p v-if="data?.enabled && !draft.enabled" class="warn">
        <TriangleAlert /> 保存后当前网页会立即断开，且无法从网页重新开启。
      </p>
    </template>
  </SettingsSection>
</template>

<style scoped>
.warn {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--red);
  background: color-mix(in srgb, var(--red) 9%, transparent);
}
.warn svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
</style>
