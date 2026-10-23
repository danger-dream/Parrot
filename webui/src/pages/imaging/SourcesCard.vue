<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Film, Image as ImageIcon } from '@lucide/vue'
import { api, enc, type Schemas } from '@/api/client'
import { toast } from '@/stores/ui'
import { errorDetail, errorTitle } from '@/utils/errors'
import { openChannel } from '@/links'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import Empty from '@/components/ui/Empty.vue'
import Skeleton from '@/components/ui/Skeleton.vue'
import Switch from '@/components/ui/Switch.vue'
import { PROVIDER_NAME } from './labels'

type Source = Schemas['MediaSourceData']

const props = defineProps<{ kind: 'image' | 'video' }>()
const emit = defineEmits<{ changed: [] }>()
const noun = computed(() => (props.kind === 'image' ? '图片' : '视频'))

const items = ref<Source[] | null>(null)
const error = ref('')
const busy = ref('')

async function load(): Promise<void> {
  try {
    items.value = (await api.get<Schemas['MediaSourcesData']>(`/media/${props.kind}/sources`)).data.sources
    error.value = ''
  } catch (err) {
    error.value = errorTitle(err)
  }
}
onMounted(load)
defineExpose({ reload: load })

async function toggle(s: Source, v: boolean): Promise<void> {
  busy.value = s.sourceId
  try {
    const r = await api.patch<Source>(`/media/${props.kind}/sources/${enc(s.sourceId)}`, { enabled: v }, { ifMatch: s.revision })
    if (items.value) items.value = items.value.map((x) => (x.sourceId === s.sourceId ? r.data : x))
    toast(v ? `已允许 ${s.label} 用于${noun.value}` : `已停止 ${s.label} 用于${noun.value}`, 'success')
    emit('changed')
  } catch (err) {
    toast(errorTitle(err), 'error', errorDetail(err), 7000)
    await load()
  } finally {
    busy.value = ''
  }
}

function kindOf(id: string): string {
  return id.startsWith('api:') ? 'API 渠道' : 'OAuth 账号'
}
</script>

<template>
  <Card :title="`${noun}来源`" :subtitle="`哪些账号和渠道可以用于${noun}生成；点名称查看详情`" flush>
    <template #icon><component :is="kind === 'image' ? ImageIcon : Film" /></template>
    <p v-if="error" class="state">{{ error }}</p>
    <div v-else-if="!items" class="pad"><Skeleton v-for="i in 3" :key="i" h="30px" r="8px" /></div>
    <Empty v-else-if="!items.length" :title="`还没有可用于${noun}的来源`" detail="登录支持该能力的 OAuth 账号，或添加对应渠道后会出现在这里。" />
    <ul v-else class="list">
      <li v-for="s in items" :key="s.sourceId" :class="{ off: !s.enabled }">
        <div class="m">
          <p class="n">
            <button type="button" class="lk truncate" :title="s.sourceId" @click="openChannel(s.sourceId)">{{ s.label }}</button>
            <Badge tone="purple">{{ PROVIDER_NAME[s.provider] || s.provider }}</Badge>
            <Badge v-if="s.enabled && s.effectiveAvailable" tone="green" dot>可用</Badge>
            <Badge v-else-if="s.enabled" tone="orange" dot>暂不可用</Badge>
            <Badge v-else dot>已停用</Badge>
          </p>
          <p class="sub">{{ kindOf(s.sourceId) }}<template v-if="s.unavailableReason && !s.effectiveAvailable"> · {{ s.unavailableReason }}</template></p>
        </div>
        <Switch
          :model-value="s.enabled"
          :loading="busy === s.sourceId"
          :disabled="!s.enabled && !s.canEnable"
          :label="`${s.label} 用于${noun}`"
          :title="!s.enabled && !s.canEnable ? '账号状态异常，需先在账号管理中修复' : undefined"
          @update:model-value="(v: boolean) => toggle(s, v)"
        />
      </li>
    </ul>
  </Card>
</template>

<style scoped>
.state {
  padding: 30px 16px;
  text-align: center;
  color: var(--text-3);
}
.pad {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 0.5px solid var(--hairline);
}
li {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 52px;
  padding: 8px 16px;
  border-bottom: 0.5px solid var(--hairline);
}
li:last-child {
  border-bottom: 0;
}
.m {
  flex: 1;
  min-width: 0;
}
.n {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.lk {
  font-weight: 550;
  text-align: left;
}
.lk:hover {
  color: var(--accent);
}
li.off .lk {
  color: var(--text-2);
}
.sub {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-3);
}
</style>
