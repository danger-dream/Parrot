<script setup lang="ts">
import { computed } from 'vue'
import { MapPin, TriangleAlert } from '@lucide/vue'
import type { Schemas } from '@/api/client'
import { useResource } from '@/composables/useResource'
import { confirmAction } from '@/stores/dialogs'
import { errorTitle } from '@/utils/errors'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'
import TextInput from '@/components/ui/TextInput.vue'
import Btn from '@/components/ui/Btn.vue'

type WebUi = Schemas['WebUiSettingsData']
const res = useResource<WebUi>('/settings/web-ui')
const { draft, data, dirty, saving, loading, error } = res

// Same rule as the server: an absolute http(s) address without credentials, query or fragment.
const urlInvalid = computed(() => {
  const value = draft.value?.publicUrl?.trim()
  if (!value) return false
  try {
    const u = new URL(value)
    return !/^https?:$/.test(u.protocol) || !u.hostname || Boolean(u.username || u.password || u.search || u.hash)
  } catch {
    return true
  }
})

function setUrl(value: string): void {
  if (draft.value) draft.value.publicUrl = value
}
function useCurrent(): void {
  setUrl(new URL(import.meta.env.BASE_URL, window.location.origin).href)
}

async function save(): Promise<void> {
  if (!draft.value || urlInvalid.value) return
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
      <Field label="访问地址" hint="Telegram 主菜单和「系统设置 → WebUI」里显示的控制台链接。留空时按公网 IP 和端口推算；经域名或反向代理访问时，填写实际打开的地址。">
        <div class="url-row">
          <TextInput :model-value="draft?.publicUrl ?? ''" type="url" mono clearable :invalid="urlInvalid" placeholder="例如 https://example.com/ui/" @update:model-value="setUrl" />
          <Btn @click="useCurrent()"><MapPin /> 使用当前地址</Btn>
        </div>
        <p v-if="urlInvalid" class="warn"><TriangleAlert /> 需要以 http:// 或 https:// 开头的完整地址，不能带账号密码、? 或 #。</p>
      </Field>
    </template>
  </SettingsSection>
</template>

<style scoped>
.url-row {
  display: flex;
  gap: 8px;
}
.url-row > :first-child {
  flex: 1;
  min-width: 0;
}
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
