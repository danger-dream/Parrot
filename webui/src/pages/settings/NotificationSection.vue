<script setup lang="ts">
import { computed } from 'vue'
import { useResource } from '@/composables/useResource'
import { errorTitle } from '@/utils/errors'
import SettingsSection from '@/components/ui/SettingsSection.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'

// Telegram notification switches. Mirrors the TG menu: the canonical event catalog plus the
// four retained system events. Older per-event keys that the backend still returns are only
// fallbacks for unset events, so (like TG) they are not listed to avoid duplicate titles.
type Notifications = { enabled: boolean; events: Record<string, boolean>; revision?: string }
const res = useResource<Notifications>('/settings/notifications')
const { draft, dirty, saving, loading, error } = res

interface EventDef {
  key: string
  label: string
  hint: string
}
const GROUPS: { title: string; items: EventDef[] }[] = [
  {
    title: '账号授权',
    items: [
      { key: 'accountAuthRefreshed', label: '授权已续期', hint: 'OAuth 账号自动续期成功时通知。' },
      { key: 'accountAuthRefreshFailed', label: '授权续期失败', hint: '续期失败、账号可能需要重新登录时通知。' },
      { key: 'accountAuthRecovered', label: '授权已恢复', hint: '之前续期失败的账号恢复正常时通知。' },
    ],
  },
  {
    title: '额度',
    items: [
      { key: 'accountQuotaBlocked', label: '额度达到暂停线', hint: '账号用量达到阈值、被暂停调用时通知。' },
      { key: 'accountQuotaRecovered', label: '额度已恢复', hint: '被暂停的账号额度重置、恢复调用时通知。' },
      { key: 'modelQuotaBlocked', label: '模型额度已暂停', hint: '某个模型或额度池用尽、被暂停时通知。' },
      { key: 'modelQuotaRecovered', label: '模型额度已恢复', hint: '被暂停的模型额度恢复时通知。' },
      { key: 'accountCreditsStarted', label: '套餐额度用完，开始消耗积分', hint: '账号开始消耗额外积分时通知，可及时决定是否暂停。' },
    ],
  },
  {
    title: '账号与权益',
    items: [
      { key: 'accountActionAttention', label: '账户操作未完成', hint: '签到、领取等账户操作失败或结果未知时通知。' },
      { key: 'accountBenefitGranted', label: '权益已领取', hint: '账号成功领取权益时通知。' },
      { key: 'accountCatalogChanged', label: '模型目录有变化', hint: '账号可用的模型列表有新增或移除时通知。' },
    ],
  },
  {
    title: '模型与渠道',
    items: [
      { key: 'requestModelChanged', label: '上游返回的模型与请求不同', hint: '上游实际使用的模型与请求的不一致时通知。' },
      { key: 'requestModelConflict', label: '上游模型信息不一致', hint: '多个来源给出的实际模型互相矛盾时通知。' },
      { key: 'channelModelFrozen', label: '模型已永久冻结', hint: '某个渠道的模型进入永久冷却时通知。' },
      { key: 'channelModelRecovered', label: '模型限制已解除', hint: '被冻结的渠道模型恢复可用时通知。' },
      { key: 'modelEntitlementBlocked', label: '套餐不含此模型', hint: '账号套餐不支持某个模型、暂时跳过时通知。' },
      { key: 'modelEntitlementRecovered', label: '套餐模型限制已解除', hint: '上述套餐限制解除时通知。' },
      { key: 'routingUnavailable', label: '无可用渠道', hint: '某个模型找不到任何可用渠道、请求失败时通知。' },
    ],
  },
  {
    title: '系统',
    items: [
      { key: 'systemResponsesStoreFailed', label: 'Responses 历史保存失败', hint: '对话历史保存失败（可能磁盘满或无权限）时通知。' },
      { key: 'operationCompleted', label: '操作已完成', hint: '后台批量操作完成时发送回执。' },
      { key: 'operationAttention', label: '操作部分未完成', hint: '后台操作部分失败、需要处理时通知。' },
      { key: 'statusAlert', label: '上游官网状态', hint: 'Claude / OpenAI / Cloudflare 官网发布故障事件时通知（详见「状态告警」）。' },
      { key: 'appUpdate', label: '应用更新', hint: 'Parrot 有新版本可用时通知。' },
      { key: 'networkMonitor', label: '网络健康', hint: '网络监控发现 DNS、代理或上游不可达时通知。' },
      { key: 'modelRegistry', label: '模型命名变化', hint: '上游模型命名或注册信息变化时通知。' },
      { key: 'translationFailed', label: '翻译层连续失败', hint: '翻译模型连续多次调用失败时通知，可及时换用可用的模型。' },
    ],
  },
]

// Fallback keys of the pre-catalog scheme; hidden on purpose (see comment above).
const LEGACY = new Set([
  'oauthRefreshed', 'oauthRefreshFailed', 'quotaDisabled', 'quotaResumed', 'quotaCooldown', 'openaiCreditsStarted',
  'workbuddyActionFailed', 'modelDegraded', 'channelPermanent', 'channelRecovered', 'planExcluded', 'noChannels',
  'openaiStoreSaveFailed',
])
const known = new Set(GROUPS.flatMap((g) => g.items.map((i) => i.key)))
const groups = computed(() => {
  const ev = draft.value?.events || {}
  const out = GROUPS.map((g) => ({ ...g, items: g.items.filter((i) => i.key in ev) })).filter((g) => g.items.length)
  const extra = Object.keys(ev).filter((k) => !known.has(k) && !LEGACY.has(k))
  if (extra.length) out.push({ title: '其他事件', items: extra.map((k) => ({ key: k, label: k, hint: '新增的通知事件。' })) })
  return out
})
const onCount = computed(() => groups.value.reduce((n, g) => n + g.items.filter((i) => draft.value?.events[i.key]).length, 0))
const total = computed(() => groups.value.reduce((n, g) => n + g.items.length, 0))

// Send only the switches that actually changed, so untouched events keep their stored form.
function save(): void {
  const cur = res.data.value
  const d = draft.value
  if (!cur || !d) return
  const body: { enabled?: boolean; events?: Record<string, boolean> } = {}
  if (d.enabled !== cur.enabled) body.enabled = d.enabled
  const events: Record<string, boolean> = {}
  for (const [k, v] of Object.entries(d.events)) if (cur.events[k] !== v) events[k] = v
  if (Object.keys(events).length) body.events = events
  if (Object.keys(body).length) res.save(body)
}

function setGroup(items: EventDef[], on: boolean): void {
  if (!draft.value) return
  for (const i of items) draft.value.events[i.key] = on
}
</script>

<template>
  <SettingsSection
    title="通知"
    description="通过 Telegram Bot 推送的事件提醒。总开关关闭时所有事件都不发送；单个事件关闭只影响该类提醒。"
    :dirty="dirty"
    :saving="saving"
    :loading="loading && !draft"
    :error="error ? errorTitle(error) : ''"
    @save="save"
    @reset="res.reset()"
  >
    <template v-if="draft">
      <Field label="通知总开关" :hint="`已开启 ${onCount}/${total} 类事件。`" inline>
        <Switch v-model="draft.enabled" label="通知总开关" />
      </Field>
      <div class="groups" :class="{ off: !draft.enabled }">
        <div v-for="g in groups" :key="g.title" class="grp">
          <div class="gh">
            <span>{{ g.title }}</span>
            <button type="button" @click="setGroup(g.items, true)">全开</button>
            <button type="button" @click="setGroup(g.items, false)">全关</button>
          </div>
          <Field v-for="i in g.items" :key="i.key" :label="i.label" :hint="i.hint" inline>
            <Switch v-model="draft.events[i.key]" :label="i.label" />
          </Field>
        </div>
      </div>
    </template>
  </SettingsSection>
</template>

<style scoped>
.groups {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 4px 28px;
  transition: opacity 0.2s;
}
.groups.off {
  opacity: 0.55;
}
.grp {
  min-width: 0;
}
.gh {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
  padding-bottom: 4px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-3);
  border-bottom: 0.5px solid var(--hairline);
}
.gh span {
  margin-right: auto;
}
.gh button {
  font-size: 11.5px;
  font-weight: 400;
  color: var(--accent);
  min-height: 24px;
}
@container (max-width: 520px) {
  .groups {
    grid-template-columns: 1fr;
  }
}
</style>
