// Interaction effects applied app-wide by event delegation (no per-component wiring):
//  - ripple: a soft "liquid" ripple from the click point on buttons
//  - tilt: gentle 3D tilt that follows the pointer on elements marked .tilt (KPI tiles, stats)
// Both are skipped when the OS asks for reduced motion.

const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches

function ripple(e: PointerEvent) {
  const el = (e.target as HTMLElement | null)?.closest<HTMLElement>('.btn, .folder, .view-switch button, .sidebar button, .kpi, .seg button')
  if (!el || reduced() || (el as HTMLButtonElement).disabled) return
  const r = el.getBoundingClientRect()
  const size = Math.max(r.width, r.height) * 1.6
  const dot = document.createElement('span')
  dot.className = 'fx-ripple'
  dot.style.width = dot.style.height = `${size}px`
  dot.style.left = `${e.clientX - r.left - size / 2}px`
  dot.style.top = `${e.clientY - r.top - size / 2}px`
  el.appendChild(dot)
  dot.addEventListener('animationend', () => dot.remove(), { once: true })
}

let tilted: HTMLElement | null = null
function tilt(e: PointerEvent) {
  if (reduced()) return
  const el = (e.target as HTMLElement | null)?.closest<HTMLElement>('.tilt')
  if (tilted && tilted !== el) {
    tilted.style.setProperty('--rx', '0deg')
    tilted.style.setProperty('--ry', '0deg')
    tilted = null
  }
  if (!el) return
  const r = el.getBoundingClientRect()
  const x = (e.clientX - r.left) / r.width - 0.5
  const y = (e.clientY - r.top) / r.height - 0.5
  el.style.setProperty('--rx', `${(-y * 6).toFixed(2)}deg`)
  el.style.setProperty('--ry', `${(x * 8).toFixed(2)}deg`)
  el.style.setProperty('--mx', `${((x + 0.5) * 100).toFixed(1)}%`)
  el.style.setProperty('--my', `${((y + 0.5) * 100).toFixed(1)}%`)
  tilted = el
}

export function installFx() {
  document.addEventListener('pointerdown', ripple, { passive: true })
  document.addEventListener('pointermove', tilt, { passive: true })
}
