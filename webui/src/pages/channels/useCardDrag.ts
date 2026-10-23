import { ref, type Ref } from 'vue'

/**
 * Drag-to-reorder for a grid of cards (mouse and touch). Each card carries `data-drag-id`; the
 * drag starts from a handle via `start(event, id)`. The order is updated live while dragging and
 * `onDrop(next, previous)` runs once when the pointer is released over a changed order.
 */
export function useCardDrag(order: Ref<string[]>, onDrop: (next: string[], previous: string[]) => void) {
  const dragging = ref('')

  function scroller(el: HTMLElement | null): HTMLElement | null {
    for (let n = el?.parentElement || null; n; n = n.parentElement) {
      const s = getComputedStyle(n).overflowY
      if ((s === 'auto' || s === 'scroll') && n.scrollHeight > n.clientHeight) return n
    }
    return null
  }

  function start(e: PointerEvent, id: string): void {
    if (e.button !== 0 || dragging.value) return
    e.preventDefault()
    e.stopPropagation()
    const handle = e.currentTarget as HTMLElement
    const grid = handle.closest<HTMLElement>('[data-drag-grid]')
    if (!grid) return
    const before = [...order.value]
    const scroll = scroller(grid)
    let lastY = e.clientY
    let raf = 0
    dragging.value = id

    const autoScroll = () => {
      if (!scroll || !dragging.value) return
      const r = scroll.getBoundingClientRect()
      const edge = 64
      const dy = lastY < r.top + edge ? -12 : lastY > r.bottom - edge ? 12 : 0
      if (dy) scroll.scrollTop += dy
      raf = requestAnimationFrame(autoScroll)
    }
    raf = requestAnimationFrame(autoScroll)

    let lastSwap = 0
    const onMove = (ev: PointerEvent) => {
      lastY = ev.clientY
      if (performance.now() - lastSwap < 120) return
      // Use layout boxes (offset*), not getBoundingClientRect: cards animate with transforms
      // while the list reflows, and their visual boxes would make the target flip back and forth.
      const g = grid.getBoundingClientRect()
      const x = ev.clientX - g.left
      const y = ev.clientY - g.top
      const cards = [...grid.querySelectorAll<HTMLElement>('[data-drag-id]')]
      const over = cards.find((c) => {
        const left = c.offsetLeft - grid.offsetLeft
        const top = c.offsetTop - grid.offsetTop
        return x >= left && x <= left + c.offsetWidth && y >= top && y <= top + c.offsetHeight
      })
      const target = over?.dataset.dragId
      if (!target || target === id) return
      const next = order.value.filter((v) => v !== id)
      const at = next.indexOf(target)
      const from = order.value.indexOf(id)
      const to = order.value.indexOf(target)
      next.splice(from < to ? at + 1 : at, 0, id)
      order.value = next
      lastSwap = performance.now()
    }
    const onUp = () => {
      cancelAnimationFrame(raf)
      dragging.value = ''
      document.body.style.userSelect = prevSelect
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      if (order.value.join('\n') !== before.join('\n')) onDrop([...order.value], before)
    }
    // Listen on the window rather than capturing the pointer: the card re-renders while dragging.
    const prevSelect = document.body.style.userSelect
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  return { dragging, start }
}
