<script setup lang="ts">
import { computed } from 'vue'
import { ArrowDown, ArrowUp, Copy, CopyPlus, Pencil, Trash2 } from '@lucide/vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Switch from '@/components/ui/Switch.vue'
import { relative } from '@/utils/format'
import {
  CATEGORY_LOOK,
  STEP_META,
  categoryLabel,
  describeStep,
  describeThreshold,
  finalLabel,
  providerLabel,
  treeSegments,
  type CategoryInfo,
  type ErrorRule,
} from './model'

const props = defineProps<{
  rule: ErrorRule
  categories: CategoryInfo[]
  layer: 'user' | 'preset'
  index?: number
  total?: number
  hits?: { count: number; lastAt?: number | null } | null
  busy?: string
  inactive?: boolean
  channelLabel: (id: string) => string
}>()

const emit = defineEmits<{
  toggle: [value: boolean]
  edit: []
  duplicate: []
  remove: []
  move: [delta: -1 | 1]
}>()

const look = computed(() => CATEGORY_LOOK[props.rule.category])
const sentence = computed(() => treeSegments(props.rule.when, { provider: providerLabel, channel: props.channelLabel }))
const locked = computed(() => !!props.busy)
</script>

<template>
  <article class="rule" :class="{ off: !rule.enabled || inactive }" :style="{ '--c': look.color }">
    <div class="head">
      <span v-if="layer === 'user' && index !== undefined" class="ord">{{ index + 1 }}</span>
      <Switch
        :model-value="rule.enabled"
        :loading="busy === `toggle:${rule.id}`"
        :disabled="locked && busy !== `toggle:${rule.id}`"
        :label="`${rule.enabled ? '停用' : '启用'}「${rule.name}」`"
        @update:model-value="(v) => emit('toggle', v)"
      />
      <h4 class="name">{{ rule.name }}</h4>
      <Badge :tone="look.tone">
        <component :is="look.icon" class="b-ico" />
        {{ categoryLabel(categories, rule.category) }}
      </Badge>
      <span class="hits" :class="{ hot: hits && hits.count }">
        <template v-if="hits && hits.count">7 天命中 {{ hits.count }} 次 · 最近 {{ relative(hits.lastAt) }}</template>
        <template v-else>7 天内未命中</template>
      </span>
      <div class="ops">
        <template v-if="layer === 'user'">
          <Btn icon size="sm" variant="ghost" title="上移（更早匹配）" :disabled="locked || index === 0" @click="emit('move', -1)"><ArrowUp /></Btn>
          <Btn icon size="sm" variant="ghost" title="下移" :disabled="locked || index === (total ?? 1) - 1" @click="emit('move', 1)"><ArrowDown /></Btn>
          <Btn icon size="sm" variant="ghost" title="编辑" :disabled="locked" @click="emit('edit')"><Pencil /></Btn>
          <Btn icon size="sm" variant="ghost" title="复制一条" :disabled="locked" @click="emit('duplicate')"><Copy /></Btn>
          <Btn icon size="sm" variant="ghost" title="删除" :disabled="locked" @click="emit('remove')"><Trash2 /></Btn>
        </template>
        <Btn v-else size="sm" :disabled="locked" title="复制成我的规则，修改后优先生效" @click="emit('duplicate')">
          <CopyPlus /> 复制为我的规则
        </Btn>
      </div>
    </div>

    <p class="when">
      <span class="lead">当</span>
      <template v-for="(s, i) in sentence" :key="i"><b v-if="s.strong">{{ s.text }}</b><span v-else :class="{ muted: s.muted }">{{ s.text }}</span></template>
    </p>
    <p class="then">
      <span class="arrow">→</span>
      <span v-for="(st, i) in rule.steps" :key="i" class="act" :style="{ '--t': STEP_META[st.type].tone }">
        <component :is="STEP_META[st.type].icon" class="a-ico" />{{ describeStep(st) }}
      </span>
      <span class="act last">{{ finalLabel(rule.final) }}<template v-if="rule.final !== 'pass' && rule.response.status">（客户端收到 {{ rule.response.status }}）</template></span>
    </p>
    <p v-for="(t, i) in rule.thresholds" :key="i" class="th">累计：{{ describeThreshold(t) }}</p>
    <p v-if="rule.note" class="note">{{ rule.note }}</p>
  </article>
</template>

<style scoped>
.rule {
  --c: var(--text-3);
  position: relative;
  padding: 12px 14px 12px 16px;
  border-radius: 12px;
  border: 0.5px solid var(--hairline);
  background: var(--surface-raised, var(--fill-3, transparent));
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: opacity 0.2s, border-color 0.15s;
}
.rule::before {
  content: '';
  position: absolute;
  left: 0;
  top: 12px;
  bottom: 12px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--c);
}
.rule:hover {
  border-color: var(--border);
}
.rule.off {
  opacity: 0.55;
}
.rule.off::before {
  background: var(--text-3);
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-height: 28px;
}
.ord {
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  display: inline-grid;
  place-items: center;
  font-size: 11px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  color: var(--text-2);
  background: var(--fill-2);
}
.name {
  font-size: 13.5px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.b-ico {
  width: 12px;
  height: 12px;
  margin-right: 2px;
  vertical-align: -2px;
}
.hits {
  font-size: 11.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.hits.hot {
  color: var(--text-2);
}
.ops {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 2px;
}
.when {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
}
.when b {
  color: var(--text);
  font-weight: 600;
}
.when .muted {
  color: var(--text-3);
}
.lead {
  margin-right: 4px;
}
.then {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 0;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
}
.arrow {
  color: var(--c);
  font-weight: 700;
  margin-right: 6px;
}
.act {
  --t: var(--text-3);
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
.act:not(:last-child)::after {
  content: '›';
  margin: 0 7px;
  color: var(--text-3);
}
.act.last {
  color: var(--text);
  font-weight: 550;
}
.a-ico {
  width: 12px;
  height: 12px;
  color: var(--t);
}
.th {
  font-size: 12px;
  color: var(--text-2);
}
.note {
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.5;
}
@media (max-width: 720px) {
  .ops {
    margin-left: 0;
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
