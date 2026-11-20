<script setup lang="ts">
import { computed } from 'vue'
import { ArrowDown, ArrowUp, Plus, Trash2 } from '@lucide/vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import TextInput from '@/components/ui/TextInput.vue'
import DurationInput from './DurationInput.vue'
import { MODE_OPTIONS, STEP_META, newStep, type Step, type StepType } from './model'

const props = withDefaults(defineProps<{ steps: Step[]; allowRetry?: boolean; emptyText?: string }>(), {
  allowRetry: true,
  emptyText: '还没有步骤',
})

const SCOPES = [
  { value: 'model' as const, label: '这个模型' },
  { value: 'account' as const, label: '整个账号' },
]
const TIMEZONES = [
  { value: '+08:00', label: '北京时间（UTC+8）' },
  { value: 'UTC', label: 'UTC' },
  { value: '+09:00', label: '东京（UTC+9）' },
  { value: '-08:00', label: '美西（UTC−8）' },
  { value: '-05:00', label: '美东（UTC−5）' },
  { value: '+01:00', label: '欧洲中部（UTC+1）' },
]

const MODE_SELECT = MODE_OPTIONS.map(({ value, label }) => ({ value, label }))
// The cooldown ladder is per model, so an account pause needs a definite end.
const ACCOUNT_MODE_SELECT = MODE_SELECT.filter((o) => o.value !== 'ladder')

function setScope(s: Step, scope: 'model' | 'account'): void {
  s.scope = scope
  if (scope === 'account') {
    if (s.mode === 'ladder') s.mode = 'fixed'
    if (!s.fallbackSeconds) s.fallbackSeconds = 900
  }
}

const addable = computed(() =>
  (Object.keys(STEP_META) as StepType[]).filter((t) => props.allowRetry || t !== 'retry'),
)

function add(type: StepType): void {
  props.steps.push(newStep(type))
}

function move(i: number, delta: number): void {
  const [s] = props.steps.splice(i, 1)
  props.steps.splice(i + delta, 0, s)
}

function boundaryList(hours: number | null | undefined): string {
  if (!hours || hours < 1) return ''
  const list: number[] = []
  for (let x = 0; x < 24; x += hours) list.push(x)
  return list.length > 8 ? `每 ${hours} 小时` : `北京时间 ${list.map((x) => `${x} 点`).join('、')}`
}
</script>

<template>
  <div class="steps">
    <p v-if="!steps.length" class="empty">{{ emptyText }}</p>
    <ol v-else class="list">
      <li v-for="(s, i) in steps" :key="i" class="step" :style="{ '--t': STEP_META[s.type].tone }">
        <span class="no">{{ i + 1 }}</span>
        <div class="body">
          <div class="line">
            <component :is="STEP_META[s.type].icon" class="ico" />
            <template v-if="s.type === 'retry'">
              <span>等</span>
              <NumberInput v-model="s.delaySeconds" :min="0" :max="300" integer unit="秒" width="96px" />
              <span>后在同一渠道重试</span>
              <NumberInput v-model="s.times" :min="1" :max="5" integer unit="次" width="86px" />
            </template>
            <template v-else-if="s.type === 'pause'">
              <span>暂停</span>
              <Segmented :model-value="s.scope || 'model'" :options="SCOPES" size="sm" @update:model-value="(v) => setScope(s, v)" />
              <Select :model-value="s.mode || 'fixed'" :options="s.scope === 'account' ? ACCOUNT_MODE_SELECT : MODE_SELECT" width="196px" @update:model-value="(v) => (s.mode = v)" />
            </template>
            <template v-else-if="s.type === 'notify'">
              <span>发通知，同一渠道和模型</span>
              <NumberInput v-model="s.repeatHours" :min="1" :max="168" integer unit="小时" width="96px" />
              <span>内只提醒一次</span>
            </template>
            <span v-else class="label">{{ STEP_META[s.type].label }}</span>
          </div>

          <div v-if="s.type === 'pause'" class="sub">
            <template v-if="s.mode === 'fixed'">
              <span>时长</span>
              <DurationInput v-model="s.seconds" />
            </template>
            <template v-if="s.mode === 'boundary' || s.mode === 'usage_window'">
              <span>{{ s.mode === 'boundary' ? '每' : '读不到时到下一个' }}</span>
              <NumberInput v-model="s.boundaryHours" :min="s.mode === 'boundary' ? 1 : 0" :max="168" integer unit="小时" width="100px" />
              <span>{{ s.mode === 'boundary' ? '一个周期' : '周期边界' }}</span>
              <small v-if="boundaryList(s.boundaryHours)">（{{ boundaryList(s.boundaryHours) }}）</small>
            </template>
            <template v-if="s.mode === 'message_time' || s.mode === 'retry_after' || (s.mode === 'usage_window' && !s.boundaryHours)">
              <span>读不到时间时</span>
              <DurationInput v-model="s.fallbackSeconds" :allow-zero="s.scope !== 'account'" />
              <small v-if="!s.fallbackSeconds">（填 0 表示按冷却阶梯）</small>
            </template>
            <template v-if="s.mode === 'message_time'">
              <span class="br" />
              <span>时间格式</span>
              <TextInput v-model="s.pattern" mono placeholder="自动识别（可填正则，第一个括号是时间）" width="260px" />
              <Select :model-value="s.timezone || '+08:00'" :options="TIMEZONES" width="150px" @update:model-value="(v) => (s.timezone = v)" />
            </template>
            <small v-if="s.mode === 'ladder'">阶梯在「系统设置 → 错误冷却」里调整</small>
          </div>
          <p class="hint">{{ s.type === 'pause' ? MODE_OPTIONS.find((o) => o.value === s.mode)?.hint : STEP_META[s.type].hint }}</p>
        </div>
        <div class="ops">
          <button type="button" title="上移" :disabled="i === 0" @click="move(i, -1)"><ArrowUp /></button>
          <button type="button" title="下移" :disabled="i === steps.length - 1" @click="move(i, 1)"><ArrowDown /></button>
          <button type="button" title="删除" class="danger" @click="steps.splice(i, 1)"><Trash2 /></button>
        </div>
      </li>
    </ol>
    <div class="adder">
      <span class="adder-label"><Plus /> 添加步骤：</span>
      <button v-for="t in addable" :key="t" type="button" class="add" :style="{ '--t': STEP_META[t].tone }" :title="STEP_META[t].hint" @click="add(t)">
        <component :is="STEP_META[t].icon" /> {{ STEP_META[t].label }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.empty {
  font-size: 12px;
  color: var(--text-3);
  padding: 8px 10px;
  border-radius: 9px;
  border: 0.5px dashed var(--border);
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.step {
  --t: var(--blue);
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: flex-start;
  padding: 9px 10px;
  border-radius: 11px;
  border: 0.5px solid var(--hairline);
  background: var(--surface-solid, var(--surface));
}
.no {
  width: 22px;
  height: 22px;
  margin-top: 4px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 700;
  color: var(--t);
  background: color-mix(in srgb, var(--t) 14%, transparent);
}
.body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.line,
.sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text);
}
.sub {
  color: var(--text-2);
}
.sub small,
.line small {
  color: var(--text-3);
  font-size: 11.5px;
}
.br {
  flex-basis: 100%;
  height: 0;
}
.ico {
  width: 15px;
  height: 15px;
  color: var(--t);
  flex-shrink: 0;
}
.label {
  font-weight: 550;
}
.hint {
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.45;
}
.ops {
  display: flex;
  gap: 2px;
  margin-top: 2px;
}
.ops button {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 7px;
  color: var(--text-3);
  cursor: pointer;
}
.ops button:hover:not(:disabled) {
  background: var(--fill-2);
  color: var(--text);
}
.ops button.danger:hover {
  color: var(--red);
}
.ops button:disabled {
  opacity: 0.35;
  cursor: default;
}
.ops svg {
  width: 13px;
  height: 13px;
}
.adder {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.adder-label {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  color: var(--text-3);
}
.adder-label svg {
  width: 12px;
  height: 12px;
}
.add {
  --t: var(--blue);
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
.add:hover {
  border-color: var(--t);
  color: var(--text);
  background: color-mix(in srgb, var(--t) 8%, transparent);
}
.add svg {
  width: 13px;
  height: 13px;
  color: var(--t);
}
@media (max-width: 560px) {
  .step {
    grid-template-columns: 22px minmax(0, 1fr);
  }
  .ops {
    grid-column: 2;
    justify-content: flex-end;
  }
}
</style>
