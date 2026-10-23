<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { CircleCheck, CircleX, LoaderCircle, Play } from '@lucide/vue'
import type { Operation } from '@/api/operations'
import { openModel } from '@/links'
import Btn from '@/components/ui/Btn.vue'
import { runOperation, type ModelPair, type ProbeResult } from './meta'

export interface ProbeState {
  status: 'idle' | 'running' | 'ok' | 'fail'
  ms?: number
  reason?: string
  note?: string
}

const props = defineProps<{
  models: ModelPair[]
  start: (model: string) => Promise<{ data: Operation }>
  /** Make model names clickable (existing channels only). */
  linkModels?: boolean
  initial?: Record<string, ProbeState>
}>()
const emit = defineEmits<{ result: [model: string, state: ProbeState] }>()

const states = reactive<Record<string, ProbeState>>({ ...(props.initial || {}) })
const runningAll = ref(false)
const busy = computed(() => runningAll.value || Object.values(states).some((s) => s.status === 'running'))
const summary = computed(() => {
  const vals = props.models.map((m) => states[m.real]?.status || 'idle')
  return { ok: vals.filter((v) => v === 'ok').length, fail: vals.filter((v) => v === 'fail').length }
})

async function test(model: string): Promise<void> {
  states[model] = { status: 'running' }
  const out = await runOperation<ProbeResult>(() => props.start(model))
  let s: ProbeState
  if (!out.ok) s = { status: 'fail', reason: out.error }
  else if (out.result?.ok) {
    const note = out.result.permanent_cooldown_cleared ? '已自动解除永久冷却' : out.result.cooldown_cleared ? '已自动清除冷却与失败计数' : undefined
    s = { status: 'ok', ms: out.result.elapsed_ms, note }
  } else s = { status: 'fail', ms: out.result?.elapsed_ms, reason: out.result?.reason || '未知错误' }
  states[model] = s
  emit('result', model, s)
}

async function testAll(): Promise<void> {
  runningAll.value = true
  try {
    for (const m of props.models) await test(m.real)
  } finally {
    runningAll.value = false
  }
}

defineExpose({ states })
</script>

<template>
  <div class="pp">
    <div class="bar">
      <span class="faint">
        共 {{ models.length }} 个模型
        <template v-if="summary.ok || summary.fail"> · <span class="c-green">通过 {{ summary.ok }}</span> · <span class="c-red">失败 {{ summary.fail }}</span></template>
      </span>
      <Btn size="sm" variant="primary" :loading="runningAll" :disabled="busy || !models.length" @click="testAll"><Play /> 测试全部</Btn>
    </div>
    <ul class="list">
      <li v-for="m in models" :key="m.real" :class="states[m.real]?.status || 'idle'">
        <div class="row">
          <span class="ic">
            <LoaderCircle v-if="states[m.real]?.status === 'running'" class="spin" />
            <CircleCheck v-else-if="states[m.real]?.status === 'ok'" />
            <CircleX v-else-if="states[m.real]?.status === 'fail'" />
            <i v-else class="dot" />
          </span>
          <button v-if="linkModels" type="button" class="name mono truncate link" @click="openModel(m.alias)">{{ m.alias }}</button>
          <span v-else class="name mono truncate">{{ m.alias }}</span>
          <span v-if="states[m.real]?.ms != null" class="ms mono">{{ states[m.real].ms }}ms</span>
          <Btn size="sm" variant="ghost" :disabled="busy" @click="test(m.real)">测试</Btn>
        </div>
        <p v-if="states[m.real]?.reason" class="reason">{{ states[m.real].reason }}</p>
        <p v-if="states[m.real]?.note" class="note">{{ states[m.real].note }}</p>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.pp {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 4px;
  border-radius: var(--radius-md);
  background: var(--fill-3);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  max-height: 320px;
  overflow-y: auto;
}
li {
  padding: 3px 6px;
  border-radius: 6px;
}
li + li {
  border-top: 0.5px solid var(--hairline);
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
}
.ic {
  width: 16px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.ic svg {
  width: 15px;
  height: 15px;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-3);
}
.ok .ic {
  color: var(--green);
}
.fail .ic {
  color: var(--red);
}
.running .ic {
  color: var(--blue);
}
.spin {
  animation: spin 0.9s linear infinite;
}
.name {
  flex: 1;
  font-size: 12px;
  text-align: left;
}
.link:hover {
  color: var(--accent);
}
.ms {
  font-size: 11.5px;
  color: var(--text-2);
}
.reason,
.note {
  margin: 0 0 4px 24px;
  font-size: 11.5px;
  line-height: 1.5;
  word-break: break-word;
}
.reason {
  color: var(--red);
}
.note {
  color: var(--green);
}
</style>
