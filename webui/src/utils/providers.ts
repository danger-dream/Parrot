// Provider icons are the Telegram custom emoji the bot uses, exported to static images.
import manifest from '@/assets/provider-icons/manifest.json'

interface Tile {
  label: string
  color: string
  glyph: string
}

const TILES: Record<string, Tile> = {
  claude: { label: 'Claude', color: '#d97757', glyph: 'C' },
  openai: { label: 'OpenAI', color: '#10a37f', glyph: 'O' },
  xai: { label: 'Grok', color: '#8e8e93', glyph: 'X' },
  cursor: { label: 'Cursor', color: '#7c7c86', glyph: '▲' },
  antigravity: { label: 'Antigravity', color: '#4285f4', glyph: 'G' },
  workbuddy: { label: 'WorkBuddy', color: '#14b8a6', glyph: 'W' },
  zhipu: { label: '智谱', color: '#3c6cff', glyph: '智' },
  qoder: { label: 'Qoder', color: '#a259ff', glyph: 'Q' },
  kimi: { label: 'Kimi', color: '#111827', glyph: 'K' },
  deepseek: { label: 'DeepSeek', color: '#4d6bfe', glyph: 'D' },
  minimax: { label: 'MiniMax', color: '#e5484d', glyph: 'M' },
}

const ALIASES: Record<string, string> = { anthropic: 'claude', grok: 'xai', moonshot: 'kimi', moonshotai: 'kimi', 'z.ai': 'zhipu', zai: 'zhipu' }

const files = manifest as Record<string, string>
const urls = import.meta.glob('@/assets/provider-icons/*.{png,webp,svg}', { eager: true, import: 'default' }) as Record<string, string>
const byFile: Record<string, string> = {}
for (const [path, url] of Object.entries(urls)) byFile[path.slice(path.lastIndexOf('/') + 1)] = url

export function providerKey(provider: string | null | undefined): string {
  const p = String(provider || '').trim().toLowerCase()
  return ALIASES[p] || p
}

export function providerIconUrl(provider: string | null | undefined): string {
  const file = files[providerKey(provider)]
  return (file && byFile[file]) || ''
}

export function providerTile(provider: string | null | undefined): Tile {
  const key = providerKey(provider)
  if (TILES[key]) return TILES[key]
  const raw = String(provider || '?')
  return { label: raw, color: 'var(--text-3)', glyph: raw.slice(0, 1).toUpperCase() }
}

/** Provider of a routing channel key: `oauth:<provider>:…` or an API channel's catalog provider. */
export function channelProvider(channelKey: string | null | undefined, apiProvider?: string | null): string {
  const key = String(channelKey || '')
  if (key.startsWith('oauth:')) return key.split(':')[1] || ''
  return providerKey(apiProvider || key.replace(/^(?:api|channel):/, '').split(':')[0])
}
