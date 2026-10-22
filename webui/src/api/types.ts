import type { Schemas } from './client'

export type OverviewData = Schemas['OverviewData']
export type StatsSummaryData = Schemas['StatsSummaryData']
export type StatsBreakdownData = Schemas['StatsBreakdownData']
export type StatsMetricData = Schemas['StatsMetricData']
export type RequestLogData = Schemas['RequestLogData']
export type RequestLogDetailData = Schemas['RequestLogDetailData']
export type RequestLogFilterOptionsData = Schemas['RequestLogFilterOptionsData']
export type RuntimeStatusData = Schemas['RuntimeStatusData']
export type ChannelStatusData = Schemas['ChannelStatusData']
export type ConcurrencyData = Schemas['ConcurrencyData']
export type OAuthAccountSummaryData = Schemas['OAuthAccountSummaryData']
export type OAuthAccountDetailData = Schemas['OAuthAccountDetailData']
export type ChannelData = Schemas['ChannelData']
export type LoadBalancingData = Schemas['LoadBalancingData']
export type StatusIncidentData = Schemas['StatusIncidentData']

export type StatsPeriod = StatsSummaryData['period']

export interface LoginOptions {
  webUiEnabled: boolean
  telegramApproval: { available: boolean; ttlSeconds: number }
  managementKey: { available: boolean }
}

export interface TelegramApprovalCreated {
  approvalId: string
  exchangeSecret: string
  expiresAt: string
  pollAfterSeconds: number
  confirmCode?: string
}

export type ApprovalStatus = 'pending' | 'approved' | 'denied' | 'expired' | 'consumed'

export interface TelegramApprovalStatus {
  approvalId: string
  status: ApprovalStatus
  expiresAt: string
  pollAfterSeconds: number
}

export interface SessionSummary {
  sessionId: string
  subjectId: string
  authMethod: string
  issuedAt: string
  expiresAt: string
  idleExpiresAt: string
}

export interface LoginSession {
  sessionId: string
  authMethod: string
  clientName: string | null
  deviceSummary: string | null
  sourceAddress: string | null
  issuedAt: string
  lastSeenAt: string
  expiresAt: string
  idleExpiresAt: string
  current: boolean
}

export type TimeRange = '1h' | '24h' | '7d' | '30d'
export type TimeBucket = 'minute' | 'hour' | 'day'
export type TimeGroupBy = 'none' | 'model' | 'channel' | 'apiKey'

export interface TimePoint {
  t: string
  total: number
  successCount: number
  errorCount: number
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheCreationTokens: number
  costTicks: number
  averageFirstTokenMilliseconds: number | null
  averageTotalMilliseconds: number | null
}

export interface TimeSeries {
  key: string
  label?: string
  points: TimePoint[]
}

export interface TimeSeriesData {
  range: TimeRange
  bucket: TimeBucket
  bucketSeconds: number
  from: string
  to: string
  timezone: string
  series: TimeSeries[]
  revision: string
}

export interface RequestStartedEvent {
  id: string
  createdAt: string
  apiKeyName: string | null
  requestedModel: string | null
  protocol: string | null
  isStream: boolean
  clientIp: string | null
  reasoningEffort: string | null
  fastMode: boolean | null
}

export interface RequestUpdatedEvent {
  id: string
  channelId?: string | null
  model?: string | null
  retryCount?: number
}

export interface ServerEventMap {
  hello: { serverTime: string; version: string; sessionExpiresAt: string }
  'request.started': RequestStartedEvent
  'request.updated': RequestUpdatedEvent
  'request.finished': { log: RequestLogData }
  'runtime.snapshot': ConcurrencyData
  resync: { reason?: 'overflow' | 'enrichmentFailed' }
  ping: Record<string, never>
}

export type ServerEventType = keyof ServerEventMap
