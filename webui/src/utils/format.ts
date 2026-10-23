export const TICKS_PER_USD = 10_000_000_000

const TZ = 'Asia/Shanghai'

/*
 * Number display rules — one per kind of value, used on every page:
 *   次数        integer   6,639
 *   Token 用量  compact   6.3B · 36.7M · 785.2M · 1.0K（缩写时固定 1 位小数；不足 1000 写整数）
 *   容量上限    capacity  1.05M · 1M · 262.1K · 128K（上下文 / 输出上限按原值，最多 2 位，去掉末尾 0）
 *   百分比      pct       99.1% · 100.0%（固定 1 位；没到 100% 时最多显示 99.9%）
 *   金额        usd       $3,258.42 · $0.69 · $0.00（汇总金额固定 2 位，千分位；大于 0 但不足 1 分写 <$0.01）
 *   单次金额    usdOne    $0.0042 · $1.2345（单次请求 / 单条日志的金额固定 4 位，否则大多只剩 <$0.01）
 *   单价        price     $0.15 · $0.003（每百万 Token，至少 2 位、最多 4 位）
 *   耗时        ms        983ms · 3.89s · 14.50s · 1m05s（不足 1 秒写毫秒，1 分钟内固定 2 位）
 *   速度        tps       53.9 t/s · 112.0 t/s · 4.2K t/s（固定 1 位）
 *   容量大小    bytes     86.4 MB · 1.5 GB（固定 1 位）
 *   任务进度    整数百分比 37%（进度条旁的完成度，不是统计值）
 */

export function compact(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  const units: [number, string][] = [
    [1e12, 'T'],
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K'],
  ]
  for (const [size, unit] of units) {
    if (abs >= size) return `${(n / size).toFixed(1)}${unit}`
  }
  return Math.round(n).toLocaleString('en-US')
}

/** Chart axis ticks are round numbers: “350M”, “1.8K”, “900”. */
export function axisNumber(n: number): string {
  return compact(n).replace(/\.0(?=[KMBT]$)/, '')
}

export function capacity(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(2)}M`
  if (n >= 1000) return `${+(n / 1000).toFixed(1)}K`
  return String(n)
}

export function integer(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  return Math.round(n).toLocaleString('en-US')
}

/** A 0–1 ratio as a percentage. */
export function pct(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return '—'
  let text = (ratio * 100).toFixed(1)
  // 99.96 % must not read as a full 100 %.
  if (ratio < 1 && Number(text) >= 100) text = '99.9'
  return `${text}%`
}

/** A value already on the 0–100 scale. */
export function pctValue(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : pct(value / 100)
}

export function percent(part: number, whole: number): string {
  return whole ? pct(part / whole) : '—'
}

export function usd(ticks: number | null | undefined): string {
  if (ticks === null || ticks === undefined || !Number.isFinite(ticks)) return '—'
  return dollars(ticks / TICKS_PER_USD)
}

/** The cost of one request or log entry. */
export function usdOne(ticks: number | null | undefined): string {
  if (ticks === null || ticks === undefined || !Number.isFinite(ticks)) return '—'
  return `$${(ticks / TICKS_PER_USD).toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 })}`
}

/** A dollar amount (not ticks), same rule as usd(). */
export function dollars(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  if (v > 0 && v < 0.005) return '<$0.01'
  const text = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return `${v < 0 ? '-' : ''}$${text}`
}

/** Unit price per million tokens. */
export function price(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  if (v === 0) return '免费'
  const two = v.toFixed(2)
  return `$${Math.abs(Number(two) - v) < 1e-9 ? two : String(+v.toFixed(4))}`
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value < 1000) return `${Math.round(value)}ms`
  if (value < 60_000) return `${(value / 1000).toFixed(2)}s`
  const m = Math.floor(value / 60_000)
  const s = Math.round((value % 60_000) / 1000)
  return `${m}m${s.toString().padStart(2, '0')}s`
}

/** Generation speed in tokens per second. */
export function tps(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return v >= 1000 ? `${(v / 1000).toFixed(1)}K t/s` : `${v.toFixed(1)} t/s`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.floor(seconds))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}天 ${h}小时`
  if (h > 0) return `${h}小时 ${m}分`
  if (m > 0) return `${m}分 ${s % 60}秒`
  return `${s}秒`
}

function parts(date: Date): Record<string, string> {
  const out: Record<string, string> = {}
  for (const p of new Intl.DateTimeFormat('zh-CN', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date)) {
    out[p.type] = p.value
  }
  return out
}

export function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  const d = value instanceof Date ? value : new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value)
  return Number.isNaN(d.getTime()) ? null : d
}

export function clock(value: string | number | Date | null | undefined, withSeconds = true): string {
  const d = toDate(value)
  if (!d) return '—'
  const p = parts(d)
  return withSeconds ? `${p.hour}:${p.minute}:${p.second}` : `${p.hour}:${p.minute}`
}

export function dateTime(value: string | number | Date | null | undefined): string {
  const d = toDate(value)
  if (!d) return '—'
  const p = parts(d)
  return `${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`
}

export function dayLabel(value: string | number | Date | null | undefined): string {
  const d = toDate(value)
  if (!d) return '—'
  const p = parts(d)
  return `${Number(p.month)}/${Number(p.day)}`
}

export function relative(value: string | number | Date | null | undefined, now = Date.now()): string {
  const d = toDate(value)
  if (!d) return '—'
  const diff = Math.round((now - d.getTime()) / 1000)
  if (diff < -60) return `${duration(-diff)}后`
  if (diff < 5) return '刚刚'
  if (diff < 60) return `${diff}秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`
  return `${Math.floor(diff / 86400)}天前`
}

export function shortChannel(id: string | null | undefined): string {
  if (!id) return '—'
  if (id.startsWith('api:')) return id.slice(4)
  if (id.startsWith('oauth:')) {
    const rest = id.slice(6)
    const [provider, ...tail] = rest.split(':')
    const ident = tail.join(':')
    if (provider === 'zhipu') return `智谱 · ${ident.split(':')[2]?.slice(-6) || '账号'}`
    return `${provider} · ${ident.split(':')[0]}`
  }
  return id
}

export function channelKind(id: string | null | undefined): 'oauth' | 'api' | 'other' {
  if (!id) return 'other'
  if (id.startsWith('oauth:')) return 'oauth'
  if (id.startsWith('api:')) return 'api'
  return 'other'
}

export function providerOf(id: string | null | undefined): string | null {
  if (!id) return null
  if (id.startsWith('oauth:')) return id.slice(6).split(':')[0]
  return null
}

/** “30 分钟”, “1 小时 30 分”, “2 天” — rounded, for hints next to second-based inputs. */
export function humanSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const s = Math.max(0, Math.round(value))
  if (s < 60) return `${s} 秒`
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (d) return h ? `${d} 天 ${h} 小时` : `${d} 天`
  if (h) return m ? `${h} 小时 ${m} 分` : `${h} 小时`
  return sec ? `${m} 分 ${sec} 秒` : `${m} 分钟`
}

/** Binary byte size: “86.4 MB”, “1.5 GB”. */
export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  let n = Math.max(0, value)
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(1)} ${units[i]}`
}

/** Full Beijing date-time, “2026-10-22 16:54”. */
export function fullTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const p: Record<string, string> = {}
  for (const x of new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(d))
    p[x.type] = x.value
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`
}

/** Random Idempotency-Key; crypto.randomUUID is missing on plain-HTTP (non-secure) origins. */
export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const buf = new Uint8Array(16)
  crypto.getRandomValues(buf)
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('')
}
