/** Masking rules for privacy mode (utils/privacy): e-mails, phone numbers, API keys, IP addresses. */

const OCTET = '(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)'
const IPV4 = new RegExp(`(?<![\\d.])(${OCTET}\\.${OCTET})\\.${OCTET}\\.${OCTET}(?!\\.?\\d)`, 'g')
const IPV6 = /(?<![\w:.*])[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}(?![\w:*])/g
const EMAIL = /([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)/g
const KEY = /\b((?:ccp|sk|rk|pk)[-_])([A-Za-z0-9_-]{12,})/g
const BEARER = /\b(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}/g
const PHONE_CN = /(?<![\d*])1[3-9]\d{9}(?![\d*])/g
const PHONE_INTL = /(\+\d{1,3}[ -]?)(\d{8,14})(?!\d)/g

/** 18213053121 → 1*2****3**1: of the first three digits only the 1st and 3rd stay, of the last four the 1st and 4th. */
function maskPhoneDigits(d: string): string {
  return `${d[0]}*${d[2]}${'*'.repeat(d.length - 7)}${d[d.length - 4]}**${d[d.length - 1]}`
}

function maskEmail(_: string, local: string, domain: string): string {
  const head = local.length > 2 ? local.slice(0, 2) : local.slice(0, 1)
  const labels = domain.split('.')
  const tld = labels.pop()
  return `${head}***@${labels.map((l) => `${l.slice(0, 1)}***`).join('.')}.${tld}`
}

function maskIpv6(m: string): string {
  const groups = m.split(':')
  // Clock times ("02:40:08") and other colon runs without hex letters or "::" are not addresses.
  if (!/[a-fA-F]/.test(m) && !m.includes('::')) return m
  if (groups.some((g) => g.length > 4) || groups.filter(Boolean).length < 2) return m
  return `${groups.slice(0, 2).join(':')}:*:*`
}

export function maskText(text: string): string {
  if (!text || text.length < 7) return text
  return text
    .replace(KEY, (_, prefix: string, body: string) => `${prefix}••••${body.slice(-4)}`)
    .replace(BEARER, '$1••••')
    .replace(EMAIL, maskEmail)
    .replace(IPV4, '$1.*.*')
    .replace(IPV6, maskIpv6)
    .replace(PHONE_CN, maskPhoneDigits)
    .replace(PHONE_INTL, (_, prefix: string, digits: string) => prefix + maskPhoneDigits(digits))
}

const ID_TOKEN = /[A-Za-z0-9_-]{8,}/g

/** Opaque identifiers (account / org / project / device IDs) in elements marked `data-private`. */
export function maskIdentifiers(text: string): string {
  return maskText(text).replace(ID_TOKEN, (t) => (/\d/.test(t) ? `${t.slice(0, 4)}***${t.slice(-3)}` : t))
}
