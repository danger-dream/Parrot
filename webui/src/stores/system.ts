import { reactive } from 'vue'
import { api } from '@/api/client'
import type { LoadBalancingData, OverviewData } from '@/api/types'

export interface OverviewExtras {
  publicIp?: string | null
  telegramBot?: { configured: boolean }
  update?: { available: boolean; latestVersion: string | null }
}

export type Overview = OverviewData & OverviewExtras

export const system = reactive({
  overview: null as Overview | null,
  loadBalancing: null as LoadBalancingData | null,
  loadedAt: 0,
  error: '' as string,
})

const MODE_LABEL: Record<string, string> = {
  smart: 'Smart',
  order: '顺序',
  priority: '优先级',
}

export function modeLabel(mode: string | null | undefined): string {
  if (!mode) return '—'
  return MODE_LABEL[mode] || mode
}

let inflight: Promise<void> | null = null

export function refreshSystem(): Promise<void> {
  if (inflight) return inflight
  inflight = Promise.all([api.get<Overview>('/overview'), api.get<LoadBalancingData>('/load-balancing')])
    .then(([ov, lb]) => {
      system.overview = ov.data
      system.loadBalancing = lb.data
      system.loadedAt = Date.now()
      system.error = ''
    })
    .catch((err: Error) => {
      system.error = err.message
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}
