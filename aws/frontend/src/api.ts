import type { QueueResponse, SessionState } from './types'

const RAW_BASE = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')
const BASE = RAW_BASE || (import.meta.env.DEV ? 'http://localhost:8000' : '')

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: HeadersInit = {
    ...(typeof init?.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
    ...(init?.headers || {}),
  }
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: 'omit',
    headers,
  })

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // Keep the status text.
    }
    throw new Error(detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  createSession: () =>
    request<SessionState>('/sessions', { method: 'POST', body: '{}' }),

  getSession: (sessionId: string) =>
    request<SessionState>(`/sessions/${sessionId}`),

  sendMessage: (sessionId: string, message: string, forceSearch: boolean) =>
    request<QueueResponse>(`/sessions/${sessionId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message, forceSearch }),
    }),

  select: (sessionId: string, index: number) =>
    request<QueueResponse>(`/sessions/${sessionId}/select`, {
      method: 'POST',
      body: JSON.stringify({ index }),
    }),

  reset: (sessionId: string) =>
    request<SessionState>(`/sessions/${sessionId}/reset`, { method: 'POST' }),

  deleteSession: (sessionId: string) =>
    request<void>(`/sessions/${sessionId}`, { method: 'DELETE' }),
}
