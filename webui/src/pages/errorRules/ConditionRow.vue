<script setup lang="ts">
import { computed } from 'vue'
import { Trash2 } from '@lucide/vue'
import ProviderIcon from '@/components/ProviderIcon.vue'
import Select from '@/components/ui/Select.vue'
import TextInput from '@/components/ui/TextInput.vue'
import TagInput from './TagInput.vue'
import {
  CHANNEL_TYPE_LABELS,
  FIELD_META,
  FIELD_ORDER,
  LIST_OPS,
  NO_VALUE_OPS,
  OP_LABELS,
  PHASE_OPTIONS,
  providerLabel,
  validPath,
  type Cond,
  type Field,
  type Op,
} from './model'

const props = defineProps<{
  cond: Cond
  providers: { id: string; channelCount: number }[]
  channelOptions: { value: string; label: string; hint?: string }[]
  channelLabel: (id: string) => string
  removable: boolean
}>()
const emit = defineEmits<{ remove: [] }>()

const meta = computed(() => FIELD_META[props.cond.field])
const fieldOptions = FIELD_ORDER.map((f) => ({ value: f, label: FIELD_META[f].label }))
const opOptions = computed(() => meta.value.ops.map((o) => ({ value: o, label: OP_LABELS[o] })))
const isList = computed(() => LIST_OPS.includes(props.cond.op))
// A rule may name a provider that has no channel right now (copied or imported); keep it visible.
const providerChoices = computed(() => [
  ...props.providers,
  ...(props.cond.field === 'provider' ? props.cond.values || [] : [])
    .filter((v) => !props.providers.some((p) => p.id === v))
    .map((id) => ({ id, channelCount: 0 })),
])
const noValue = computed(() => NO_VALUE_OPS.includes(props.cond.op))

function setField(field: Field): void {
  const c = props.cond
  if (c.field === field) return
  c.field = field
  c.op = FIELD_META[field].ops[0]
  c.path = field === 'path' ? '$.' : ''
  c.values = field === 'channelType' ? ['oauth'] : []
}

function setOp(op: Op): void {
  const c = props.cond
  c.op = op
  if (!LIST_OPS.includes(op) && (c.values?.length ?? 0) > 1) c.values = c.values!.slice(0, 1)
}

function toggle(value: string): void {
  const list = props.cond.values || (props.cond.values = [])
  const i = list.indexOf(value)
  if (i >= 0) list.splice(i, 1)
  else list.push(value)
}

function addChannel(id: string): void {
  const list = props.cond.values || (props.cond.values = [])
  if (id && !list.includes(id)) list.push(id)
}

const single = computed({
  get: () => props.cond.values?.[0] ?? '',
  set: (v: string) => {
    props.cond.values = v.trim() ? [v.trim()] : []
  },
})
</script>

<template>
  <div class="cond">
    <div class="head">
      <Select :model-value="cond.field" :options="fieldOptions" width="118px" @update:model-value="setField" />
      <TextInput
        v-if="cond.field === 'path'"
        v-model="cond.path"
        mono
        placeholder="$.error.code"
        width="190px"
        :invalid="!validPath(cond.path)"
      />
      <Select :model-value="cond.op" :options="opOptions" width="136px" @update:model-value="setOp" />
      <div v-if="!noValue" class="value">
        <div v-if="meta.kind === 'provider'" class="pills">
          <button
            v-for="p in providerChoices"
            :key="p.id"
            type="button"
            class="pill"
            :class="{ on: cond.values?.includes(p.id) }"
            :title="p.channelCount ? `${p.channelCount} 个渠道` : '暂无渠道'"
            @click="toggle(p.id)"
          >
            <ProviderIcon :provider="p.id" :size="13" /> {{ providerLabel(p.id) }}
          </button>
        </div>
        <div v-else-if="meta.kind === 'phase'" class="pills">
          <button v-for="p in PHASE_OPTIONS" :key="p.value" type="button" class="pill" :class="{ on: cond.values?.includes(p.value) }" :title="p.hint" @click="toggle(p.value)">
            {{ p.label }}
          </button>
        </div>
        <div v-else-if="meta.kind === 'channelType'" class="pills">
          <button v-for="(label, v) in CHANNEL_TYPE_LABELS" :key="v" type="button" class="pill" :class="{ on: cond.values?.[0] === v }" @click="cond.values = [String(v)]">
            {{ label }}
          </button>
        </div>
        <div v-else-if="meta.kind === 'channel'" class="chan">
          <Select
            model-value=""
            :options="channelOptions.map((o) => ({ ...o, disabled: cond.values?.includes(o.value) }))"
            placeholder="选择渠道或账号…"
            searchable
            width="100%"
            @update:model-value="(v) => addChannel(String(v))"
          />
          <div v-if="cond.values?.length" class="chips">
            <span v-for="c in cond.values" :key="c" class="chip">
              {{ channelLabel(c) }}
              <button type="button" :aria-label="`移除 ${channelLabel(c)}`" @click="toggle(c)">×</button>
            </span>
          </div>
        </div>
        <TagInput v-else-if="isList" :model-value="cond.values || []" @update:model-value="(v) => (cond.values = v)" :mono="meta.kind !== 'text'" :placeholder="`${meta.placeholder || '值'}，回车添加多个`" />
        <TextInput v-else v-model="single" :mono="meta.kind !== 'text'" :placeholder="meta.placeholder || '值'" />
      </div>
      <button v-if="removable" type="button" class="del" title="删除这个条件" @click="emit('remove')"><Trash2 /></button>
    </div>
    <p
      v-if="meta.hint && ['path', 'code', 'status'].includes(cond.field) && (cond.field === 'path' ? !validPath(cond.path) : !(cond.values?.length))"
      class="hint"
    >{{ meta.hint }}</p>
    <p v-if="isList && (cond.values?.length ?? 0) > 1" class="hint">
      {{ cond.op === 'not_in' || cond.op === 'not_contains' ? '一个都不符合时成立' : '符合任一个即成立' }}
    </p>
  </div>
</template>

<style scoped>
.cond {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.head {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  flex-wrap: wrap;
}
.value {
  flex: 1 1 200px;
  min-width: 0;
}
.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding-top: 2px;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 9px;
  border-radius: 7px;
  font-size: 12px;
  border: 0.5px solid var(--border);
  color: var(--text-2);
  cursor: pointer;
}
.pill:hover {
  background: var(--fill-2);
}
.pill.on {
  border-color: var(--accent, var(--blue));
  background: var(--accent-soft, color-mix(in srgb, var(--blue) 14%, transparent));
  color: var(--text);
  font-weight: 550;
}
.chan {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--fill-2);
}
.chip button {
  color: var(--text-3);
  cursor: pointer;
}
.del {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  color: var(--text-3);
  cursor: pointer;
  flex-shrink: 0;
}
.del:hover {
  background: var(--fill-2);
  color: var(--red);
}
.del svg {
  width: 14px;
  height: 14px;
}
.hint {
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.45;
}
</style>
