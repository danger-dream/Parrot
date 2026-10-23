export type Tone = 'green' | 'red' | 'orange' | 'blue' | 'neutral'

export function statusTone(status: string | null | undefined): Tone {
  if (status === 'success') return 'green'
  if (status === 'error') return 'red'
  if (status === 'cancelled') return 'orange'
  if (status === 'pending') return 'blue'
  return 'neutral'
}

export function statusLabel(status: string | null | undefined): string {
  return (
    { success: '成功', error: '失败', cancelled: '已取消', pending: '进行中' }[status || ''] || status || '未知'
  )
}

export function protocolLabel(protocol: string | null | undefined): string {
  return (
    {
      anthropic: 'Anthropic',
      chat: 'Chat',
      responses: 'Responses',
      responses_ws: 'Responses WS',
      'openai-chat': 'Chat',
      'openai-responses': 'Responses',
    }[protocol || ''] ||
    protocol ||
    '—'
  )
}

const KIND_LABEL: Record<string, string> = {
  params: '参数',
  metadata: '元数据',
  tools: '工具定义',
  system: '系统提示',
  user: '用户',
  assistant: '助手',
  tool_call: '工具调用',
  tool_use: '工具调用',
  tool_result: '工具结果',
  usage: '用量',
  thinking: '思考',
  reasoning: '推理',
  text: '文本',
  image: '图片',
  error: '错误',
  headers: '请求头',
  event: '事件',
}

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] || kind
}

export function kindTone(kind: string): Tone {
  if (kind === 'user') return 'blue'
  if (kind === 'assistant' || kind === 'text') return 'green'
  if (kind === 'tool_call' || kind === 'tool_use' || kind === 'tool_result') return 'orange'
  if (kind === 'error') return 'red'
  return 'neutral'
}
