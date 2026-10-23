// Field descriptors for the flat system-settings resources. Ranges follow the backend
// schemas (src/management_api/schemas/system.py) and the Telegram input validation.

export interface FieldDef {
  key: string
  label: string
  hint: string
  type: 'int' | 'float' | 'bool' | 'select'
  min?: number
  max?: number
  unit?: string
  options?: { value: string; label: string }[]
  /** Caption shown when the value is 0, e.g. “不限”. */
  zeroText?: string
  humanize?: 'seconds' | 'minutes'
}

export const TIMEOUT_FIELDS: FieldDef[] = [
  { key: 'connect', label: '连接超时', type: 'int', min: 1, unit: '秒', humanize: 'seconds', hint: '与上游建立连接最多等待多久，超时算作该渠道失败。默认 10 秒。' },
  { key: 'firstByte', label: '首字超时', type: 'int', min: 1, unit: '秒', humanize: 'seconds', hint: '发出请求后等待上游返回第一段内容的最长时间；推理模型思考较久时可调大。默认 30 秒。' },
  { key: 'idle', label: '空闲超时', type: 'int', min: 1, unit: '秒', humanize: 'seconds', hint: '流式输出中两段内容之间的最长间隔，超过即认为上游卡住并中断。默认 120 秒。' },
  { key: 'total', label: '总时长上限', type: 'int', min: 1, unit: '秒', humanize: 'seconds', hint: '一次请求从开始到结束的最长时间，超过会被强制结束。默认 600 秒。' },
]

export const SCORING_FIELDS: FieldDef[] = [
  { key: 'emaAlpha', label: '平滑系数', type: 'float', min: 0, max: 1, hint: '新结果在评分里的权重（0–1）。越大越看重最近几次请求，越小评分越平稳。默认 0.25。' },
  { key: 'recentWindow', label: '统计窗口', type: 'int', min: 1, max: 1000, unit: '次', hint: '计算成功率时参考最近多少次请求（1–1000）。默认 50。' },
  { key: 'errorPenaltyFactor', label: '失败惩罚倍数', type: 'int', min: 0, max: 100, unit: '倍', hint: '失败率对评分的放大倍数（0–100）。越大，出错的渠道越快排到后面。默认 8。' },
  { key: 'explorationRate', label: '探索率', type: 'float', min: 0, max: 1, hint: '有多大比例的请求随机尝试非最优渠道（0–1），用于发现已恢复的渠道；0 表示总选最优。默认 0.2。' },
]

export const AFFINITY_FIELDS: FieldDef[] = [
  { key: 'ttlMinutes', label: '会话绑定时长', type: 'int', min: 1, max: 1440, unit: '分钟', humanize: 'minutes', hint: '同一会话在这段时间内尽量继续使用上次的渠道，以提高提示词缓存命中（1–1440 分钟）。默认 30 分钟。' },
]

export const CCH_FIELDS: FieldDef[] = [
  {
    key: 'mode',
    label: 'Claude Code 伪装头',
    type: 'select',
    options: [
      { value: 'disabled', label: '关闭' },
      { value: 'dynamic', label: '动态计算' },
    ],
    hint: '关闭：不发送伪装头。动态计算：按每次请求内容计算校验值并附带在请求里。',
  },
]

export const CONCURRENCY_FIELDS: FieldDef[] = [
  { key: 'enabled', label: '启用渠道并发限制', type: 'bool', hint: '开启后，每个渠道同时处理的请求数受上限约束，超出的请求排队等待。' },
  { key: 'queueWaitSeconds', label: '排队等待', type: 'int', min: 0, unit: '秒', humanize: 'seconds', zeroText: '不排队', hint: '渠道全满时请求最多排队多久，超时返回 429；0 表示不排队直接返回。默认 30 秒。' },
  { key: 'defaultMaxConcurrent', label: '默认并发上限', type: 'int', min: 0, zeroText: '不限', hint: '渠道没有单独设置并发上限时使用这个值；0 表示不限。各渠道的上限在渠道详情里设置。' },
]

export const API_KEY_CONCURRENCY_FIELDS: FieldDef[] = [
  { key: 'enabled', label: '启用 API Key 限流', type: 'bool', hint: '开启后，每个下游 API Key 的并发和排队受下面的默认值约束（Key 可单独覆盖）。' },
  { key: 'defaultMaxConcurrent', label: '默认并发上限', type: 'int', min: 0, zeroText: '不限', hint: '每个 Key 同时处理的请求数；0 表示不限。默认 5。' },
  { key: 'defaultMaxQueue', label: '默认排队上限', type: 'int', min: 0, zeroText: '不排队', hint: '每个 Key 超出并发后最多排队多少个请求；0 表示不排队。默认 50。' },
  { key: 'defaultQueueWaitSeconds', label: '默认最长等待', type: 'int', min: 0, unit: '秒', humanize: 'seconds', zeroText: '不等待', hint: '排队中的请求最多等待多久；0 表示不等待。默认 1800 秒（30 分钟）。' },
]

export const QUOTA_MONITOR_FIELDS: FieldDef[] = [
  { key: 'enabled', label: '启用额度监控', type: 'bool', hint: '定时读取每个 OAuth 账号的用量，任一指标达到阈值就自动暂停该账号，额度重置后自动恢复。' },
  { key: 'intervalSeconds', label: '检查间隔', type: 'int', min: 10, max: 86400, unit: '秒', humanize: 'seconds', hint: '多久读取一次用量（10–86400 秒）。建议不少于 60 秒，过于频繁可能触发官方风控。' },
  { key: 'thresholdPercent', label: '暂停阈值', type: 'float', min: 1, max: 100, unit: '%', hint: '5 小时、7 天等任一用量指标达到这个百分比就暂停账号（1–100）。常用 90 / 95 / 99。' },
]

export const WEBSOCKET_FIELDS: FieldDef[] = [
  {
    key: 'responsesUpstreamWsForOAuth',
    label: 'HTTP 请求改走 WebSocket 上游',
    type: 'bool',
    hint: '客户端用 HTTP 调用 /v1/responses 且选中 OpenAI OAuth 账号时，改用 WebSocket 连接上游。普通 OpenAI API 渠道和其他接口不受影响；下游的 WebSocket 接口始终可用。',
  },
]
