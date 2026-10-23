<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FlaskConical, RefreshCcw, Save, Server, X } from '@lucide/vue'
import { api } from '@/api/client'
import type { Operation } from '@/api/operations'
import { runAction } from '@/composables/useResource'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import { humanSeconds } from '@/utils/format'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import TextArea from '@/components/ui/TextArea.vue'
import OpProgress from '@/components/ui/OpProgress.vue'
import { useOperation } from '@/composables/useOperation'
import TestResult from './TestResult.vue'
import type { NetworkSettings, TestPlan } from './types'

const props = defineProps<{ net: NetworkSettings }>()
const emit = defineEmits<{ updated: [value: NetworkSettings] }>()

const text = ref(props.net.dns.servers.join('\n'))
const editing = ref(false)
watch(
  () => props.net.dns.servers,
  (v) => {
    if (!editing.value) text.value = v.join('\n')
  },
)

const servers = computed(() => text.value.split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean))
const test = useOperation()
const plan = computed<TestPlan | null>(() => {
  const r = test.op.value?.status === 'succeeded' ? (test.op.value.result as { plan?: TestPlan } | null) : null
  return r?.plan || null
})
const saving = ref(false)
const syncing = ref(false)

async function runTest(): Promise<void> {
  if (!servers.value.length) {
    toast('请至少填写一个 DNS 服务器', 'warning')
    return
  }
  if (servers.value.length > 10) {
    toast('最多填写 10 个 DNS 服务器', 'warning')
    return
  }
  editing.value = true
  await test.run(() => api.post<Operation>('/network/dns/tests', { servers: servers.value }))
}

async function commit(force: boolean): Promise<void> {
  const p = plan.value
  if (!p) return
  if (force) {
    const ok = await confirmAction({
      title: '仍然保存这组 DNS？',
      detail: '检测发现部分域名无法解析。保存后，依赖这些域名的上游请求、Telegram 通知或更新检查可能失败。',
      confirmText: '仍然保存',
      danger: true,
    })
    if (!ok) return
  }
  saving.value = true
  const res = await runAction(
    () => api.post<NetworkSettings>('/network/dns/commits', { planId: p.id, force }, { ifMatch: props.net.revision }),
    force ? 'DNS 已强制保存' : 'DNS 已保存',
  )
  saving.value = false
  if (res) {
    editing.value = false
    test.reset()
    emit('updated', res)
  }
}

function discard(): void {
  test.reset()
  editing.value = false
  text.value = props.net.dns.servers.join('\n')
}

async function syncSystem(): Promise<void> {
  const ok = await confirmAction({
    title: '同步系统 DNS？',
    detail: '读取服务器系统的 DNS 配置（/etc/resolv.conf）并替换当前设置，立即生效。',
    confirmText: '同步',
  })
  if (!ok) return
  syncing.value = true
  const res = await runAction(() => api.post<NetworkSettings>('/network/dns/actions/sync-system', undefined, { ifMatch: props.net.revision }))
  syncing.value = false
  if (res) {
    toast('已同步系统 DNS', 'success', res.dns.servers.join(', '))
    discard()
    emit('updated', res)
  }
}
</script>

<template>
  <Card title="DNS" subtitle="Parrot 访问上游时使用的域名解析服务器。修改前会先测试，测试通过才建议保存。">
    <template #icon><Server /></template>
    <template #actions>
      <Btn size="sm" variant="ghost" :loading="syncing" @click="syncSystem"><RefreshCcw /> 同步系统 DNS</Btn>
    </template>

    <div class="cur">
      <span class="lab">当前</span>
      <span class="chips">
        <code v-for="s in net.dns.servers" :key="s" class="chip">{{ s }}</code>
      </span>
      <span class="ttl">缓存 {{ humanSeconds(net.dns.cacheTtlSeconds) }}</span>
    </div>

    <Field label="新的 DNS 服务器" hint="每行一个（也可用逗号分隔），最多 10 个。支持普通 IP、DNS over TLS（dot://1.1.1.1:853?hostname=cloudflare-dns.com）和 DNS over HTTPS（https://dns.google/dns-query）。填域名形式的服务器时，其自身用系统 DNS 解析。">
      <TextArea v-model="text" :rows="3" mono placeholder="1.1.1.1&#10;8.8.8.8" @update:model-value="editing = true" />
    </Field>

    <div class="acts">
      <Btn variant="primary" :loading="test.running.value" @click="runTest"><FlaskConical /> 测试</Btn>
      <template v-if="plan">
        <Btn v-if="plan.passed" variant="primary" :loading="saving" @click="commit(false)"><Save /> 保存</Btn>
        <Btn v-else variant="danger" :loading="saving" @click="commit(true)"><Save /> 仍然保存</Btn>
        <Btn variant="ghost" :disabled="saving" @click="discard"><X /> 放弃</Btn>
        <span class="exp">测试结果 5 分钟内有效</span>
      </template>
    </div>

    <OpProgress v-if="test.op.value && !plan" :op="test.op.value" title="正在测试 DNS" class="gap" />
    <TestResult v-if="plan" :plan="plan" class="gap" />
  </Card>
</template>

<style scoped>
.cur {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  margin-bottom: 12px;
}
.lab {
  font-size: 12px;
  color: var(--text-3);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}
.chip {
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 12px;
  background: var(--fill-2);
  overflow-wrap: anywhere;
}
.ttl {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--text-3);
}
.acts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.exp {
  font-size: 11.5px;
  color: var(--text-3);
}
.gap {
  margin-top: 12px;
}
</style>
