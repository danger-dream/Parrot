import { watch } from 'vue'
import { ui } from '@/stores/ui'
import { maskIdentifiers, maskText } from './mask'

/**
 * Privacy mode: masks e-mail addresses, phone numbers, API keys and IP addresses wherever they
 * are rendered — pages, drawers, dialogs, toasts and tooltips — by rewriting text nodes and
 * `title` attributes in place, so no component has to opt in. Originals are kept per node and
 * put back when the mode is switched off; copy buttons still copy the real value. Input fields
 * cannot be rewritten without breaking editing, so a sensitive value is blurred until focused.
 */

interface Saved {
  orig: string
  masked: string
}
const texts = new WeakMap<Text, Saved>()
const attrs = new WeakMap<Element, Map<string, Saved>>()
const ATTRS = ['title', 'aria-label']
const SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'NOSCRIPT'])
const PLAIN_INPUTS = new Set(['password', 'checkbox', 'radio', 'range', 'color', 'file', 'hidden', 'button', 'submit'])

let enabled = false
let observer: MutationObserver | null = null
let inputsQueued = false

function maskTextNode(node: Text): void {
  const parent = node.parentElement
  if (!parent || SKIP.has(parent.tagName)) return
  const value = node.nodeValue || ''
  const saved = texts.get(node)
  if (saved && value === saved.masked) return
  const masked = parent.closest('[data-private]') ? maskIdentifiers(value) : maskText(value)
  if (masked === value) {
    texts.delete(node)
    return
  }
  texts.set(node, { orig: value, masked })
  node.nodeValue = masked
}

function maskAttr(el: Element, name: string): void {
  const value = el.getAttribute(name)
  let saved = attrs.get(el)
  const prev = saved?.get(name)
  if (value === null || (prev && value === prev.masked)) return
  const masked = maskText(value)
  if (masked === value) {
    saved?.delete(name)
    return
  }
  if (!saved) attrs.set(el, (saved = new Map()))
  saved.set(name, { orig: value, masked })
  el.setAttribute(name, masked)
}

function scan(node: Node): void {
  if (node.nodeType === Node.TEXT_NODE) {
    maskTextNode(node as Text)
    return
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return
  const root = node as Element
  for (const name of ATTRS) maskAttr(root, name)
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) maskTextNode(n as Text)
    else for (const name of ATTRS) maskAttr(n as Element, name)
  }
}

function updateInput(el: Element | null): void {
  if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) return
  if (el instanceof HTMLInputElement && PLAIN_INPUTS.has(el.type)) return
  const sensitive = enabled && document.activeElement !== el && maskText(el.value) !== el.value
  el.classList.toggle('pm-blur', sensitive)
}

function queueInputs(): void {
  if (inputsQueued) return
  inputsQueued = true
  requestAnimationFrame(() => {
    inputsQueued = false
    document.querySelectorAll('input, textarea').forEach(updateInput)
  })
}

function onRecords(records: MutationRecord[]): void {
  for (const r of records) {
    if (r.type === 'characterData') maskTextNode(r.target as Text)
    else if (r.type === 'attributes' && r.attributeName) maskAttr(r.target as Element, r.attributeName)
    else r.addedNodes.forEach(scan)
  }
  queueInputs()
}

function onInputEvent(e: Event): void {
  updateInput(e.target as Element)
}

function enable(): void {
  enabled = true
  scan(document.body)
  observer = new MutationObserver(onRecords)
  observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS })
  document.addEventListener('focusin', onInputEvent, true)
  document.addEventListener('focusout', onInputEvent, true)
  document.addEventListener('input', onInputEvent, true)
  queueInputs()
}

function disable(): void {
  enabled = false
  observer?.disconnect()
  observer = null
  document.removeEventListener('focusin', onInputEvent, true)
  document.removeEventListener('focusout', onInputEvent, true)
  document.removeEventListener('input', onInputEvent, true)
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) {
      const saved = texts.get(n as Text)
      if (saved && n.nodeValue === saved.masked) n.nodeValue = saved.orig
      texts.delete(n as Text)
      continue
    }
    const el = n as Element
    const saved = attrs.get(el)
    if (saved) {
      for (const [name, s] of saved) if (el.getAttribute(name) === s.masked) el.setAttribute(name, s.orig)
      attrs.delete(el)
    }
    if (el.classList.contains('pm-blur')) el.classList.remove('pm-blur')
  }
}

export function initPrivacy(): void {
  watch(
    () => ui.privacy,
    (on) => {
      document.documentElement.classList.toggle('privacy', on)
      if (on && !enabled) enable()
      else if (!on && enabled) disable()
    },
    { immediate: true },
  )
}
