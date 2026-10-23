<script setup lang="ts">
import { computed, ref } from 'vue'
import { FlaskConical, Save, Shield, X } from '@lucide/vue'
import { api } from '@/api/client'
import type { Operation } from '@/api/operations'
import { runAction } from '@/composables/useResource'
import { confirmAction } from '@/stores/dialogs'
import { toast } from '@/stores/ui'
import Card from '@/components/ui/Card.vue'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Switch from '@/components/ui/Switch.vue'
import TextInput from '@/components/ui/TextInput.vue'
import Badge from '@/components/ui/Badge.vue'
import OpProgress from '@/components/ui/OpProgress.vue'
import { useOperation } from '@/composables/useOperation'
import TestResult from './TestResult.vue'
import { maskUrl, type NetworkSettings, type TestPlan } from './types'

const props = defineProps<{ net: NetworkSettings }>()
const emit = defineEmits<{ updated: [value: NetworkSettings] }>()

const url = ref('')
const preview = computed(() => maskUrl(url.value))
const test = useOperation()
const plan = computed<TestPlan | null>(() => {
  const r = test.op.value?.status === 'succeeded' ? (test.op.value.result as { plan?: TestPlan } | null) : null
  return r?.plan || null
})
const saving = ref(false)
const toggling = ref(false)
const enabledView = ref<boolean | null>(null)
const enabled = computed(() => enabledView.value ?? props.net.socks5.enabled)

async function runTest(): Promise<void> {
  if (!url.value.trim()) {
    toast('请填写 SOCKS5 地址', 'warning')
    return
  }
  await test.run(() => api.post<Operation>('/network/socks5/tests', { url: url.value.trim() }))
}

async function commit(force: boolean): Promise<void> {
  const p = plan.value
  if (!p) return
  if (force) {
    const ok = await confirmAction({
      title: '仍然保存并启用这个代理？',
      detail: '检测发现部分站点经该代理无法访问。保存并启用后，走 SOCKS5 的出站请求可能失败。',
      confirmText: '仍然保存并启用',
      danger: true,
    })
    if (!ok) return
  }
  saving.value = true
  const res = await runAction(
    () => api.post<NetworkSettings>('/network/socks5/commits', { planId: p.id, force }, { ifMatch: props.net.revision }),
    'SOCKS5 已保存并启用',
  )
  saving.value = false
  if (res) {
    url.value = ''
    test.reset()
    enabledView.value = null
    emit('updated', res)
  }
}

function discard(): void {
  test.reset()
  url.value = ''
}

// Optimistic switch: flip immediately, revert if the server rejects it.
async function toggle(v: boolean): Promise<void> {
  enabledView.value = v
  toggling.value = true
  const res = await runAction(() => api.patch<NetworkSettings>('/network/socks5', { enabled: v }, { ifMatch: props.net.revision }), v ? 'SOCKS5 已启用' : 'SOCKS5 已关闭')
  toggling.value = false
  enabledView.value = null
  if (res) emit('updated', res)
}
</script>

<template>
  <Card title="SOCKS5 代理" subtitle="旧版全局 SOCKS5 出口。需要按渠道或模型分流时，请使用「代理」页面的代理组与路由规则。">
    <template #icon><Shield /></template>

    <Field label="启用 SOCKS5" :hint="net.socks5.configured ? '关闭后保留地址，可随时重新启用。' : '还没有设置代理地址，请先在下方填写并测试。'" inline>
      <Switch :model-value="enabled" :disabled="!net.socks5.configured" :loading="toggling" label="启用 SOCKS5" @update:model-value="toggle" />
    </Field>
    <div class="cur">
      <span class="lab">当前地址</span>
      <code v-if="net.socks5.maskedUrl" class="mono url">{{ net.socks5.maskedUrl }}</code>
      <span v-else class="muted">未设置</span>
      <Badge v-if="net.socks5.configured" :tone="net.socks5.enabled ? 'green' : 'neutral'" dot>{{ net.socks5.enabled ? '使用中' : '已关闭' }}</Badge>
    </div>

    <Field label="新的代理地址" hint="支持 socks5://127.0.0.1:1080、tcp://127.0.0.1:1080、127.0.0.1:1080，以及带账号密码的 socks5://user:pass@host:1080。密码只用于测试和保存，不会再显示。">
      <TextInput v-model="url" type="password" mono placeholder="socks5://user:pass@proxy.example.com:1080" @enter="runTest" />
      <p v-if="preview" class="pv">将保存为 <code class="mono">{{ preview }}</code></p>
    </Field>

    <div class="acts">
      <Btn variant="primary" :loading="test.running.value" :disabled="!url.trim()" @click="runTest"><FlaskConical /> 测试</Btn>
      <template v-if="plan">
        <Btn v-if="plan.passed" variant="primary" :loading="saving" @click="commit(false)"><Save /> 保存并启用</Btn>
        <Btn v-else variant="danger" :loading="saving" @click="commit(true)"><Save /> 仍然保存并启用</Btn>
        <Btn variant="ghost" :disabled="saving" @click="discard"><X /> 放弃</Btn>
        <span class="exp">测试结果 5 分钟内有效</span>
      </template>
    </div>
    <OpProgress v-if="test.op.value && !plan" :op="test.op.value" title="正在经代理访问测试站点" class="gap" />
    <TestResult v-if="plan" :plan="plan" class="gap" />
  </Card>
</template>

<style scoped>
.cur {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  margin: 8px 0 14px;
}
.lab {
  font-size: 12px;
  color: var(--text-3);
}
.url {
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 12px;
  background: var(--fill-2);
  overflow-wrap: anywhere;
}
.pv {
  margin-top: 6px;
  font-size: 11.5px;
  color: var(--text-3);
  overflow-wrap: anywhere;
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
