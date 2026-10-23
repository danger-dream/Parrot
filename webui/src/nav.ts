import type { Component } from 'vue'
import {
  Activity,
  Ban,
  BellRing,
  Boxes,
  ChartColumn,
  CircleArrowUp,
  Database,
  Film,
  Globe,
  Image,
  KeyRound,
  Languages,
  LayoutDashboard,
  MonitorSmartphone,
  Network,
  Puzzle,
  Scale,
  Search,
  Settings2,
  ShieldCheck,
  Waypoints,
} from '@lucide/vue'

export interface NavItem {
  key: string
  label: string
  icon: Component
  tint: string
  to: string
  phase: 1 | 2 | 3
  summary?: string
  features?: string[]
  /** Routed but kept out of the sidebar, tab bar and command palette. */
  hidden?: boolean
}

export interface NavGroup {
  label: string
  items: NavItem[]
}

export const navGroups: NavGroup[] = [
  {
    label: '监控',
    items: [
      { key: 'overview', label: '概览', icon: LayoutDashboard, tint: 'var(--blue)', to: '/', phase: 1 },
      { key: 'live', label: '实时请求', icon: Activity, tint: 'var(--teal)', to: '/live', phase: 1 },
      {
        key: 'analytics',
        label: '统计分析',
        icon: ChartColumn,
        tint: 'var(--purple)',
        to: '/analytics',
        phase: 3,
        summary: '任意时间范围的用量与费用分析。',
        features: ['按模型、渠道、Key 拆分', '实际费用与估算费用', 'Token 与缓存命中', '首字耗时与输出速度'],
      },
      {
        key: 'media',
        label: '媒体日志',
        icon: Image,
        tint: 'var(--pink)',
        to: '/media',
        phase: 3,
        summary: '图片与视频生成记录。',
        features: ['生成、编辑、延长记录', '产物预览与下载', '失败原因查看'],
      },
    ],
  },
  {
    label: '资源',
    items: [
      {
        key: 'oauth',
        label: 'OAuth 账户',
        icon: ShieldCheck,
        tint: 'var(--orange)',
        to: '/oauth',
        phase: 2,
        summary: '官方账号池与额度管理。',
        features: [
          '各账号 5 小时 / 7 天额度与重置倒计时',
          'Claude、Cursor、OpenAI、xAI、Antigravity、Qoder、智谱、WorkBuddy 登录',
          '从 cpa / sub2api 导入，失效账号批量清理',
          '账号模型管理与拖动排序',
        ],
      },
      {
        key: 'channels',
        label: 'API 渠道',
        icon: Waypoints,
        tint: 'var(--green)',
        to: '/channels',
        phase: 2,
        summary: '第三方 API 渠道的接入与诊断。',
        features: ['新建向导：品牌预设、模型发现、连通性测试', '兼容性开关', '冷却与亲和清理', '渠道额度查询'],
      },
      {
        key: 'keys',
        label: '下游 API Key',
        icon: KeyRound,
        tint: 'var(--yellow)',
        to: '/keys',
        phase: 2,
        summary: '分发给客户端的访问密钥。',
        features: ['创建与重新生成（只显示一次）', '允许使用的模型', '并发与排队限制', '单个 Key 的用量统计'],
      },
      {
        key: 'proxies',
        label: '代理',
        icon: Globe,
        tint: 'var(--teal)',
        to: '/proxies',
        phase: 2,
        summary: '出口代理与分流规则。',
        features: ['代理与代理组', '连通性测试', '按功能、账号、渠道、模型分流'],
      },
    ],
  },
  {
    label: '模型与路由',
    items: [
      {
        key: 'models',
        label: '模型中心',
        icon: Boxes,
        tint: 'var(--indigo)',
        to: '/models',
        phase: 3,
        summary: '所有可用模型的统一管理。',
        features: ['模型库存、启停与可见性', '元数据与定价绑定', '默认推理强度'],
      },
      {
        key: 'routing',
        label: '负载均衡',
        icon: Scale,
        tint: 'var(--blue)',
        to: '/routing',
        phase: 3,
        summary: '请求如何分配到各个渠道。',
        features: ['Smart / 顺序 / 优先级', '全局与按模型的渠道顺序（拖动调整）', '会话亲和清理'],
      },
    ],
  },
  {
    label: '扩展',
    items: [
      {
        key: 'search',
        label: '搜索',
        icon: Search,
        tint: 'var(--blue)',
        to: '/search',
        phase: 3,
        summary: '联网搜索后端与调用记录。',
        features: ['搜索后端与优先级', '测试搜索', '调用日志与统计'],
      },
      {
        key: 'mcp',
        label: 'MCP',
        icon: Puzzle,
        tint: 'var(--green)',
        to: '/mcp',
        phase: 3,
        summary: 'MCP 工具开关与调用记录。',
        features: ['工具启停', '调用日志与统计'],
      },
      {
        key: 'translation',
        label: '翻译',
        icon: Languages,
        tint: 'var(--orange)',
        to: '/translation',
        phase: 3,
        hidden: true,
        summary: '提示词翻译设置。',
        features: ['主模型与备用模型', '作用范围', '测试与缓存清理'],
      },
      {
        key: 'imaging',
        label: '图片与视频',
        icon: Film,
        tint: 'var(--pink)',
        to: '/imaging',
        phase: 3,
        summary: '图片、视频生成的模型与缓存设置。',
        features: ['模型与来源', '缓存与保留', 'xAI 媒体设置'],
      },
    ],
  },
  {
    label: '系统',
    items: [
      { key: 'devices', label: '登录设备', icon: MonitorSmartphone, tint: 'var(--teal)', to: '/devices', phase: 1 },
      {
        key: 'settings',
        label: '系统设置',
        icon: Settings2,
        tint: 'var(--text-2)',
        to: '/settings',
        phase: 3,
        summary: '运行参数集中设置。',
        features: ['重试、超时、冷却阶梯', '评分、亲和、CCH', '并发与额度监控', '通知与 OpenAI WS'],
      },
      {
        key: 'network',
        label: '网络',
        icon: Network,
        tint: 'var(--blue)',
        to: '/network',
        phase: 3,
        summary: 'DNS、SOCKS5 与网络监控。',
        features: ['先测试再保存', '网络监控与历史'],
      },
      {
        key: 'alerts',
        label: '状态告警',
        icon: BellRing,
        tint: 'var(--red)',
        to: '/alerts',
        phase: 3,
        summary: '上游服务状态告警。',
        features: ['告警目标与阈值', '事件静音'],
      },
      {
        key: 'blacklist',
        label: '内容黑名单',
        icon: Ban,
        tint: 'var(--red)',
        to: '/blacklist',
        phase: 3,
        summary: '按渠道屏蔽的关键词。',
        features: ['默认规则', '按渠道规则'],
      },
      {
        key: 'retention',
        label: '日志保留',
        icon: Database,
        tint: 'var(--text-2)',
        to: '/retention',
        phase: 3,
        summary: '请求日志与正文的保存期限。',
        features: ['正文保存开关', '先预览再清理'],
      },
      {
        key: 'updates',
        label: '版本更新',
        icon: CircleArrowUp,
        tint: 'var(--green)',
        to: '/updates',
        phase: 3,
        summary: '在线更新 Parrot。',
        features: ['检查新版本', '下载、重启、自动健康检查', '失败自动回滚'],
      },
    ],
  },
]

export const navItems: NavItem[] = navGroups.flatMap((g) => g.items)
export const visibleNavGroups: NavGroup[] = navGroups.map((g) => ({ ...g, items: g.items.filter((i) => !i.hidden) }))
export const visibleNavItems: NavItem[] = navItems.filter((i) => !i.hidden)

export function findNav(key: string): NavItem | undefined {
  return navItems.find((i) => i.key === key)
}
