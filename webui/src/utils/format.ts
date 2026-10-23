export const TICKS_PER_USD = 10_000_000_000

const TZ = 'Asia/Shanghai'

export function compact(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  const units: [number, string][] = [
    [1e12, 'T'],
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K'],
  ]
  for (const [size, unit] of units) {
    if (abs >= size) {
      const v = n / size
      return `${v.toFixed(v >= 100 ? 0 : digits).replace(/\.0+$/, '')}${unit}`
    }
  }
  return Math.round(n).toLocaleString('en-US')
}

export function integer(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  return Math.round(n).toLocaleString('en-US')
}

export function usd(ticks: number | null | undefined, opts: { precise?: boolean } = {}): string {
  if (ticks === null || ticks === undefined || !Number.isFinite(ticks)) return '—'
  const v = ticks / TICKS_PER_USD
  if (v === 0) return '$0'
  const abs = Math.abs(v)
  if (abs >= 10_000 && !opts.precise) return `$${compact(v, 1)}`
  if (abs >= 100) return `$${v.toLocaleString('en-US', { maximumFractionDigits: opts.precise ? 2 : 0 })}`
  if (abs >= 1) return `$${v.toFixed(2)}`
  if (abs >= 0.01) return `$${v.toFixed(3)}`
  return `$${v.toPrecision(2)}`
}

export function percent(part: number, whole: number, digits = 1): string {
  if (!whole) return '—'
  const v = (part / whole) * 100
  if (v >= 99.95 && part !== whole) return `${(99.9).toFixed(digits)}%`
  return `${v.toFixed(digits)}%`
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value < 1000) return `${Math.round(value)}ms`
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)}s`
  const m = Math.floor(value / 60_000)
  const s = Math.round((value % 60_000) / 1000)
  return `${m}m${s.toString().padStart(2, '0')}s`
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

/** Binary byte size: “1.2 GB”. */
export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  let n = Math.max(0, value)
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return i === 0 ? `${n} B` : `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`
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
