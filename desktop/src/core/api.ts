// Thin HTTP client for the Ordexa API. Distinguishes "server unreachable" (keep billing offline)
// from "server said no" (show the reason).

export class NetworkError extends Error {}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

export interface ApiOptions {
  baseUrl: string
  token?: string | null
  deviceId?: string | null
  timeoutMs?: number
}

export async function apiRequest<T>(opts: ApiOptions, method: 'GET' | 'POST' | 'PUT' | 'DELETE', path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`
  if (opts.deviceId) headers['X-Device-Id'] = opts.deviceId
  let res: Response
  try {
    res = await fetch(`${opts.baseUrl.replace(/\/+$/, '')}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(opts.timeoutMs ?? 60_000), // free hosting tiers can take ~50 s to wake up
    })
  } catch (e) {
    throw new NetworkError(`server unreachable: ${(e as Error).message}`)
  }
  const text = await res.text()
  let data: any = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    // Not our API answering (hosting proxy page while the server wakes up or restarts).
    if (res.status === 502 || res.status === 503 || res.status === 504 || res.ok) {
      throw new NetworkError(`server not ready (HTTP ${res.status})`)
    }
  }
  if (res.status === 502 || res.status === 503 || res.status === 504) {
    throw new NetworkError(`server not ready (HTTP ${res.status})`)
  }
  if (!res.ok) {
    const err = data?.error
    throw new ApiError(res.status, err?.code ?? 'HTTP_ERROR', err?.message ?? data?.detail?.[0]?.msg ?? `HTTP ${res.status}`)
  }
  return data as T
}

/** Production servers must use HTTPS (passwords and business data travel over it). Plain http is allowed only for
 * this PC or the shop's own network (development / on-premise). */
export function checkServerUrl(raw: string): string {
  let u: URL
  try {
    u = new URL(raw.trim())
  } catch {
    throw new Error('Server address is not a valid URL, e.g. https://ordexa-api.onrender.com')
  }
  const local = /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(u.hostname)
  if (u.protocol !== 'https:' && !(u.protocol === 'http:' && local)) {
    throw new Error('Use an https:// server address (http is only allowed on this PC or the local network).')
  }
  return u.origin
}
