<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RefreshCw } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { toast } from '@/stores/ui'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Modal from '@/components/ui/Modal.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import { errorLine } from './oauthApi'
import { pick, type LoginFlow, type LoginPoll } from './meta'

type Mutation = Schemas['OAuthMutationData']

const props = defineProps<{ open: boolean; accountId: string }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const flow = ref<LoginFlow | null>(null)
const poll = ref<LoginPoll | null>(null)
const busy = ref(false)
const error = ref('')

const choices = computed(() => {
  const list = pick<Record<string, unknown>[]>(poll.value?.accountPreview, 'choices')
  return Array.isArray(list) ? list : []
})

async function start(): Promise<void> {
  busy.value = true
  error.value = ''
  poll.value = null
  try {
    flow.value = (await api.post<LoginFlow>(`/oauth/accounts/${enc(props.accountId)}/zhipu/project-flows`)).data
    poll.value = (await api.post<LoginPoll>(`/oauth/login-flows/${enc(flow.value.flowId)}/poll`, { flowSecret: flow.value.flowSecret })).data
  } catch (err) {
    error.value = errorLine(err)
  } finally {
    busy.value = false
  }
}

async function cancel(): Promise<void> {
  const f = flow.value
  flow.value = null
  if (!f) return
  try {
    await api.post(`/oauth/login-flows/${enc(f.flowId)}/cancel`, { flowSecret: f.flowSecret })
  } catch {
    /* already finished */
  }
}

watch(
  () => props.open,
  (o) => (o ? start() : cancel()),
)

async function select(c: Record<string, unknown>): Promise<void> {
  const f = flow.value
  if (!f) return
  busy.value = true
  try {
    const res = await api.post<Mutation>(`/oauth/login-flows/${enc(f.flowId)}/zhipu/project`, {
      flowSecret: f.flowSecret,
      organizationId: String(pick(c, 'organizationId')),
      projectId: String(pick(c, 'projectId')),
    })
    flow.value = null
    toast('已切换团队 / 项目', 'success', res.data.accountId)
    emit('saved')
  } catch (err) {
    error.value = errorLine(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Modal :open="open" title="切换团队 / 项目" subtitle="选择这个智谱账号要使用的组织和项目" width="md" @close="emit('close')">
    <p v-if="error" class="err">{{ error }}</p>
    <div v-else-if="busy && !choices.length" class="sk"><Skeleton v-for="i in 3" :key="i" h="38px" /></div>
    <p v-else-if="!choices.length" class="faint">没有查到可选择的项目。账号保持不变，可以稍后重新查询。</p>
    <div v-else class="list">
      <p class="faint small">席位和订阅状态会在选择后单独核验。</p>
      <button v-for="c in choices" :key="String(pick(c, 'projectId'))" type="button" class="choice" :disabled="busy" @click="select(c)">
        <span class="t">
          <span class="truncate">{{ pick(c, 'organizationName') || pick(c, 'organizationId') }}</span>
          <span class="faint small truncate">{{ pick(c, 'projectName') || pick(c, 'projectId') }}</span>
        </span>
        <Badge :tone="pick(c, 'planScope') === 'team' ? 'purple' : 'blue'">{{ pick(c, 'planScope') === 'team' ? '团队' : '个人' }}</Badge>
      </button>
    </div>
    <template #footer>
      <Btn variant="ghost" @click="emit('close')">取消</Btn>
      <Btn :loading="busy" @click="start"><RefreshCw /> 重新查询</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.choice {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 44px;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--fill-3);
  text-align: left;
}
.choice:hover:not(:disabled) {
  background: var(--accent-soft);
}
.t {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  font-size: 12.5px;
}
.small {
  font-size: 11.5px;
}
.err {
  color: var(--text-2);
  font-size: 12.5px;
}
.sk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
</style>
