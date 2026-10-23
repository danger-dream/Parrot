import { api, type Schemas } from '@/api/client'

export type OutcomeItem = Schemas['OutcomeTimelineItemData']
export interface OutcomeTimeline {
  from: string
  step: number
  items: Record<string, OutcomeItem>
}

/** Last 24 hours of finished requests per channel / API key / model, one bucket per clock hour. */
export async function loadOutcomeTimeline(dimension: 'channel' | 'apiKey' | 'model'): Promise<OutcomeTimeline | null> {
  try {
    const res = await api.get<Schemas['OutcomeTimelineData']>('/stats/outcome-timeline', { dimension, bucketMinutes: 60, buckets: 24 })
    return { from: res.data.from, step: res.data.bucketSeconds, items: Object.fromEntries(res.data.items.map((i) => [i.key, i])) }
  } catch {
    return null
  }
}
