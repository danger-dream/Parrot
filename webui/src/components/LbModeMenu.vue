<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, ChevronDown, ChevronRight } from '@lucide/vue'
import { api } from '@/api/client'
import type { LoadBalancingData } from '@/api/types'
import { goPage } from '@/links'
import { system, modeLabel } from '@/stores/system'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'

defineProps<{ compact?: boolean; align?: 'left' | 'right' | 'center' }>()

const MODES: { value: LoadBalancingData['mode']; label: string; desc: string }[] = [
  { value: 'smart', label: '智能调度', desc: '按近期成功率和速度打分排序，保留 20% 探索' },
  { value: 'order', label: '顺序调度', desc: '按配置的渠道顺序依次尝试' },
  { value: 'priority', label: '优先级调度', desc: '模型专属顺序优先，其次是统一的渠道/账号顺序' },
]

const open = ref(false)
const busy = ref(false)
const root = ref<HTMLElement | null>(null)

function onDoc(e: MouseEvent): void {
  if (open.value && root.value && !root.value.contains(e.target as Node)) open.value = false
}
onMounted(() => document.addEventListener('mousedown', onDoc))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDoc))

async function choose(mode: LoadBalancingData['mode']): Promise<void> {
  const cur = system.loadBalancing
  if (!cur || mode === cur.mode) {
    open.value = false
    return
  }
  busy.value = true
  try {
    const res = await api.patch<LoadBalancingData>('/load-balancing', { mode }, { ifMatch: cur.revision })
    system.loadBalancing = res.data
    toast(`已切换为${modeLabel(mode)}`, 'success')
    open.value = false
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <span ref="root" class="lbm" :class="[{ compact }, `a-${align || 'left'}`]">
    <button type="button" class="trigger" :class="{ on: open }" :disabled="!system.loadBalancing" @click.stop="open = !open">
      <span class="tag">{{ modeLabel(system.loadBalancing?.mode) }}</span>
      <ChevronDown class="chev" />
    </button>
    <Transition name="pop">
      <div v-if="open" class="menu glass" @click.stop>
        <p class="title">调度方式</p>
        <button v-for="m in MODES" :key="m.value" class="opt" :disabled="busy" @click="choose(m.value)">
          <span class="check"><Check v-if="system.loadBalancing?.mode === m.value" /></span>
          <span class="txt">
            <span class="l">{{ m.label }}</span>
            <span class="d">{{ m.desc }}</span>
          </span>
        </button>
        <button class="more" @click="(open = false), goPage('/routing')">渠道顺序与亲和设置 <ChevronRight /></button>
      </div>
    </Transition>
  </span>
</template>

<style scoped>
.lbm {
  position: relative;
  display: inline-flex;
}
.trigger {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 4px 1px 0;
  border-radius: 6px;
}
.trigger:hover .tag,
.trigger.on .tag {
  background: color-mix(in srgb, var(--orange) 24%, transparent);
}
.tag {
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 11.5px;
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 14%, transparent);
  transition: background 0.15s;
}
.chev {
  width: 12px;
  height: 12px;
  color: var(--text-3);
}
.menu {
  position: absolute;
  top: calc(100% + 8px);
  z-index: 60;
  width: 300px;
  padding: 6px;
  border-radius: var(--radius-md);
  background: var(--popover);
  box-shadow: var(--shadow-lg);
  text-align: left;
  white-space: normal;
}
.a-left .menu {
  left: 0;
}
.a-right .menu {
  right: 0;
}
.a-center .menu {
  left: 50%;
  transform: translateX(-50%);
}
.title {
  padding: 6px 8px 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
}
.opt {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 7px 8px;
  border-radius: 7px;
  text-align: left;
}
.opt:hover:not(:disabled) {
  background: var(--fill-2);
}
.check {
  width: 16px;
  display: grid;
  place-items: center;
  color: var(--accent);
  flex-shrink: 0;
  padding-top: 1px;
}
.check svg {
  width: 14px;
  height: 14px;
}
.txt {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.l {
  font-size: 13px;
  font-weight: 550;
}
.d {
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.45;
}
.more {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  margin-top: 4px;
  padding: 8px;
  border-top: 0.5px solid var(--hairline);
  font-size: 12px;
  color: var(--accent);
}
.more svg {
  width: 13px;
  height: 13px;
}
.pop-enter-active,
.pop-leave-active {
  transition: opacity 0.16s, transform 0.2s var(--ease-out);
}
.pop-enter-from,
.pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
.a-center .pop-enter-from,
.a-center .pop-leave-to {
  transform: translateX(-50%) translateY(-4px);
}
</style>
