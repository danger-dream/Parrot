import { Clapperboard, FileText, Globe, ImagePlus, Paintbrush, Film } from '@lucide/vue'
import type { Component } from 'vue'
export { bytes } from '@/utils/format'

export const TOOLS: { name: string; label: string; desc: string; icon: Component; subject: [string, string] }[] = [
  { name: 'web_search', label: '网络搜索', desc: '按搜索词检索网页，使用「搜索」页配置的来源', icon: Globe, subject: ['query', '搜索词'] },
  { name: 'web_fetch', label: '网页抓取', desc: '读取指定网址的正文内容', icon: FileText, subject: ['url', '网址'] },
  { name: 'image_generate', label: '图片生成', desc: '根据提示词生成图片', icon: ImagePlus, subject: ['prompt', '提示词'] },
  { name: 'image_edit', label: '图片编辑', desc: '基于上传的图片和提示词修改图片', icon: Paintbrush, subject: ['prompt', '提示词'] },
  { name: 'video_generate', label: '视频生成', desc: '根据提示词提交视频生成任务', icon: Clapperboard, subject: ['prompt', '提示词'] },
  { name: 'video_status', label: '视频查询', desc: '查询视频任务进度并取回结果', icon: Film, subject: ['request_id', '任务'] },
]

export function toolLabel(name: string | null | undefined): string {
  return TOOLS.find((t) => t.name === name)?.label || name || '—'
}

export function toolIcon(name: string | null | undefined): Component {
  return TOOLS.find((t) => t.name === name)?.icon || Globe
}

export const STATUS_LABEL: Record<string, string> = {
  running: '进行中',
  success: '成功',
  error: '失败',
  timeout: '超时',
  denied: '已拒绝',
}

export const STATUS_TONE: Record<string, 'green' | 'red' | 'orange' | 'blue' | 'neutral'> = {
  running: 'blue',
  success: 'green',
  error: 'red',
  timeout: 'orange',
  denied: 'orange',
}

const ERROR_LABEL: Record<string, string> = {
  api_key_limit_queue_full: 'Key 并发排队已满',
  empty_extract_response: '抓取结果为空',
  invalid_search_input: '搜索参数不正确',
  search_upstream_error: '搜索上游返回错误',
  tool_error: '工具执行出错',
  tool_disabled: '工具已关闭',
  mcp_not_allowed: '该 Key 未开启 MCP',
}
export function errorLabel(code: string | null | undefined): string {
  if (!code) return ''
  return ERROR_LABEL[code] || code
}

/** The one input that tells what a call was about (query / URL / prompt / task id). */
export function subjectOf(log: { toolName: string; params?: unknown }): { label: string; value: string } {
  const tool = TOOLS.find((t) => t.name === log.toolName)
  const [field, label] = tool?.subject || ['prompt', '输入']
  const p = log.params
  if (p && typeof p === 'object' && !Array.isArray(p)) {
    const v = (p as Record<string, unknown>)[field]
    if (v !== undefined && v !== null && v !== '') return { label, value: String(v) }
  }
  return { label, value: '' }
}

