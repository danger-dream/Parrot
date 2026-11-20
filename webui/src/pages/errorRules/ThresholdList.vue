<script setup lang="ts">
import { Plus, Trash2 } from '@lucide/vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import Segmented from '@/components/ui/Segmented.vue'
import Select from '@/components/ui/Select.vue'
import StepList from './StepList.vue'
import { THRESHOLD_SCOPE_OPTIONS, newThreshold, type Threshold } from './model'

const props = defineProps<{ thresholds: Threshold[] }>()

const MODES = [
  { value: 'consecutive' as const, label: '连续出现' },
  { value: 'window' as const, label: '一段时间内累计' },
]

function setScope(t: Threshold, scope: Threshold['scope']): void {
  t.scope = scope
  if (scope === 'rule') t.mode = 'window'
}
</script>

<template>
  <div class="ths">
    <div v-for="(t, i) in thresholds" :key="i" class="th">
      <div class="th-head">
        <span>当</span>
        <Select :model-value="t.scope" :options="THRESHOLD_SCOPE_OPTIONS" width="170px" @update:model-value="(v) => setScope(t, v)" />
        <Segmented v-if="t.scope !== 'rule'" v-model="t.mode" :options="MODES" size="sm" />
        <NumberInput v-if="t.mode === 'window'" v-model="t.windowMinutes" :min="1" :max="10080" integer unit="分钟内" width="120px" />
        <span class="nowrap">
          <NumberInput v-model="t.count" :min="2" :max="1000" integer unit="次" width="90px" />
          <span>时：</span>
        </span>
      </div>
      <button type="button" class="del" title="删除这条累计触发" @click="props.thresholds.splice(i, 1)"><Trash2 /></button>
      <p class="th-hint">
        每次上游返回这个错误算一次，同渠道重试后又失败也算。{{ t.mode === 'consecutive' ? '中间只要成功一次就重新计数。' : `只数最近 ${t.windowMinutes} 分钟内的次数，成功不清零。` }}
        达到次数后执行下面的动作并重新计数；如果动作里有暂停或停用，这次就不再往同一渠道重试。
      </p>
      <StepList :steps="t.steps" :allow-retry="false" empty-text="还没有动作，从下面添加" />
    </div>
    <button type="button" class="add" @click="props.thresholds.push(newThreshold())"><Plus /> 添加累计触发</button>
  </div>
</template>

<style scoped>
.ths {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.th {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 12px;
  border: 0.5px solid var(--hairline);
  background: color-mix(in srgb, var(--orange) 5%, transparent);
}
.th-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding-right: 30px;
  font-size: 12.5px;
}
.nowrap {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.th-hint {
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.45;
}
.del {
  position: absolute;
  top: 12px;
  right: 10px;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 7px;
  color: var(--text-3);
  cursor: pointer;
}
.del:hover {
  background: var(--fill-2);
  color: var(--red);
}
.del svg {
  width: 13px;
  height: 13px;
}
.add {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12.5px;
  color: var(--accent, var(--blue));
  cursor: pointer;
}
.add svg {
  width: 13px;
  height: 13px;
}
</style>
