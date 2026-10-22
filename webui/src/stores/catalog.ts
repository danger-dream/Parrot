import { reactive } from 'vue'
import { api } from '@/api/client'
import type { RuntimeStatusData } from '@/api/types'
import { shortChannel } from '@/utils/format'

export const catalog = reactive({
  channels: {} as Record<string, { name: string; type: string; protocol: string; health: string }>,
  status: null as RuntimeStatusData | null,
  loadedAt: 0,
})

let inflight: Promise<void> | null = null

export function refreshCatalog(force = false): Promise<void> {
  if (!force && Date.now() - catalog.loadedAt < 30_000) return Promise.resolve()
  if (inflight) return inflight
  inflight = api
    .get<RuntimeStatusData>('/runtime/status')
    .then((res) => {
      const map: typeof catalog.channels = {}
      for (const ch of res.data.channels) {
        map[ch.id] = { name: ch.name, type: ch.type, protocol: ch.protocol, health: ch.health }
      }
      catalog.channels = map
      catalog.status = res.data
      catalog.loadedAt = Date.now()
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}

export function channelName(id: string | null | undefined): string {
  if (!id) return '—'
  return catalog.channels[id]?.name || shortChannel(id)
}
