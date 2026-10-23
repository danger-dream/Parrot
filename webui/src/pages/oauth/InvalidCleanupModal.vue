<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CircleCheck, Trash2 } from '@lucide/vue'
import { api, type Schemas } from '@/api/client'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { clock } from '@/utils/format'
import Btn from '@/components/ui/Btn.vue'
import CheckList from '@/components/ui/CheckList.vue'
import Empty from '@/components/ui/Empty.vue'
import Modal from '@/components/ui/Modal.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { errorToast, fetchAllPages } from './oauthApi'
import { providerMeta, reasonLabel, type AccountSummary } from './meta'
import { errorTitle } from '@/utils/errors'

type Plan = Schemas['OAuthDeletionPlanData']

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; done: [] }>()

const items = ref<AccountSummary[]>([])
const loading = ref(false)
const error = ref('')
const selected = ref<string[]>([])
const plan = ref<Plan | null>(null)
const busy = ref(false)

async function load(): Promise<void> {
  loading.value = true
  plan.value = null
  try {
    items.value = (await fetchAllPages<AccountSummary>('/oauth/invalid-accounts')).items
    selected.value = items.value.map((a) => a.accountId)
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    loading.value = false
  }
}
watch(
  () => props.open,
  (o) => o && load(),
  { immediate: true },
)

const options = computed(() =>
  items.value.map((a) => ({ value: a.accountId, label: a.displayName, hint: `${providerMeta(a.provider).label} · ${reasonLabel(a.disabledReason) || '失效'}` })),
)
const byId = computed(() => new Map(items.value.map((a) => [a.accountId, a])))

async function makePlan(): Promise<void> {
  busy.value = true
  try {
    // Always send the explicit list: the backend's `all: true` uses a wider predicate than
    // the invalid list (it also sweeps auth-failed WorkBuddy/Qoder accounts not shown here).
    const res = await api.post<Plan>('/oauth/invalid-accounts/delete-plan', { accountIds: selected.value })
    plan.value = res.data
  } catch (err) {
    errorToast(err)
  } finally {
    busy.value = false
  }
}

async function execute(): Promise<void> {
  if (!plan.value) return
  const n = plan.value.accountIds.length
  const ok = await confirmAction({
    title: `删除 ${n} 个失效账号？`,
    detail: '账号会从账号池和调度顺序中移除，相关统计与亲和绑定一并清理。此操作无法撤销。',
    confirmText: '永久删除',
    danger: true,
    requireText: '删除',
  })
  if (!ok) return
  busy.value = true
  try {
    const res = await api.post<{ deleted: number }>('/oauth/invalid-accounts/delete', { planToken: plan.value.planToken })
    toast(`已删除 ${res.data.deleted} 个失效账号`, 'success')
    emit('done')
    emit('close')
  } catch (err) {
    errorToast(err)
    load()
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="失效账号清理" subtitle="认证已失效、需要重新登录的账号" width="md" @close="emit('close')">
    <p v-if="error" class="err">{{ error }}</p>
    <div v-else-if="loading" class="sk"><Skeleton v-for="i in 3" :key="i" h="30px" /></div>
    <Empty v-else-if="!items.length" title="没有失效账号" detail="所有账号的认证都正常。">
      <template #icon><CircleCheck /></template>
    </Empty>
    <template v-else-if="!plan">
      <p class="lead">勾选要删除的账号，然后生成删除计划。还想继续使用的账号，可以在详情里重新登录。</p>
      <CheckList v-model="selected" :options="options" :searchable="items.length > 8" max-height="340px" />
    </template>
    <template v-else>
      <p class="lead">
        删除计划已生成，将删除以下 <b>{{ plan.accountIds.length }}</b> 个账号。计划在 <span class="mono">{{ clock(plan.expiresAt, false) }}</span> 前有效；期间账号若有变化，执行会被拒绝。
      </p>
      <ul class="plan">
        <li v-for="id in plan.accountIds" :key="id">
          <Trash2 />
          <span class="truncate">{{ byId.get(id)?.displayName || id }}</span>
          <span class="faint">{{ providerMeta(byId.get(id)?.provider).label }}</span>
        </li>
      </ul>
    </template>
    <template #footer>
      <Btn variant="ghost" @click="plan ? (plan = null) : emit('close')">{{ plan ? '返回修改' : '关闭' }}</Btn>
      <Btn v-if="items.length && !plan" variant="primary" :loading="busy" :disabled="!selected.length" @click="makePlan">
        生成删除计划（{{ selected.length }}）
      </Btn>
      <Btn v-if="plan" variant="danger" :loading="busy" @click="execute"><Trash2 /> 确认删除</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.lead {
  margin-bottom: 10px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
}
.plan {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.plan li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 8px;
  font-size: 12.5px;
  background: color-mix(in srgb, var(--red) 8%, transparent);
}
.plan svg {
  width: 13px;
  height: 13px;
  color: var(--red);
  flex-shrink: 0;
}
.plan .truncate {
  flex: 1;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.err {
  color: var(--text-2);
}
</style>
