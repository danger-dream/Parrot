<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RefreshCw, RotateCcw } from '@lucide/vue'
import { api } from '@/api/client'
import { toast } from '@/stores/ui'
import { dateTime, relative } from '@/utils/format'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import SubBlock from '@/components/SubBlock.vue'
import { accountPath, errorLine, errorToast } from './oauthApi'
import { RESET_CARD_STATUS, resetCardType, type ResetCard, type ResetCredits } from './meta'

/** `scope` is the detail page's slot prefix: the block renders into its 额度 section. */
const props = defineProps<{ accountId: string; revision: string; scope: string; tick?: number; resetting?: boolean }>()
/** The parent runs plan → confirm → execute with the chosen card (null = upstream default). */
const emit = defineEmits<{ reset: [creditId: string | null, label: string] }>()

const data = ref<ResetCredits | null>(null)
const error = ref('')
const live = ref(false)
const selected = ref<string>('')

async function load(refresh = false): Promise<void> {
  if (refresh) live.value = true
  try {
    data.value = (await api.get<ResetCredits>(accountPath(props.accountId, '/openai/reset-credits'), { refresh: refresh || undefined })).data
    error.value = ''
    if (refresh) toast('已从 OpenAI 实时查询重置卡', 'success')
    if (selected.value && !data.value.cards.some((c) => c.creditId === selected.value)) selected.value = ''
  } catch (err) {
    if (refresh) errorToast(err)
    else error.value = errorLine(err)
  } finally {
    live.value = false
  }
}
onMounted(() => load())
watch([() => props.revision, () => props.tick], () => load())

const usable = (c: ResetCard) => !c.status || c.status === 'available'
const tone = (s: string | null | undefined) => (s === 'available' ? 'green' : s === 'redeeming' ? 'orange' : 'neutral')
const chosen = computed(() => data.value?.cards.find((c) => c.creditId === selected.value) || null)
const count = computed(() => {
  const d = data.value
  if (!d) return null
  return d.availableCount ?? d.cards.filter(usable).length
})

function reset(): void {
  const c = chosen.value
  emit('reset', c ? c.creditId : null, c ? `${resetCardType(c.resetType)}（${c.expiresAt ? `${dateTime(c.expiresAt)} 过期` : c.creditId}）` : '由 OpenAI 自动选择的默认卡')
}
</script>

<template>
  <Teleport defer :to="`#${scope}-quota`">
    <SubBlock title="官方重置卡" :note="count !== null ? `可用 ${count} 张` : ''">
      <template #actions>
        <Btn variant="ghost" size="sm" :loading="live" @click="load(true)"><RefreshCw /> 实时查询</Btn>
      </template>
      <p v-if="error" class="faint small">{{ error }}</p>
      <Skeleton v-else-if="!data" h="40px" />
      <template v-else>
        <ul v-if="data.cards.length" class="cards" role="radiogroup" aria-label="选择要使用的重置卡">
          <li
            v-for="c in data.cards"
            :key="c.creditId"
            :class="{ on: selected === c.creditId, off: !usable(c) }"
            role="radio"
            :aria-checked="selected === c.creditId"
            :tabindex="usable(c) ? 0 : -1"
            @click="usable(c) && (selected = selected === c.creditId ? '' : c.creditId)"
            @keydown.enter="usable(c) && (selected = selected === c.creditId ? '' : c.creditId)"
          >
            <span class="dot" />
            <div class="ci">
              <p class="cl">
                <span>{{ resetCardType(c.resetType) }}</span>
                <Badge :tone="tone(c.status)">{{ RESET_CARD_STATUS[c.status || ''] || c.status || '未知状态' }}</Badge>
              </p>
              <p class="faint small">发放 {{ c.grantedAt ? dateTime(c.grantedAt) : '—' }} · 过期 {{ c.expiresAt ? dateTime(c.expiresAt) : '上游未返回' }}</p>
            </div>
          </li>
        </ul>
        <p class="faint small">
          {{ data.source === 'live' ? '实时数据' : '缓存数据' }}<template v-if="data.fetchedAt"> · 更新于 {{ relative(data.fetchedAt) }}</template>
          <template v-if="!data.cards.length"> · 上游没有返回卡片明细</template>
        </p>
      </template>
      <template v-if="data" #footer>
        <span class="faint small hint">{{ chosen ? '将使用选中的卡' : data.cards.length ? '未选择时由 OpenAI 使用默认卡' : '将由 OpenAI 使用默认卡' }}</span>
        <Btn size="sm" variant="danger" :loading="resetting" @click="reset"><RotateCcw /> 使用重置卡…</Btn>
      </template>
    </SubBlock>
  </Teleport>
</template>

<style scoped>
.cards {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cards li {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  box-shadow: inset 0 0 0 0.5px var(--hairline);
  cursor: pointer;
}
.cards li.off {
  cursor: default;
  opacity: 0.55;
}
.cards li.on {
  box-shadow: inset 0 0 0 1px var(--accent);
  background: var(--accent-soft);
}
.dot {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  border-radius: 50%;
  box-shadow: inset 0 0 0 1.5px var(--border-strong);
}
.on .dot {
  box-shadow: inset 0 0 0 4px var(--accent);
}
.ci {
  flex: 1;
  min-width: 0;
}
.cl {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
}
.hint {
  margin-right: auto;
}
.small {
  font-size: 11.5px;
}
</style>
