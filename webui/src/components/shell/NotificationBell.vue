<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Bell, CheckCheck, CircleCheck, Info, OctagonAlert, TriangleAlert } from '@lucide/vue'
import { goPage, openAccount, openApiKey, openChannel, openModel, openRequest } from '@/links'
import {
  fetchNotifications,
  markRead,
  notifications,
  refreshUnread,
  type NotificationItem,
  type NotificationView,
} from '@/stores/notifications'
import { errorTitle } from '@/utils/errors'
import { dateTime, relative } from '@/utils/format'
import Segmented from '@/components/ui/Segmented.vue'
import Badge from '@/components/ui/Badge.vue'
import Btn from '@/components/ui/Btn.vue'
import Skeleton from '@/components/ui/Skeleton.vue'

type Link = NotificationItem['links'][number]

const open = ref(false)
const wrap = ref<HTMLElement | null>(null)
const view = ref<NotificationView>('unread')
const items = ref<NotificationItem[]>([])
const page = ref(1)
const hasNext = ref(false)
const loading = ref(false)
const more = ref(false)
const error = ref('')
const clearing = ref(false)

const badge = computed(() => (notifications.unread > 99 ? '99+' : String(notifications.unread)))
const views = computed(() => [
  { value: 'unread' as NotificationView, label: notifications.unread ? `未读 ${badge.value}` : '未读' },
  { value: 'read' as NotificationView, label: '已读' },
])
const TONE = { info: Info, warning: TriangleAlert, error: OctagonAlert, critical: OctagonAlert, recovered: CircleCheck }
const STATUS = {
  active: { label: '进行中', tone: 'orange' },
  recovered: { label: '已恢复', tone: 'green' },
  ended: { label: '已结束', tone: 'neutral' },
} as const

let seq = 0
async function load(next = 1): Promise<void> {
  const mine = ++seq
  if (next === 1) loading.value = true
  else more.value = true
  try {
    const r = await fetchNotifications(view.value, next)
    if (mine !== seq) return
    items.value = next === 1 ? r.items : [...items.value, ...r.items.filter((i) => !items.value.some((x) => x.id === i.id))]
    page.value = next
    hasNext.value = r.hasNext
    error.value = ''
  } catch (err) {
    if (mine !== seq) return
    error.value = errorTitle(err)
  } finally {
    if (mine === seq) {
      loading.value = false
      more.value = false
    }
  }
}

function toggle(): void {
  open.value = !open.value
  if (open.value) {
    view.value = 'unread'
    void load(1)
  }
}

watch(view, () => {
  if (open.value) void load(1)
})
watch(
  () => notifications.version,
  () => {
    if (open.value) void load(1)
  },
)

function follow(link: Link): void {
  open.value = false
  if (link.kind === 'account') openAccount(link.id)
  else if (link.kind === 'channel') openChannel(link.id)
  else if (link.kind === 'model') openModel(link.id)
  else if (link.kind === 'apikey') openApiKey(link.id)
  else if (link.kind === 'request') openRequest(link.id)
  else goPage(link.id)
}

async function select(item: NotificationItem, link: Link | undefined = item.links[0]): Promise<void> {
  if (item.unread) {
    items.value = items.value.filter((i) => i.id !== item.id)
    notifications.unread = Math.max(0, notifications.unread - 1)
    markRead([item.id]).catch(() => refreshUnread())
  }
  if (link) follow(link)
}

async function readAll(): Promise<void> {
  clearing.value = true
  try {
    await markRead([], true)
    items.value = []
    hasNext.value = false
  } catch (err) {
    error.value = errorTitle(err)
  } finally {
    clearing.value = false
  }
}

function onDoc(e: MouseEvent): void {
  if (open.value && wrap.value && !wrap.value.contains(e.target as Node)) open.value = false
}
function onKey(e: KeyboardEvent): void {
  if (open.value && e.key === 'Escape') open.value = false
}
onMounted(() => {
  document.addEventListener('mousedown', onDoc)
  document.addEventListener('keydown', onKey)
  void refreshUnread()
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDoc)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="wrap" class="bell-wrap">
    <button
      class="bell"
      :class="{ on: open }"
      :aria-label="notifications.unread ? `通知，${notifications.unread} 条未读` : '通知'"
      :title="notifications.unread ? `${notifications.unread} 条未读通知` : '通知'"
      @click="toggle"
    >
      <Bell />
      <span v-if="notifications.unread" class="count num">{{ badge }}</span>
    </button>
    <Transition name="pop">
      <div v-if="open" class="panel glass" role="dialog" aria-label="通知">
        <div class="head">
          <p class="head-title">通知</p>
          <Segmented v-model="view" :options="views" size="sm" />
          <button v-if="view === 'unread'" class="clear" :disabled="!notifications.unread || clearing" title="全部标为已读" @click="readAll">
            <CheckCheck /> 全部已读
          </button>
        </div>

        <div class="body">
          <div v-if="loading && !items.length" class="skeletons">
            <Skeleton v-for="n in 4" :key="n" h="52px" r="8px" />
          </div>
          <p v-else-if="error && !items.length" class="state">{{ error }}<button class="retry" @click="load(1)">重试</button></p>
          <p v-else-if="!items.length" class="state">{{ view === 'unread' ? '没有未读通知' : '没有已读通知' }}</p>
          <template v-else>
            <div
              v-for="item in items"
              :key="item.id"
              class="item"
              :class="{ unread: item.unread, link: item.links.length > 0 }"
              :title="item.lines.join('\n')"
              @click="select(item)"
            >
              <span class="tone" :data-tone="item.tone"><component :is="TONE[item.tone]" /></span>
              <div class="main">
                <div class="row">
                  <span class="ttl">{{ item.title }}</span>
                  <span class="time" :title="dateTime(item.lastAt)">{{ relative(item.lastAt) }}</span>
                </div>
                <p v-if="item.subject" class="sub">{{ item.subject }}</p>
                <p v-if="item.summary" class="sum">{{ item.summary }}</p>
                <div v-if="item.status || item.count > 1 || item.links.length > 1" class="meta">
                  <Badge v-if="item.status" :tone="STATUS[item.status].tone">{{ STATUS[item.status].label }}</Badge>
                  <span v-if="item.count > 1" class="times num">共 {{ item.count }} 次</span>
                  <button v-for="l in item.links.slice(1)" :key="l.kind + l.id" class="sublink" @click.stop="select(item, l)">{{ l.label }}</button>
                </div>
              </div>
            </div>
            <div v-if="hasNext" class="foot">
              <Btn size="sm" variant="ghost" :loading="more" @click="load(page + 1)">加载更多</Btn>
            </div>
          </template>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.bell-wrap {
  position: relative;
}
.bell {
  position: relative;
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  background: var(--fill-2);
  box-shadow: inset 0 0 0 0.5px var(--border);
  transition: background 0.15s, color 0.15s;
}
.bell:hover,
.bell.on {
  background: var(--fill);
  color: var(--text);
}
.bell svg {
  width: 15px;
  height: 15px;
}
.count {
  position: absolute;
  top: -5px;
  right: -6px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--red);
  color: #fff;
  font-size: 10px;
  font-weight: 650;
  line-height: 16px;
  text-align: center;
  box-shadow: 0 0 0 2px var(--topbar);
}
.panel {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: 384px;
  max-height: min(560px, calc(100vh - 80px));
  display: flex;
  flex-direction: column;
  border-radius: var(--radius-md);
  background: var(--popover);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 0.5px solid var(--hairline);
}
.head-title {
  font-weight: 650;
  margin-right: auto;
}
.clear {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 6px;
  border-radius: 6px;
  font-size: 12px;
  color: var(--accent);
}
.clear:hover:not(:disabled) {
  background: var(--fill-2);
}
.clear:disabled {
  color: var(--text-3);
  cursor: default;
}
.clear svg {
  width: 14px;
  height: 14px;
}
.body {
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 4px;
}
.skeletons {
  display: grid;
  gap: 6px;
  padding: 6px;
}
.state {
  padding: 36px 16px;
  text-align: center;
  color: var(--text-3);
  font-size: 12.5px;
}
.retry {
  margin-left: 8px;
  color: var(--accent);
}
.item {
  position: relative;
  display: flex;
  gap: 10px;
  padding: 9px 10px 9px 14px;
  border-radius: 8px;
}
.item.link {
  cursor: pointer;
}
.item:hover {
  background: var(--fill-2);
}
.item.unread::before {
  content: '';
  position: absolute;
  left: 4px;
  top: 15px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
.item + .item {
  margin-top: 1px;
}
.tone {
  flex-shrink: 0;
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  color: var(--text-2);
  background: var(--fill-2);
}
.tone svg {
  width: 14px;
  height: 14px;
}
.tone[data-tone='warning'] {
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 14%, transparent);
}
.tone[data-tone='error'],
.tone[data-tone='critical'] {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 14%, transparent);
}
.tone[data-tone='recovered'] {
  color: var(--green);
  background: color-mix(in srgb, var(--green) 14%, transparent);
}
.tone[data-tone='info'] {
  color: var(--accent);
  background: var(--accent-soft);
}
.main {
  min-width: 0;
  flex: 1;
}
.row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ttl {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item:not(.unread) .ttl {
  font-weight: 500;
  color: var(--text-2);
}
.time {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-3);
}
.sub,
.sum {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sum {
  color: var(--text-3);
  white-space: normal;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 5px;
}
.times {
  font-size: 11px;
  color: var(--text-3);
}
.sublink {
  font-size: 11.5px;
  color: var(--accent);
}
.sublink:hover {
  text-decoration: underline;
}
.foot {
  display: flex;
  justify-content: center;
  padding: 6px 0 4px;
}
.pop-enter-active,
.pop-leave-active {
  transition: opacity 0.16s, transform 0.2s var(--ease-out);
  transform-origin: top right;
}
.pop-enter-from,
.pop-leave-to {
  opacity: 0;
  transform: scale(0.96) translateY(-4px);
}
@media (max-width: 767px) {
  .panel {
    position: fixed;
    left: 8px;
    right: 8px;
    top: calc(var(--topbar-h) + env(safe-area-inset-top) + 6px);
    width: auto;
    max-height: calc(100vh - var(--topbar-h) - env(safe-area-inset-top) - var(--tabbar-h) - env(safe-area-inset-bottom) - 16px);
  }
}
</style>
