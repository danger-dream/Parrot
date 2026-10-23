<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, ArrowUp, Plus, X } from '@lucide/vue'
import { api, enc } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import Btn from '@/components/ui/Btn.vue'
import Field from '@/components/ui/Field.vue'
import Modal from '@/components/ui/Modal.vue'
import TextInput from '@/components/ui/TextInput.vue'
import { nameError, notifyProxiesChanged, targetLabel, typeLabel, type Proxy, type ProxyGroup } from './meta'

const props = defineProps<{ open: boolean; group: ProxyGroup | null; proxies: Proxy[]; taken: string[] }>()
const emit = defineEmits<{ close: []; saved: [group: ProxyGroup] }>()

const name = ref('')
const members = ref<string[]>([])
const saving = ref(false)

watch(
  () => props.open,
  (o) => {
    if (!o) return
    name.value = props.group?.name || ''
    members.value = [...(props.group?.members || [])]
  },
)

const nErr = computed(() => nameError(name.value, props.taken.filter((t) => t !== props.group?.name)))
const available = computed(() => [
  ...props.proxies.map((p) => ({ id: p.proxyId, label: p.name, hint: `${typeLabel(p.type)} · ${p.server || ''}${p.port ? `:${p.port}` : ''}` })),
  { id: 'direct', label: '直连', hint: '不经过代理，作为最后的兜底' },
])
const memberHint = (id: string) => available.value.find((a) => a.id === id)?.hint || '已不存在'

function add(id: string): void {
  if (!members.value.includes(id)) members.value.push(id)
}
function remove(i: number): void {
  members.value.splice(i, 1)
}
function move(i: number, d: number): void {
  const j = i + d
  if (j < 0 || j >= members.value.length) return
  const arr = [...members.value]
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  members.value = arr
}

async function save(): Promise<void> {
  if (nErr.value || !members.value.length) return
  saving.value = true
  try {
    let res
    if (props.group) {
      const body: Record<string, unknown> = { members: members.value }
      if (name.value.trim() !== props.group.name) body.name = name.value.trim()
      res = await api.patch<ProxyGroup>(`/proxy-groups/${enc(props.group.groupId)}`, body, { ifMatch: props.group.revision })
    } else {
      res = await api.post<ProxyGroup>('/proxy-groups', { name: name.value.trim(), members: members.value })
    }
    toast(`代理组 ${res.data.name} 已保存`, 'success', `成员：${res.data.members.map(targetLabel).join(' → ')}`)
    notifyProxiesChanged()
    emit('saved', res.data)
    emit('close')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal :open="open" :title="group ? '编辑代理组' : '新建代理组'" :subtitle="group?.name" width="md" :dismissible="!saving" @close="emit('close')">
    <div class="form">
      <Field label="组名" required :error="name ? nErr : ''">
        <TextInput v-model="name" mono placeholder="例如：us" :invalid="Boolean(name && nErr)" />
      </Field>
      <Field label="成员（按顺序故障转移）" hint="前一个连不上时自动切换到下一个。">
        <ol v-if="members.length" class="members">
          <li v-for="(m, i) in members" :key="m">
            <span class="idx mono">{{ i + 1 }}</span>
            <span class="text">
              <span class="mono">{{ targetLabel(m) }}</span>
              <span class="hint">{{ memberHint(m) }}</span>
            </span>
            <button type="button" title="上移" :disabled="i === 0" @click="move(i, -1)"><ArrowUp /></button>
            <button type="button" title="下移" :disabled="i === members.length - 1" @click="move(i, 1)"><ArrowDown /></button>
            <button type="button" title="移除" @click="remove(i)"><X /></button>
          </li>
        </ol>
        <p v-else class="empty">还没有成员，从下面点选添加。</p>
      </Field>
      <div class="pool">
        <button v-for="a in available.filter((x) => !members.includes(x.id))" :key="a.id" type="button" class="add" @click="add(a.id)">
          <Plus /> <span class="mono">{{ a.label }}</span>
        </button>
      </div>
    </div>
    <template #footer>
      <Btn v-if="members.length" variant="ghost" :disabled="saving" @click="members = []">清空成员</Btn>
      <span class="grow" />
      <Btn variant="ghost" :disabled="saving" @click="emit('close')">取消</Btn>
      <Btn variant="primary" :loading="saving" :disabled="Boolean(nErr) || !members.length" @click="save">保存</Btn>
    </template>
  </Modal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.members {
  list-style: none;
  margin: 0;
  padding: 4px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
}
.members li {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 40px;
  padding: 2px 6px;
}
.members li + li {
  border-top: 0.5px solid var(--hairline);
}
.idx {
  width: 18px;
  font-size: 11px;
  color: var(--text-3);
  text-align: right;
}
.text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.hint {
  font-size: 11px;
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.members button {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 6px;
  color: var(--text-2);
}
.members button:hover:not(:disabled) {
  background: var(--fill-2);
}
.members button:disabled {
  opacity: 0.3;
}
.members svg {
  width: 14px;
  height: 14px;
}
.empty {
  font-size: 12px;
  color: var(--text-3);
}
.pool {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.add {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 32px;
  padding: 0 10px;
  border-radius: 8px;
  font-size: 12px;
  background: var(--fill-2);
}
.add:hover {
  background: var(--accent-soft);
  color: var(--accent);
}
.add svg {
  width: 12px;
  height: 12px;
}
.grow {
  flex: 1;
}
</style>
