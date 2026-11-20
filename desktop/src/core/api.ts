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

export async function apiRequest<T>(opts: ApiOptions, method: 'GET' | 'POST' | 'PUT', path: string, body?: unknown): Promise<T> {
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
      signal: AbortSignal.timeout(opts.timeoutMs ?? 20_000),
    })
  } catch (e) {
    throw new NetworkError(`server unreachable: ${(e as Error).message}`)
  }
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) {
    const err = data?.error
    throw new ApiError(res.status, err?.code ?? 'HTTP_ERROR', err?.message ?? data?.detail?.[0]?.msg ?? `HTTP ${res.status}`)
  }
  return data as T
}
