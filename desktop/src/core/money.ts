// Money is held as integer paisa and quantities as integer milli-units, so the desktop never does
// floating-point arithmetic on money. Rounding is half-up, matching the backend's Decimal ROUND_HALF_UP.

export type Paisa = number
export type Milli = number

function parseScaled(value: string | number, scale: number): number {
  const text = String(value).trim()
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text)
  if (!m) throw new Error(`not a number: "${text}"`)
  const [, sign, whole, frac = ''] = m
  const digits = frac.padEnd(scale + 1, '0')
  let n = Number(whole) * 10 ** scale + Number(digits.slice(0, scale) || '0')
  if (Number(digits[scale]) >= 5) n += 1 // half-up on the first dropped digit
  if (!Number.isSafeInteger(n)) throw new Error(`number out of range: "${text}"`)
  return sign ? -n : n
}

export const toPaisa = (v: string | number): Paisa => parseScaled(v, 2)
export const toMilli = (v: string | number): Milli => parseScaled(v, 3)

function formatScaled(n: number, scale: number): string {
  const sign = n < 0 ? '-' : ''
  const abs = Math.abs(n)
  const base = 10 ** scale
  return `${sign}${Math.floor(abs / base)}.${String(abs % base).padStart(scale, '0')}`
}

export const paisaToString = (p: Paisa): string => formatScaled(p, 2)
export const milliToString = (m: Milli): string => {
  const s = formatScaled(m, 3)
  return s.replace(/\.?0+$/, '') || '0'
}

/** Display format for PKR, e.g. "Rs 12,450.00". */
export function formatRs(p: Paisa): string {
  const [w, f] = paisaToString(Math.abs(p)).split('.')
  return `${p < 0 ? '-' : ''}Rs ${Number(w).toLocaleString('en-PK')}.${f}`
}

/** qty x price - discount, rounded half-up to paisa (same as backend ``line_total``). */
export function lineTotal(qty: Milli, unitPrice: Paisa, discount: Paisa = 0): Paisa {
  const raw = qty * unitPrice // milli-paisa
  if (!Number.isSafeInteger(raw)) throw new Error('line amount out of range')
  const rounded = raw >= 0 ? Math.floor((raw + 500) / 1000) : -Math.floor((-raw + 500) / 1000)
  return rounded - discount
}
