// One batch for the whole overview: every card renders from the same snapshot, so the page paints
// once instead of block by block (the old per-card loading made the layout jump several times).
import { reactive } from 'vue'
import { api, type Schemas } from '@/api/client'
import type { OAuthAccountSummaryData, StatsBreakdownData, StatsSummaryData, TimePoint, TimeSeriesData } from '@/api/types'
import { refreshCatalog } from '@/stores/catalog'
import { loadCompare, type Compare } from '@/utils/compare'

export type Outcome = Schemas['RecentOutcomeData']

export const ov = reactive({
  ready: false,
  loading: false,
  loadedAt: 0,
  summary: null as StatsSummaryData | null,
  hourly: [] as TimePoint[],
  tenMinutes: [] as TimePoint[],
  models: [] as StatsBreakdownData[],
  channels: [] as StatsBreakdownData[],
  outcomes: {} as Record<string, Outcome>,
  accounts: [] as OAuthAccountSummaryData[],
  /** Today against yesterday up to the same time; also today's sparklines. */
  compare: null as Compare | null,
})

function totalSeries(res: { data: TimeSeriesData } | null): TimePoint[] {
  if (!res) return []
  return res.data.series.find((s) => s.key === 'total')?.points || res.data.series[0]?.points || []
}

export async function loadOverview(): Promise<void> {
  if (ov.loading) return
  ov.loading = true
  try {
    const [summary, hourly, tenMinutes, models, channels, outcomes, accounts, , compare] = await Promise.all([
      api.get<StatsSummaryData>('/stats/summary', { period: 'today' }).catch(() => null),
      api.get<TimeSeriesData>('/stats/timeseries', { range: '24h', bucket: 'hour' }).catch(() => null),
      api.get<TimeSeriesData>('/stats/timeseries', { range: '24h', bucket: '10m' }).catch(() => null),
      api.get<StatsBreakdownData[]>('/stats/breakdown', { dimension: 'model', period: 'today', sort: 'cost', pageSize: 8 }).catch(() => null),
      api.get<StatsBreakdownData[]>('/stats/breakdown', { dimension: 'channel', period: 'today', sort: 'tokens', pageSize: 200 }).catch(() => null),
      api.get<Outcome[]>('/stats/recent-outcomes', { dimension: 'channel', limit: 20 }).catch(() => null),
      api.get<{ items: OAuthAccountSummaryData[] }>('/oauth/accounts', { pageSize: 200 }).catch(() => null),
      refreshCatalog(true).catch(() => undefined),
      loadCompare({ period: 'today' }).catch(() => null),
    ])
    if (summary) ov.summary = summary.data
    if (hourly) ov.hourly = totalSeries(hourly)
    if (tenMinutes) ov.tenMinutes = totalSeries(tenMinutes)
    if (models) ov.models = models.data
    if (channels) ov.channels = channels.data
    if (outcomes) ov.outcomes = Object.fromEntries(outcomes.data.map((o) => [o.key, o]))
    if (accounts) ov.accounts = accounts.data.items
    if (compare) ov.compare = compare
    ov.loadedAt = Date.now()
    ov.ready = true
  } finally {
    ov.loading = false
  }
}
