<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ShieldCheck } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { toast } from '@/stores/ui'
import { dateTime } from '@/utils/format'
import { errorDetail, errorTitle } from '@/utils/errors'
import { openAccount } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'

// GPT 生图账号：TG「图片设置 → OAuth 账号」的逐个开关。只有 OpenAI OAuth 账号有这个状态。

type State = Schemas['ImageAccountStateData']
const emit = defineEmits<{ changed: [] }>()

const rows = ref<State[] | null>(null)
const error = ref('')
const busy = ref('')

async function load(): Promise<void> {
  try {
    const list = await api.get<{ items: { accountId: string; provider: string }[] }>('/oauth/accounts', { pageSize: 200 })
    const ids = (list.data.items || []).filter((a) => a.provider === 'openai').map((a) => a.accountId)
    const states = await Promise.all(
      ids.map((id) =>
        api
          .get<State>(`/images/accounts/${enc(id)}`)
          .then((r) => r.data)
          .catch(() => null),
      ),
    )
    rows.value = states.filter((s): s is State => Boolean(s))
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  }
}
onMounted(load)
defineExpose({ reload: load })

async function patch(s: State, body: { enabled?: boolean; independentEnabled?: boolean }, ok: string): Promise<void> {
  busy.value = `${s.accountId}:${Object.keys(body)[0]}`
  try {
    const r = await api.patch<State>(`/images/accounts/${enc(s.accountId)}`, body, { ifMatch: s.revision })
    if (rows.value) rows.value = rows.value.map((x) => (x.accountId === s.accountId ? r.data : x))
    toast(ok, 'success')
    emit('changed')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    await load()
  } finally {
    busy.value = ''
  }
}

function cooling(s: State): boolean {
  return Boolean(s.imageCooldownUntil && new Date(s.imageCooldownUntil).getTime() > Date.now())
}
</script>

<template>
  <Card title="GPT 生图账号" subtitle="OpenAI OAuth 账号是否参与图片生成；点账号查看详情" flush>
    <template #icon><ShieldCheck /></template>
    <p v-if="error" class="state">{{ error }}</p>
    <div v-else-if="!rows" class="pad"><Skeleton v-for="i in 2" :key="i" h="40px" r="8px" /></div>
    <Empty v-else-if="!rows.length" title="没有 OpenAI OAuth 账号" detail="在「OAuth 账户」里登录 OpenAI 账号后可用于 GPT 生图。" />
    <ul v-else class="list">
      <li v-for="s in rows" :key="s.accountId">
        <div class="m">
          <p class="n">
            <button type="button" class="lk truncate" :title="s.accountId" @click="openAccount(s.accountId, s.email)">{{ s.email || s.accountId }}</button>
            <Badge v-if="s.effectiveAvailable" tone="green" dot>可生图</Badge>
            <Badge v-else tone="orange" dot>不可生图</Badge>
            <Badge v-if="!s.oauthEnabled">对话已停用</Badge>
            <Badge v-if="cooling(s)" tone="orange">冷却至 {{ dateTime(s.imageCooldownUntil) }}</Badge>
            <Badge v-if="s.missingAccountId" tone="red">缺少账号 ID</Badge>
          </p>
          <p v-if="s.unavailableReason && !s.effectiveAvailable" class="sub">{{ s.unavailableReason }}</p>
        </div>
        <div class="sw">
          <label>
            <span>用于生图</span>
            <Switch
              :model-value="s.imageEnabled"
              :loading="busy === `${s.accountId}:enabled`"
              @update:model-value="(v: boolean) => patch(s, { enabled: v }, v ? '已允许该账号生图' : '已禁止该账号生图')"
            />
          </label>
          <label :title="s.independentAllowed ? '' : '账号认证或额度异常时不能单独授权'">
            <span>对话停用时仍可生图</span>
            <Switch
              :model-value="s.independentEnabled"
              :disabled="!s.independentAllowed && !s.independentEnabled"
              :loading="busy === `${s.accountId}:independentEnabled`"
              @update:model-value="(v: boolean) => patch(s, { independentEnabled: v }, v ? '已单独授权生图' : '已取消单独授权')"
            />
          </label>
        </div>
      </li>
    </ul>
  </Card>
</template>

<style scoped>
.state {
  padding: 30px 16px;
  text-align: center;
  color: var(--text-3);
}
.pad {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 0.5px solid var(--hairline);
}
li {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 16px;
  padding: 10px 16px;
  border-bottom: 0.5px solid var(--hairline);
}
li:last-child {
  border-bottom: 0;
}
.m {
  flex: 1;
  min-width: 220px;
}
.n {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.lk {
  font-weight: 550;
  text-align: left;
  max-width: 100%;
}
.lk:hover {
  color: var(--accent);
}
.sub {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--text-3);
}
.sw {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
}
.sw label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
}
</style>
