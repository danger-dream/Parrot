import { reactive } from 'vue'
import { api, type Schemas } from '@/api/client'
import { onEvent } from '@/api/events'

export type NotificationItem = Schemas['NotificationItemData']
export type NotificationView = 'unread' | 'read'

interface Page {
  items: NotificationItem[]
  unreadCount: number
}

export const notifications = reactive({
  unread: 0,
  /** Bumped on every server-side change so an open list knows to reload. */
  version: 0,
})

let timer: number | undefined

export async function refreshUnread(): Promise<void> {
  try {
    const r = await api.get<Page>('/notifications', { view: 'unread', pageSize: 1 })
    notifications.unread = r.data.unreadCount
  } catch {
    /* the badge keeps its last known value */
  }
}

function changed(): void {
  // Bursts (one sweep pausing several accounts) collapse into one read.
  window.clearTimeout(timer)
  timer = window.setTimeout(() => {
    notifications.version += 1
    void refreshUnread()
  }, 400)
}

onEvent('notification.created', changed)
onEvent('hello', changed)

export async function fetchNotifications(view: NotificationView, page: number, pageSize = 30) {
  const r = await api.get<Page>('/notifications', { view, page, pageSize })
  notifications.unread = r.data.unreadCount
  return { items: r.data.items, hasNext: Boolean(r.meta.hasNext), total: r.meta.total ?? r.data.items.length }
}

export async function markRead(ids: string[], all = false): Promise<void> {
  const r = await api.post<{ unreadCount: number }>('/notifications/actions/mark-read', { ids, all })
  notifications.unread = r.data.unreadCount
}
