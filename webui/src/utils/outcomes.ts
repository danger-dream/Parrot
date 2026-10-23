import { api, type Schemas } from '@/api/client'

export type OutcomeItem = Schemas['OutcomeTimelineItemData']
export interface OutcomeTimeline {
  from: string
  step: number
  /** Buckets loaded (hours so far when loaded for today). */
  count: number
  items: Record<string, OutcomeItem>
}

/** Hours elapsed today in Beijing time, counting the current one. */
export function hoursToday(): number {
  return ((new Date().getUTCHours() + 8) % 24) + 1
}

/**
 * Finished requests per channel / API key / model, one bucket per clock hour: the last 24 hours,
 * or with `today` the hours since 00:00 Beijing time (same window as the other "today" cards).
 */
export async function loadOutcomeTimeline(dimension: 'channel' | 'apiKey' | 'model', opts: { today?: boolean } = {}): Promise<OutcomeTimeline | null> {
  try {
    const buckets = opts.today ? hoursToday() : 24
    const res = await api.get<Schemas['OutcomeTimelineData']>('/stats/outcome-timeline', { dimension, bucketMinutes: 60, buckets })
    return { from: res.data.from, step: res.data.bucketSeconds, count: buckets, items: Object.fromEntries(res.data.items.map((i) => [i.key, i])) }
  } catch {
    return null
  }
}
